"""Deterministic guards for the post-task correction fixes (audit brief).

These lock in the corrected contracts so a regression is caught automatically
(brief §11): capability awareness routes live-info queries to search, TTS tags
are stripped from chat but kept for voice, and transient states never become
durable semantic facts.
"""

from core.memory.extraction import _looks_transient
from core.reasoning.response_gen import (
    _is_live_info_query,
    _needs_capability_repair,
    _sanitize_tags,
    _strip_all_tags,
)


class TestTagStripping:
    """Brief §1.4: chat text is fully clean; voice text keeps real delivery tags."""

    def test_strip_all_removes_every_delivery_tag(self) -> None:
        tagged = "Oh [warm] that's great — <emphasis>congrats</emphasis>! <pause> [sigh]"
        clean = _strip_all_tags(tagged)
        for tag in ("[warm]", "<emphasis>", "</emphasis>", "<pause>", "[sigh]"):
            assert tag not in clean
        assert "that's great" in clean and "congrats" in clean

    def test_sanitize_keeps_whitelisted_drops_stray(self) -> None:
        voice = _sanitize_tags("Sure [tags] — why did the [unknown] cross? [sigh] Ha.")
        assert "[tags]" not in voice and "[unknown]" not in voice  # stray removed
        assert "[sigh]" in voice  # real delivery tag survives for TTS


class TestCapabilityRouting:
    """Brief §8.8: current-world queries route to search; feelings do not."""

    def test_live_info_queries_detected(self) -> None:
        for q in (
            "what is the weather in Kathmandu right now?",
            "give me the top 2 news headlines",
            "what is the current date and time in Nepal?",
            "what's the score of the match",
            "what's happening in Tokyo today",
        ):
            assert _is_live_info_query(q), q

    def test_emotional_statements_do_not_trigger_search(self) -> None:
        for q in (
            "I feel kind of lonely today",
            "I'm really stressed right now",
            "I had the worst day",
            "can you remember that I like tea",
        ):
            assert not _is_live_info_query(q), q

    def test_refusal_and_hollow_promise_flagged(self) -> None:
        for draft in (
            "I don't have access to real-time data.",
            "I'm coming up... I'm drawing a blank on that.",
            "I've never heard of that before.",
            "Just a moment while I get those headlines for you.",
            "Let me check that for you.",
        ):
            assert _needs_capability_repair(draft), draft

    def test_normal_reply_not_flagged(self) -> None:
        assert not _needs_capability_repair("That sounds really hard — want to talk about it?")


class TestTransientClassification:
    """Brief §1.5/§8.13: current states are episodic, never durable facts."""

    def test_transient_states_flagged(self) -> None:
        for fact in (
            "user has a headache right now",
            "user is tired today",
            "user was up late last night",
            "user is stressed at the moment",
        ):
            assert _looks_transient(fact), fact

    def test_durable_facts_not_flagged(self) -> None:
        for fact in (
            "user takes blood-pressure medication daily around 8pm",
            "user goes for a run every morning at 6am",
            "user works at Xenon",
            "user prefers directness",
        ):
            assert not _looks_transient(fact), fact
