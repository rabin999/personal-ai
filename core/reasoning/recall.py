"""Conversation-recall routing (F3/F4): answer "what did I say" and "what did we
talk about yesterday" from the ACTUAL conversation, not long-term memory facts.

Three sources, three intents (spec §4 working memory, §6 conversation store):
  - ``current``  — a question about THIS session's turns ("what did I just say",
    "2 messages ago", "before that", "at the start of this chat"). Answered from
    the ordered in-session transcript (working memory).
  - ``past``     — a question about a PRIOR conversation ("what did we talk about
    yesterday / last time", "did I mention X before"). Answered from the durable
    conversation store, by recency/time.
  - ``none``     — a normal turn; no transcript injected.

Detection is deterministic (fast, pre-LLM) so a recall question is ROUTED to the
right store instead of being answered by semantic/episodic memory — the reported
failure where "what did I say before that" returned a memory fact. The assembled
transcript is injected as a high-priority, non-trimmed section that tells the model
to answer from those exact messages.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal, Protocol

RecallKind = Literal["current", "past", "none"]

# A recall question always references *saying/talking*, not just any past event.
_RECALL_VERB = re.compile(
    r"\b(say|said|saying|tell|told|talk|talking|talked|discuss(?:ed|ing)?|"
    r"mention(?:ed|ing)?|ask(?:ed)?|chat(?:ted|ting)?|bring up|brought up|were saying)\b",
    re.I,
)
# In-session cues → the CURRENT conversation.
_CURRENT_CUE = re.compile(
    r"\b(just|a (?:moment|second|sec|minute) ago|earlier|before that|"
    r"\d+\s+messages?\s+ago|last (?:message|thing)|previously in (?:this|our)|"
    r"(?:start|beginning|top) of (?:this|our|the) (?:chat|conversation|call)|"
    r"so far|up to now|were we|we were|been (?:talking|saying)|this (?:chat|conversation))\b",
    re.I,
)
# Prior-session cues → a PAST conversation (a different day/session).
_PAST_CUE = re.compile(
    r"\b(yesterday|last time|last (?:week|night|conversation|chat|session|time)|"
    r"the other day|earlier today|this morning|a while ago|days ago|"
    r"previous(?:ly)? (?:chat|conversation|session|time)|before,? in|"
    r"when we (?:last )?(?:talked|spoke|chatted)|our last (?:chat|conversation))\b",
    re.I,
)
# "what did I/we say/talk about …" — the canonical recall question shape.
_RECALL_QUESTION = re.compile(r"\b(what|which|when|did|had|remember|recall|remind me)\b.*", re.I)


def classify_recall(utterance: str) -> RecallKind:
    """Route a recall question to current-session vs past-conversation vs none.

    A past cue wins over a current cue (an explicit "yesterday"/"last time" is a
    stronger signal than a generic "we were talking"). Requires a recall verb so
    "I felt awful yesterday" (a statement, not a recall question) stays ``none``.
    """
    text = utterance.strip()
    # "before that?" / "2 messages ago?" can omit the verb but are still positional
    # recall follow-ups — allow those explicit shapes even without a recall verb.
    positional = re.search(r"\b(\d+\s+messages?\s+ago|before that)\b", text, re.I)
    if not _RECALL_VERB.search(text) and not positional:
        return "none"
    has_question = bool(_RECALL_QUESTION.search(text)) or "?" in text
    if not has_question:
        return "none"
    if _PAST_CUE.search(text):
        return "past"
    if _CURRENT_CUE.search(text):
        return "current"
    return "none"


class _Turn(Protocol):
    @property
    def role(self) -> str: ...
    @property
    def text(self) -> str: ...


class _ConversationStore(Protocol):
    async def list_conversations(
        self,
        user_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]: ...

    async def turns(
        self, user_id: str, session_id: str, *, offset: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]: ...


# Cap injected transcript size so a long session can't blow the token budget (F14
# handles rolling compaction separately; this just bounds the recall section).
_MAX_CURRENT_TURNS = 40
_MAX_PAST_SESSIONS = 3
_MAX_PAST_TURNS = 30

_CURRENT_HEADER = (
    "## The actual conversation so far (authoritative)\n"
    "The user is asking about what was ACTUALLY said in THIS conversation. Answer "
    "using these exact messages, in this order — quote the user's own words. Do NOT "
    "answer from long-term memory facts; use the transcript below. Count positions "
    'from the most recent ("just now"/"last thing" = the latest; "2 messages ago" / '
    '"before that" = further back).'
)
_PAST_HEADER = (
    "## A past conversation you're being asked about (authoritative)\n"
    "The user is asking about a PRIOR conversation (a different day/session). Answer "
    "from the stored transcript(s) below — what you actually discussed — not from "
    "long-term memory facts. If nothing here matches, say you don't have that "
    "conversation rather than inventing one."
)


def render_current_transcript(turns: Sequence[_Turn], companion_name: str | None = None) -> str:
    """Numbered, ordered transcript of the current session (oldest→newest)."""
    name = companion_name or "companion"
    shown = turns[-_MAX_CURRENT_TURNS:]
    elided = len(turns) - len(shown)
    lines = []
    if elided > 0:
        lines.append(f"(… {elided} earlier message(s) omitted …)")
    for i, t in enumerate(shown, start=1):
        who = "user" if t.role == "user" else name
        lines.append(f"{i}. {who}: {t.text}")
    return _CURRENT_HEADER + "\n" + "\n".join(lines)


class ConversationRecall:
    """Builds the authoritative recall section for the current or a past session."""

    def __init__(self, store: _ConversationStore) -> None:
        self._store = store

    async def past_section(self, user_id: str, current_session_id: str) -> tuple[str, list[str]]:
        """Most-recent PAST sessions' transcripts as an authoritative block.

        Returns ``(section_text, source_session_ids)``; empty text if there are no
        prior conversations. Isolation: every store read is ``user_id``-scoped.
        """
        headers, _ = await self._store.list_conversations(user_id, limit=_MAX_PAST_SESSIONS + 3)
        past = [h for h in headers if h.get("session_id") != current_session_id]
        if not past:
            return "", []
        blocks: list[str] = []
        used: list[str] = []
        for header in past[:_MAX_PAST_SESSIONS]:
            sid = str(header.get("session_id", ""))
            when = str(header.get("last_at_iso") or header.get("started_at_iso") or "")[:10]
            turns = await self._store.turns(user_id, sid, limit=_MAX_PAST_TURNS)
            if not turns:
                continue
            lines = [f"### Conversation on {when or 'an earlier day'}"]
            for t in turns:
                u = (t.get("user_text") or "").strip()
                a = (t.get("assistant_text") or "").strip()
                if u:
                    lines.append(f"- user: {u}")
                if a:
                    lines.append(f"- companion: {a}")
            blocks.append("\n".join(lines))
            used.append(sid)
        if not blocks:
            return "", []
        return _PAST_HEADER + "\n" + "\n\n".join(blocks), used
