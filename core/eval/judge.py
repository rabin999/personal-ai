"""LLM-as-judge for companion-voice quality (CLAUDE.md §6/§7).

A separate, strong, PINNED model scores a captured companion reply against THIS
app's design standard (design doc §1.2-1.4, §3). Calibrated to the app's real
goals: it hard-fails genuine chatbot/assistant-speak but does NOT penalize the
companion working as designed (recalling what the person told you, warm sympathy,
one curious follow-up) — a naive "aloof cool friend" judge wrongly fails those,
which are headline features.

Lives in ``core`` (depends only on the LLM port) so BOTH the live per-turn
evaluator and the real-call test suites share one calibrated rubric — no drift.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from ports.llm import LLM as JudgeLLM
from ports.llm import Tier

# Pinned judge tier. "complex" routes to the strongest configured model so the
# judge itself is not a weak model.
JUDGE_TIER: Tier = "complex"


class Verdict(BaseModel):
    chatbot_like: bool
    companion_score: int  # 1 (pure chatbot) .. 5 (great friend)
    reason: str

    @property
    def ok(self) -> bool:
        """Passes the companion-voice bar: not chatbot-like and at least decent."""
        return not self.chatbot_like and self.companion_score >= 3


COMPANION_VOICE_RUBRIC = """You evaluate replies from a warm, human, voice-first AI
COMPANION — a friend who remembers you and is curious about you (NOT an assistant).
Judge ONLY the companion's reply against THIS app's design standard.

HARD FAIL (chatbot_like=true) ONLY for genuine assistant/chatbot behavior:
- service-desk phrasing ("How can I help you?", "I'm here to assist", "happy to
  help", "I can help with that", "is there anything else?"), corporate/support tone;
- VOLUNTEERED AI disclaimers when the user did NOT ask about the companion's nature
  ("As an AI I don't have feelings", "I can't feel or think like you do", "my purpose
  is to assist", "my existence is about processing information");
- deflecting a real/philosophical question with a lecture about being an AI;
- clarifying an obviously-clear message ("what do you mean?", "tell me more about
  what you mean", "I want to make sure I get this right") instead of engaging;
- listy / over-explaining / over-helpful hedging, or robotic, generic phrasing.

Do NOT penalize (these are the companion working AS DESIGNED — they are GOOD):
- naturally recalling something the person told you before (memory is a core feature);
- warm sympathy for someone venting, and being genuinely present with a feeling;
- ONE genuine, curious follow-up question (the companion is meant to be curious);
- using the person's name, enthusiasm, or emotional warmth;
- a brief, warm, honest one-sentence "I'm an AI" ONLY when the user asked about its nature.

PASS if it sounds like a genuine, warm, present friend and avoids the hard-fail
behaviors above. Be fair, not preciously harsh — judge against a real warm friend.

Return ONLY JSON: {"chatbot_like": bool, "companion_score": 1-5, "reason": "<one sentence>"}."""


def _extract_json(text: str) -> dict[str, Any]:
    t = text.strip()
    if "{" in t and "}" in t:
        t = t[t.index("{") : t.rindex("}") + 1]
    data: dict[str, Any] = json.loads(t)
    return data


async def judge_companion_voice(llm: JudgeLLM, user_msg: str, reply: str) -> Verdict:
    """Score one (user turn, companion reply) pair against the companion-voice rubric."""
    messages = [
        {"role": "system", "content": COMPANION_VOICE_RUBRIC},
        {"role": "user", "content": f"USER said: {user_msg!r}\nCOMPANION replied: {reply!r}"},
    ]
    result = await llm.complete(
        "judge",
        messages,
        JUDGE_TIER,
        response_format={"type": "json_object"},
        temperature=0.2,  # P2: a judgment → low temp, consistent scoring
        purpose="judge",
    )
    return Verdict.model_validate(_extract_json(result.text))
