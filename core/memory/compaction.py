"""Rolling session compaction for long-session endurance (F14).

A session that runs for hours accumulates hundreds of turns. Feeding them all into
the prompt would blow the token budget and creep latency/cost upward. Instead, once
the live working-memory buffer grows past a threshold, the older turns are folded
into a running SUMMARY (an LLM call, run OFF the reply path) and dropped from the
buffer — the raw turns stay in the durable conversation store (§6), still recallable
(F3/F4). The summary + the recent turns keep the companion coherent across a long
day without the prompt growing unbounded.

The compactor is idempotent-ish and best-effort: a failed summarization leaves the
buffer intact (it just retries next turn), and it never blocks the conversation.
"""

import logging
from typing import Protocol

from core.memory.working import Turn, WorkingMemory
from ports.llm import LLM, LLMUnavailable

logger = logging.getLogger(__name__)

# Compact when the buffer exceeds this; keep this many recent turns verbatim in the
# prompt. Deployment-tunable — the property (bounded prompt) is the invariant.
COMPACT_THRESHOLD = 24
KEEP_RECENT = 8

_SUMMARY_INSTRUCTIONS = (
    "You maintain a running summary of a long conversation between a user and their "
    "companion, so the companion remembers the thread without keeping every message. "
    "Update the summary with the new exchanges below. Keep it COMPACT (a few short "
    "paragraphs max): the topics discussed, decisions/plans made, facts the user "
    "shared about themselves, their emotional state, and anything still open. Write "
    "it as notes for the companion, third person ('The user…'). Preserve important "
    "specifics (names, numbers, dates). Do NOT invent anything.\n\n"
    "Existing summary (may be empty):\n{summary}\n\n"
    "New exchanges to fold in:\n{new}\n\n"
    "Updated running summary:"
)


class _Logs(Protocol):
    def log(self, level: str, event: str, **fields: object) -> None: ...


class SessionCompactor:
    def __init__(self, llm: LLM, working: WorkingMemory, logs: _Logs | None = None) -> None:
        self._llm = llm
        self._working = working
        self._logs = logs

    def should_compact(self, session_id: str) -> bool:
        return self._working.size(session_id) > COMPACT_THRESHOLD

    async def maybe_compact(
        self, session_id: str, user_id: str, *, keep_recent: int = KEEP_RECENT
    ) -> int:
        """If the session buffer is over threshold, summarize the overflow into the
        rolling summary and drop it from the buffer. Returns turns compacted (0 if
        none / on failure). Safe to call after every turn — cheap no-op when small."""
        buffer = self._working.all(session_id)
        overflow = len(buffer) - keep_recent
        if overflow < (COMPACT_THRESHOLD - keep_recent):  # below threshold → nothing to do
            return 0
        to_fold = buffer[:overflow]
        try:
            summary = await self._summarize(
                user_id, session_id, self._working.summary(session_id), to_fold
            )
        except LLMUnavailable:
            logger.warning("session compaction summarization unavailable; will retry next turn")
            return 0
        if not summary.strip():
            return 0
        dropped = self._working.compact(session_id, keep_recent=keep_recent, summary=summary)
        if dropped and self._logs is not None:
            self._logs.log(
                "info",
                "session.compact",
                stage="memory",
                session_id=session_id,
                dropped_turns=dropped,
                summary_chars=len(summary),
                buffer_after=self._working.size(session_id),
            )
        return dropped

    async def _summarize(self, user_id: str, session_id: str, prior: str, turns: list[Turn]) -> str:
        convo = "\n".join(f"{t.role}: {t.text}" for t in turns)
        prompt = _SUMMARY_INSTRUCTIONS.format(summary=prior or "(none yet)", new=convo)
        result = await self._llm.complete(
            user_id,
            [{"role": "user", "content": prompt}],
            "simple",  # cheap tier — summarization is off the reply path
            session_id=session_id,
            max_tokens=500,
            purpose="compaction",
        )
        return result.text.strip()
