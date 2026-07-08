"""Learning & Adaptation / Consolidation (spec §18): the slow loop.

Runs after session close, always off the conversation path (queued via §14).
One LLM analysis pass over the transcript feeds: semantic fact extraction
(§6), procedural rule reinforcement with contradiction handling (§7), mood
baseline + trait confidence updates (§17), and candidate correlations that
are stored — not acted on — until repeated confirmation (rule 3).

Guardrails (rule 4): correlations are labeled correlation-not-causation,
contradicting evidence lowers confidence instead of being ignored, and
nothing here produces a diagnosis — inference quality is human-validated.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from core.memory.episodic import EpisodicMemory
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.working import Turn
from core.psych.user_model import OCEAN_TRAITS, PsychUserModel
from ports.doc_store import DocStore
from ports.llm import LLM, LLMUnavailable
from ports.queue import QueuedTask

logger = logging.getLogger(__name__)

CORRELATIONS_COLLECTION = "psych_correlations"
CONSOLIDATION_TASK_TYPE = "consolidation"

# Confirmation gate (rule 3): a correlation is exposed only after this many
# independent sightings.
CORRELATION_CONFIRM_THRESHOLD = 3

_ANALYSIS_INSTRUCTIONS = """
You analyze one conversation session for long-term learning. Respond ONLY
with JSON:
{"behavior_observations": [
   {"rule_text": "<'when user X, do Y' phrasing>",
    "trigger": "<short trigger phrase>",
    "action": "<short action>",
    "evidence": "confirming" | "contradicting"}
 ],
 "dominant_topic": "<main topic of the session or null>",
 "session_valence": <-1..1 overall emotional tone>,
 "session_arousal": <-1..1 overall energy>,
 "ocean_evidence": {"<trait>": <0..1>}  // ONLY traits with real signal this session
}
Observations must be interaction patterns useful for a companion (how to
respond), not facts. Evidence is "contradicting" when the session contradicts
the stated rule. Never include medical or psychological labels.
""".strip()


class Observation(BaseModel):
    rule_text: str
    trigger: str
    action: str
    evidence: str = "confirming"


class SessionAnalysis(BaseModel):
    behavior_observations: list[Observation] = Field(default_factory=list)
    dominant_topic: str | None = None
    session_valence: float = 0.0
    session_arousal: float = 0.0
    ocean_evidence: dict[str, float] = Field(default_factory=dict)


class ConsolidationReport(BaseModel):
    facts_extracted: bool = False
    rules_added: int = 0
    rules_reinforced: int = 0
    rules_contradicted: int = 0
    mood_updated: bool = False
    traits_updated: int = 0
    candidate_correlations: int = 0
    duplicates_removed: int = 0  # §5 episodic dedup during consolidation


class Consolidator:
    def __init__(
        self,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
        psych: PsychUserModel,
        docs: DocStore,
        llm: LLM,
        episodic: EpisodicMemory | None = None,
    ) -> None:
        self._semantic = semantic
        self._procedural = procedural
        self._psych = psych
        self._docs = docs
        self._llm = llm
        self._episodic = episodic

    async def consolidate(
        self, user_id: str, session_id: str, transcript: list[Turn]
    ) -> ConsolidationReport:
        report = ConsolidationReport()
        if not transcript:
            return report
        text = "\n".join(f"{t.role}: {t.text}" for t in transcript)

        # (a) semantic facts with validity windows — Graphiti extraction.
        await self._semantic.add_episode(user_id, text)
        report.facts_extracted = True

        # (a2) episodic dedup (§5): collapse near-duplicate events accreted across
        # sessions so retrieval doesn't surface the same fact 2-3 times. Off the
        # latency path (this runs in the worker), user-scoped, best-effort.
        if self._episodic is not None:
            try:
                report.duplicates_removed = await self._episodic.deduplicate(user_id)
            except Exception:  # never let dedup break the rest of consolidation
                logger.exception("episodic dedup failed during consolidation")

        analysis = await self._analyze(user_id, session_id, text)
        if analysis is None:
            return report  # extraction happened; the rest degrades gracefully

        # (b) interaction patterns → procedural memory.
        await self._apply_observations(user_id, analysis, report)

        # (c) mood baseline.
        emotional_turns = [t for t in transcript if t.emotion]
        valence, arousal = _session_mood(emotional_turns, analysis)
        below_usual = await self._psych.is_below_baseline(user_id, valence)
        await self._psych.update_mood(user_id, valence, arousal)
        report.mood_updated = True

        # (d) correlation analysis — candidate until confirmed (rule 3).
        if analysis.dominant_topic and below_usual:
            await self._record_correlation(
                user_id,
                key=f"topic:{analysis.dominant_topic.lower()}|mood:low",
                description=(
                    f"sessions about '{analysis.dominant_topic}' co-occurred with "
                    "below-baseline mood (correlation, not causation)"
                ),
            )
            report.candidate_correlations = 1

        # (e) trait confidence nudges.
        for trait, evidence in analysis.ocean_evidence.items():
            if trait in OCEAN_TRAITS:
                await self._psych.update_trait(user_id, trait, float(evidence))
                report.traits_updated += 1
        return report

    async def confirmed_correlations(self, user_id: str) -> list[dict[str, Any]]:
        """Only correlations past the confirmation gate — safe to act on."""
        docs = await self._docs.find(
            CORRELATIONS_COLLECTION, {"user_id": user_id, "status": "confirmed"}
        )
        return docs

    def task_handler(self) -> Any:
        """§14 worker handler: consolidation runs post-session, never live (rule 5)."""

        async def handle(task: QueuedTask) -> dict[str, Any]:
            transcript = [Turn.model_validate(t) for t in task.params.get("transcript", [])]
            report = await self.consolidate(task.user_id, task.session_id, transcript)
            return report.model_dump()

        return handle

    # ── internals ────────────────────────────────────────────────────────

    async def _analyze(self, user_id: str, session_id: str, text: str) -> SessionAnalysis | None:
        messages = [
            {"role": "system", "content": _ANALYSIS_INSTRUCTIONS},
            {"role": "user", "content": text[:12_000]},
        ]
        for _ in range(2):  # validate; retry once (§0.5)
            try:
                result = await self._llm.complete(
                    user_id,
                    messages,
                    "moderate",
                    response_format={"type": "json_object"},
                    session_id=session_id,
                    purpose="psych_consolidation",
                )
                return SessionAnalysis.model_validate(json.loads(result.text))
            except (LLMUnavailable, ValidationError, ValueError):
                continue
        logger.warning("session analysis failed twice; skipping pattern learning")
        return None

    async def _apply_observations(
        self, user_id: str, analysis: SessionAnalysis, report: ConsolidationReport
    ) -> None:
        existing = await self._all_rules(user_id)
        for observation in analysis.behavior_observations:
            match = _best_match(observation, existing)
            if observation.evidence == "contradicting":
                if match is not None:
                    # Contradiction lowers confidence — steeper than the gain.
                    await self._procedural.reinforce(user_id, match, delta=-0.12)
                    report.rules_contradicted += 1
                continue
            if match is not None:
                await self._procedural.reinforce(user_id, match)
                report.rules_reinforced += 1
            else:
                await self._procedural.add_candidate(
                    user_id,
                    rule_text=observation.rule_text,
                    trigger=observation.trigger,
                    action=observation.action,
                )
                report.rules_added += 1

    async def _all_rules(self, user_id: str) -> list[dict[str, Any]]:
        return await self._docs.find("procedural", {"user_id": user_id}, limit=500)

    async def _record_correlation(self, user_id: str, key: str, description: str) -> None:
        existing = await self._docs.find(CORRELATIONS_COLLECTION, {"user_id": user_id, "key": key})
        if existing:
            doc = existing[0]
            doc["evidence_count"] = int(doc.get("evidence_count", 1)) + 1
            if doc["evidence_count"] >= CORRELATION_CONFIRM_THRESHOLD:
                doc["status"] = "confirmed"
            doc["updated_at"] = datetime.now(UTC).isoformat()
            await self._docs.put(CORRELATIONS_COLLECTION, doc["_id"], doc)
            return
        await self._docs.put(
            CORRELATIONS_COLLECTION,
            str(uuid.uuid4()),
            {
                "user_id": user_id,
                "key": key,
                "description": description,
                "evidence_count": 1,
                "status": "candidate",
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )


def _session_mood(emotional_turns: list[Turn], analysis: SessionAnalysis) -> tuple[float, float]:
    """Prefer measured per-turn emotion (§22); fall back to the LLM's read."""
    if emotional_turns:
        valences = [float((t.emotion or {}).get("valence", 0.0)) for t in emotional_turns]
        arousals = [float((t.emotion or {}).get("arousal", 0.0)) for t in emotional_turns]
        return sum(valences) / len(valences), sum(arousals) / len(arousals)
    return (
        max(-1.0, min(1.0, analysis.session_valence)),
        max(-1.0, min(1.0, analysis.session_arousal)),
    )


def _best_match(observation: Observation, rules: list[dict[str, Any]]) -> str | None:
    """Match an observation to an existing rule by trigger-word overlap."""
    observation_words = _words(observation.trigger)
    best_id, best_overlap = None, 0
    for rule in rules:
        overlap = len(observation_words & _words(str(rule.get("trigger", ""))))
        if overlap > best_overlap:
            best_id, best_overlap = str(rule["_id"]), overlap
    return best_id if best_overlap >= 1 else None


def _words(text: str) -> set[str]:
    return {w.strip(".,!?").lower() for w in text.split() if len(w) > 2}
