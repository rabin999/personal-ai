"""Voice-effect demos + whole-reply effect overrides (design §10.2).

Two capabilities the mood-driven reply path could not serve:
1. "show me your voice effects" / "give me 5 tone examples" → a spoken demo whose
   example sentences carry REAL delivery tags, exactly N of them, that survive the
   tag sanitizer to TTS (the normal path would strip [laugh] on a neutral register).
2. "answer in a whisper" → the WHOLE reply wrapped in that effect for TTS.

Detection must be precise: ordinary turns ("what's the weather", "I whispered to
her") must NOT trip either path.
"""

from typing import Any, cast

import pytest

from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import (
    GenerationResult,
    ResponseGenerator,
    _sanitize_tags,
)
from core.reasoning.self_model import SelfModel
from core.reasoning.voice_effects import (
    DEFAULT_DEMO_COUNT,
    EFFECT_CATALOG,
    MAX_DEMO_COUNT,
    apply_effect_override,
    build_demo,
    detect_demo_request,
    detect_effect_override,
)
from tests.fakes import FakeDocStore, FakeLLM, FakeVectorStore

USER = "u_demo_001"


# --------------------------------------------------------------------------- demo detection
@pytest.mark.parametrize(
    "utterance",
    [
        "can you give me a few tone examples?",
        "how many voice effects do you support?",
        "produce me some examples of different voice effects",
        "show me your voice effects",
        "what kind of tones can you do?",
        "demonstrate the different vibes you can do",
        "give me 5 voice effects with sentences",
        "how much voice effects can you produce",
        "list the delivery effects you support",
    ],
)
def test_demo_request_detected(utterance: str) -> None:
    assert detect_demo_request(utterance) is not None


@pytest.mark.parametrize(
    "utterance",
    [
        "what's the weather today?",
        "I love the sound of your voice",
        "tell me a joke",
        "how are you doing?",
        "I whispered to her that it was fine",
        "what's my portfolio worth?",
        "",
        "remind me to call mom",
    ],
)
def test_demo_request_not_detected(utterance: str) -> None:
    assert detect_demo_request(utterance) is None


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("give me 5 voice effect examples", 5),
        ("show me three tone examples", 3),
        ("give me a couple of voice examples", 2),
        ("can you give me a few tone examples?", DEFAULT_DEMO_COUNT),
        ("show me all your voice effects", MAX_DEMO_COUNT),
        ("give me 99 voice effects", MAX_DEMO_COUNT),  # clamped
    ],
)
def test_demo_count_extraction(utterance: str, expected: int) -> None:
    req = detect_demo_request(utterance)
    assert req is not None
    assert req.count == expected


# --------------------------------------------------------------------------- demo build
def test_build_demo_produces_exact_count_and_tagged_examples() -> None:
    display, voice = build_demo(5)
    # Exactly 5 numbered lines in the display list (+ the intro line).
    numbered = [ln for ln in display.splitlines() if ln[:2] in {f"{i}." for i in range(1, 10)}]
    assert len(numbered) == 5
    # The spoken form carries whitelisted delivery tags that SURVIVE the sanitizer —
    # the whole point: a demo must actually perform the effects.
    assert "<whisper>" in voice and "</whisper>" in voice
    assert "[laugh]" in voice
    assert _sanitize_tags(voice) == voice, "every demo tag must survive _sanitize_tags to TTS"


def test_build_demo_clamps_and_defaults() -> None:
    _d, voice_one = build_demo(1)
    assert "<whisper>" in voice_one  # first catalogue effect
    _d2, voice_big = build_demo(999)
    # Clamped to the catalogue size — never asks for more than exist.
    assert voice_big.count("Here's") == MAX_DEMO_COUNT


def test_demo_display_is_tag_free() -> None:
    display, _voice = build_demo(4)
    assert "<" not in display and "[" not in display


def test_catalog_covers_both_wrapping_and_instant() -> None:
    """The capability must span BOTH Grok effect kinds (wrapping <…> and instant […])."""
    cats = {e.category for e in EFFECT_CATALOG}
    assert cats == {"wrapping", "instant"}
    assert sum(e.category == "wrapping" for e in EFFECT_CATALOG) >= 5
    assert sum(e.category == "instant" for e in EFFECT_CATALOG) >= 5


def test_small_demo_mixes_both_kinds() -> None:
    """A 4-effect demo interleaves wrapping + instant, not four of one kind."""
    _d, voice = build_demo(4)
    assert "<whisper>" in voice  # a wrapping effect
    assert "[laugh]" in voice  # an instant effect


def test_coverage_question_shows_all() -> None:
    for q in ("how many voice effects do you support?", "what voice effects do you have?"):
        req = detect_demo_request(q)
        assert req is not None and req.count == MAX_DEMO_COUNT


@pytest.mark.parametrize("key", ["soft", "slow", "whisper", "emphasis"])
def test_wrapping_effects_apply(key: str) -> None:
    out = apply_effect_override("Here we go.", key)
    assert out == f"<{key}>Here we go.</{key}>"
    assert _sanitize_tags(out) == out


def test_no_fabricated_tags_advertised() -> None:
    """xAI Grok has no singing tag and <loud> is undocumented — we must never offer them
    (design §1.6: never fake a capability the voice can't perform)."""
    keys = {e.key for e in EFFECT_CATALOG}
    assert "sing" not in keys and "loud" not in keys
    from core.reasoning.response_gen import _ALLOWED_TAGS

    assert "sing" not in _ALLOWED_TAGS and "loud" not in _ALLOWED_TAGS


# --------------------------------------------------------------------------- override detection
@pytest.mark.parametrize(
    ("utterance", "key"),
    [
        ("answer in a whisper", "whisper"),
        ("can you whisper this?", "whisper"),
        ("say that slowly", "slow"),
        ("reply to my next question slowly", "slow"),
        ("respond excitedly please", "fast"),
        ("say it gently", "gentle"),
        ("can you answer softly", "soft"),
        ("say the next response with emphasis", "emphasis"),
    ],
)
def test_override_detected(utterance: str, key: str) -> None:
    assert detect_effect_override(utterance) == key


@pytest.mark.parametrize(
    "utterance",
    [
        "I whispered to her that it was fine",
        "the room was really quiet",
        "he spoke slowly and I got nervous",  # third-person narration, no request cue
        "what's the weather?",
        "she said it was a soft blanket",
        "",
    ],
)
def test_override_not_detected(utterance: str) -> None:
    assert detect_effect_override(utterance) is None


# --------------------------------------------------------------------------- override apply
def test_apply_wrap_effect() -> None:
    out = apply_effect_override("Sure, here you go.", "whisper")
    assert out == "<whisper>Sure, here you go.</whisper>"
    assert _sanitize_tags(out) == out


def test_apply_lead_effect() -> None:
    out = apply_effect_override("Okay, listen.", "laugh")
    assert out == "[laugh] Okay, listen."
    assert _sanitize_tags(out) == out


def test_apply_is_idempotent_and_safe() -> None:
    once = apply_effect_override("Hi there.", "whisper")
    twice = apply_effect_override(once, "whisper")
    assert once == twice  # never double-wraps
    assert apply_effect_override("", "whisper") == ""
    assert apply_effect_override("hello", "unknown_effect") == "hello"


def test_every_catalog_effect_applies_and_survives_sanitize() -> None:
    for e in EFFECT_CATALOG:
        out = apply_effect_override("The whole reply here.", e.key)
        assert out != "The whole reply here.", f"{e.key} produced no tag"
        assert _sanitize_tags(out) == out, f"{e.key} tag did not survive sanitize"


# --------------------------------------------------------------------------- engine integration
def _gen() -> ResponseGenerator:
    docs = FakeDocStore()
    llm = FakeLLM([])
    registry = TraitRegistry(docs, ProfileService(docs))
    self_model = SelfModel(docs, FakeVectorStore(), llm)
    return ResponseGenerator(llm, self_model, registry, progress_filler_gap_s=0.01)


def _prompt(utterance: str) -> AssembledPrompt:
    return AssembledPrompt(
        user_id=USER,
        session_id="s1",
        utterance=utterance,
        system_prompt="You are Companion.",
        messages=[{"role": "user", "content": utterance}],
        complexity_hint="simple",
    )


class _SpeakSpy:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def speak(self, text: str) -> None:
        if text.strip():
            self.spoken.append(text)


async def test_generate_spoken_demo_speaks_tagged_effects() -> None:
    """A demo request is answered deterministically (no LLM) and SPEAKS the tagged
    examples — [laugh] survives even though the turn's register is neutral."""
    gen = _gen()
    spy = _SpeakSpy()
    result = await gen.generate_spoken(
        _prompt("give me 4 voice effect examples"),
        cast(Any, object()),
        cast(Any, object()),
        spy.speak,
    )
    spoken = " ".join(spy.spoken)
    assert "<whisper>" in spoken and "[laugh]" in spoken
    # The chat/memory text is the clean labelled list, tag-free (brief §1.4).
    assert "<" not in result.final_text and "[" not in result.final_text
    assert result.final_text.count("\n") >= 4  # intro + 4 numbered lines


async def test_generate_spoken_whisper_override_wraps_whole_reply() -> None:
    """'answer in a whisper' → the entire generated reply is wrapped in <whisper>."""
    gen = _gen()
    spy = _SpeakSpy()

    async def _fake_generate(*_a: object, **_k: object) -> GenerationResult:
        return GenerationResult(
            final_text="Of course, it's a lovely evening.",
            voice_text="Of course, it's a lovely evening.",
            action="respond",
        )

    gen.generate = _fake_generate  # type: ignore[method-assign]

    await gen.generate_spoken(
        _prompt("answer my question in a whisper: how's the evening?"),
        cast(Any, object()),
        cast(Any, object()),
        spy.speak,
    )
    spoken = " ".join(spy.spoken)
    assert spoken.count("<whisper>") == 1 and spoken.count("</whisper>") == 1
    assert spoken.startswith("<whisper>") and spoken.endswith("</whisper>")


async def test_proactive_turn_skips_demo_and_override() -> None:
    """A companion-initiated turn never triggers a demo/override, even if the seeded
    directive text mentions effects — it speaks its one line normally."""
    gen = _gen()
    spy = _SpeakSpy()

    async def _fake_generate(*_a: object, **_k: object) -> GenerationResult:
        return GenerationResult(
            final_text="You still there?", voice_text="You still there?", action="respond"
        )

    async def _no_stream(*_a: object, **_k: object) -> None:
        return None  # force the fall-through to the (stubbed) generate() path

    gen.generate = _fake_generate  # type: ignore[method-assign]
    gen._stream_reply = _no_stream  # type: ignore[method-assign]
    result = await gen.generate_spoken(
        _prompt("give me a few voice effect examples"),
        cast(Any, object()),
        cast(Any, object()),
        spy.speak,
        proactive=True,
    )
    assert result.final_text == "You still there?"
    assert "<whisper>" not in " ".join(spy.spoken)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
