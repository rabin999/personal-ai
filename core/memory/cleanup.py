"""Memory cleanup pass (brief U1): remove accreted gibberish from the stores.

The root-cause fix for gibberish is at the WRITE step (better extraction prompt +
quality bar + persona routing in ``extraction.py``). This module cleans what was
ALREADY stored: it enumerates a user's semantic facts and episodic events, asks a
pinned judge model which are meaningful vs. junk (malformed fragments, filler,
one-off trivia, the companion's own chatter mis-stored as a fact), and deletes the
junk — plus runs episodic dedup. Everything is ``user_id``-scoped (§0.5) and
best-effort; a judge failure keeps the item (never deletes on uncertainty).

Run:  uv run python -m scripts.clean_memory [user_id ...]
"""

import json
import logging

from pydantic import BaseModel, Field, ValidationError

from core.memory.episodic import EpisodicMemory
from core.memory.semantic import SemanticMemory
from ports.llm import LLM, LLMUnavailable

logger = logging.getLogger(__name__)

_JUDGE_INSTRUCTIONS = """
You audit stored long-term memories of a personal companion for QUALITY. For each
numbered item, decide if it is a MEANINGFUL, durable memory worth keeping, or
GIBBERISH/JUNK that should be dropped.

DROP when the item is: a malformed or garbled fragment; empty/near-empty; filler or
chit-chat ("ok", "haha", "sounds good"); a one-off triviality that won't matter
later; the COMPANION's own suggestion/chatter mis-stored as if it were about the
user; or duplicated noise. KEEP genuine facts, real events, preferences, routines,
relationships, health/goals.

Respond ONLY with JSON: {"drop": [<indices to delete>]}. When unsure, KEEP (omit it).
""".strip()


class CleanupReport(BaseModel):
    semantic_reviewed: int = 0
    semantic_deleted: int = 0
    episodic_reviewed: int = 0
    episodic_deleted: int = 0
    episodic_deduped: int = 0
    dropped: list[str] = Field(default_factory=list)


class MemoryCleaner:
    def __init__(self, llm: LLM, semantic: SemanticMemory, episodic: EpisodicMemory) -> None:
        self._llm = llm
        self._semantic = semantic
        self._episodic = episodic

    async def clean_user(self, user_id: str) -> CleanupReport:
        report = CleanupReport()
        # Semantic facts (Graphiti): judge + hard-delete the junk edges.
        facts = await self._semantic.all_facts(user_id, limit=300)
        report.semantic_reviewed = len(facts)
        drop_idx = await self._judge(user_id, [f.fact for f in facts])
        for i in drop_idx:
            fact = facts[i]
            if fact.uuid and await self._semantic.delete_fact(user_id, fact.uuid):
                report.semantic_deleted += 1
                report.dropped.append(f"fact: {fact.fact}")

        # Episodic events (Qdrant): judge + delete, then collapse near-duplicates.
        events = await self._episodic.list_recent(user_id, limit=300)
        report.episodic_reviewed = len(events)
        drop_idx = await self._judge(user_id, [e.text for e in events])
        for i in drop_idx:
            event = events[i]
            if event.id and await self._episodic.delete(user_id, event.id):
                report.episodic_deleted += 1
                report.dropped.append(f"event: {event.text}")
        try:
            report.episodic_deduped = await self._episodic.deduplicate(user_id)
        except Exception:
            logger.exception("episodic dedup failed during cleanup")
        return report

    async def _judge(self, user_id: str, items: list[str]) -> list[int]:
        """Indices the judge marks as junk. On any failure, drop nothing (KEEP)."""
        if not items:
            return []
        numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(items))
        messages = [
            {"role": "system", "content": _JUDGE_INSTRUCTIONS},
            {"role": "user", "content": numbered},
        ]
        for _ in range(2):
            try:
                result = await self._llm.complete(
                    user_id,
                    messages,
                    "simple",
                    response_format={"type": "json_object"},
                    purpose="memory_cleanup",
                )
                data = _Drop.model_validate_json(_strip(result.text))
                return [i for i in data.drop if 0 <= i < len(items)]
            except (LLMUnavailable, ValidationError, ValueError, json.JSONDecodeError):
                continue
        logger.warning("cleanup judge failed twice; keeping all %d items", len(items))
        return []


class _Drop(BaseModel):
    drop: list[int] = Field(default_factory=list)


def _strip(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0]
    return stripped.strip()
