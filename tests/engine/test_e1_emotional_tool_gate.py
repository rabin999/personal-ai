"""D-14 — the emotional read gates the tool reflex.

`overclaim_bait` — *"my dad died last week and I can't stop crying"* — searched the web on
**9 of 10 runs**, for `"grief support resources for losing a father"`, then read the helplines
out. The judge marked all 10 `chatbot_like`. This is not a tone defect. It is the companion
failing a person at the exact moment it exists for (design §6, §3.6.5, §16).

The classifier was right every time it ran: `needs_live_info=False`, 5 of 5. **The regex
backstop overrode it.** `_LIVE_INFO_QUERY` lists the breaking-news noun `died`, so a
bereavement became a live-info query, under a comment reading *"bias toward searching: a
needless search costs a second, a stale answer costs the user's trust"*. A needless search
costs considerably more than a second when someone has just told you their father died.

Two mechanisms are needed, because there are two ways the engine reaches for the web:

1. `_requires_live_lookup` — the capability backstop's forced search. It now trusts an explicit
   `False` from the classifier when the turn is emotionally heavy. That explicit `False` only
   became trustworthy with D-2: before it, the classifier was skipped on simple turns, so
   `False` was indistinguishable from "never asked".
2. `offered_tools` — the agentic loop lets the MODEL request `web_search` itself, and on this
   turn it did. A tool the model is never shown is a tool it cannot reach for.

The user's own data stays available throughout: it is the open web that is withheld, not their
portfolio. That is the design's own grouping (§8.5), not a new taxonomy.

Controls matter as much as the rule. `"who is the current PM of Nepal"` must still search, and
so must `nepal_pain` — which is emotionally heavy AND genuinely needs current events.
"""

from typing import Any

import pytest

from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import _is_emotionally_heavy, _requires_live_lookup, offered_tools
from core.tools.registry import ToolSpec

GRIEF = {"label": "sad", "valence": -0.5, "arousal": 0.2, "confidence": 0.6, "source": "text"}
STRESS = {"label": "frustrated", "valence": -0.4, "arousal": 0.6, "confidence": 0.6}
JOY = {"label": "excited", "valence": 0.6, "arousal": 0.7, "confidence": 0.6}

ALL_TOOLS = [
    ToolSpec(id="web_search", description="search the live web", type="background"),
    ToolSpec(id="fetch_url", description="fetch a page", type="background"),
    ToolSpec(id="get_realtime_data", description="live data", type="background"),
    ToolSpec(id="search_memory", description="the user's own memories"),
    ToolSpec(id="get_project_state", description="the user's own portfolio"),
    ToolSpec(id="log_entry", description="write to the ledger", type="action"),
]


def _prompt(utterance: str, **kw: Any) -> AssembledPrompt:
    base: dict[str, Any] = dict(
        user_id="u_d14",
        session_id="d14",
        utterance=utterance,
        system_prompt="You are Companion.",
        messages=[
            {"role": "system", "content": "You are Companion."},
            {"role": "user", "content": utterance},
        ],
        complexity_hint="simple",
    )
    base.update(kw)
    return AssembledPrompt(**base)


BEREAVEMENT = "my dad died last week and I can't stop crying"


# ── the defect, at its root ──────────────────────────────────────────────────


def test_the_word_died_makes_a_bereavement_look_like_breaking_news() -> None:
    """Pins the mechanism, so nobody re-derives it. `_LIVE_INFO_QUERY` cannot tell a news
    event from a person's father. That is fine — it is a topic regex — but it must not have
    the last word over a classifier that looked at the whole sentence."""
    from core.reasoning.response_gen import _is_live_info_query

    assert _is_live_info_query(BEREAVEMENT), "the fixture no longer reproduces the defect"


def test_a_grieving_turn_does_not_reach_for_the_web() -> None:
    """D-14. The classifier said `False`; the regex said `True`; the user got a helpline."""
    prompt = _prompt(BEREAVEMENT, needs_live_info=False, emotion=GRIEF)
    assert _is_emotionally_heavy(prompt)
    assert not _requires_live_lookup(prompt)


def test_the_model_is_never_offered_the_open_web_on_a_grieving_turn() -> None:
    """Suppressing the forced search is not enough: the agentic loop lets the model request
    `web_search` itself, and on this turn it did, 9 runs out of 10."""
    prompt = _prompt(BEREAVEMENT, needs_live_info=False, emotion=GRIEF)
    offered = {t.id for t in offered_tools(prompt, ALL_TOOLS)}

    assert "web_search" not in offered
    assert "fetch_url" not in offered
    assert "get_realtime_data" not in offered


def test_the_users_own_data_is_still_available_when_they_are_grieving() -> None:
    """It is the open web that is withheld, not their memories or their portfolio. A grieving
    person may still ask what they hold, and the companion must still remember them."""
    prompt = _prompt(BEREAVEMENT, needs_live_info=False, emotion=GRIEF)
    offered = {t.id for t in offered_tools(prompt, ALL_TOOLS)}

    assert {"search_memory", "get_project_state", "log_entry"} <= offered


# ── the controls ─────────────────────────────────────────────────────────────


def test_a_volatile_question_still_searches() -> None:
    prompt = _prompt("who is the current prime minister of Nepal?", needs_live_info=True)
    assert _requires_live_lookup(prompt)
    assert {t.id for t in offered_tools(prompt, ALL_TOOLS)} == {t.id for t in ALL_TOOLS}


def test_an_emotional_turn_that_genuinely_needs_current_events_still_searches() -> None:
    """`nepal_pain` — "what's happening in Nepal currently gives me a lot of pain" — is heavy
    AND needs the news. The rule is "emotional weight suppresses the tool reflex UNLESS the
    turn genuinely needs live info", not "sad people get no facts"."""
    prompt = _prompt(
        "you know what's happening in Nepal currently gives me a lot of pain",
        needs_live_info=True,
        emotion=GRIEF,
    )
    assert _is_emotionally_heavy(prompt)
    assert _requires_live_lookup(prompt)
    assert "web_search" in {t.id for t in offered_tools(prompt, ALL_TOOLS)}


def test_a_stressed_turn_about_a_price_still_searches() -> None:
    """ "my portfolio is stressing me out, how's OP doing" — heavy, and a real price question."""
    prompt = _prompt(
        "my portfolio is stressing me out, how's OP doing", needs_live_info=True, emotion=STRESS
    )
    assert "web_search" in {t.id for t in offered_tools(prompt, ALL_TOOLS)}


@pytest.mark.parametrize("emotion", [None, JOY], ids=["neutral", "happy"])
def test_an_unheavy_turn_keeps_the_backstop(emotion: dict[str, Any] | None) -> None:
    """On a turn carrying no emotional weight a needless search really does only cost a second,
    so the deterministic backstop keeps its vote — that is what catches the questions the LLM
    classifier misses (its recall is 0.954, the composed gate's is 0.989)."""
    prompt = _prompt("what's the price of SYPNL?", needs_live_info=False, emotion=emotion)
    assert not _is_emotionally_heavy(prompt)
    assert _requires_live_lookup(prompt), "the backstop lost its vote on a non-emotional turn"


def test_an_unknown_verdict_never_suppresses_the_search() -> None:
    """`None` means the classifier produced nothing usable. It is not a `False`, and it must
    never be read as one — that swallowing is the original S1 defect."""
    prompt = _prompt("what's the price of SYPNL?", needs_live_info=None, emotion=GRIEF)
    assert _requires_live_lookup(prompt)
