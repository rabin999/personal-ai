"""Unit tests for prompt caching (L6): stable-prefix + Anthropic cache_control."""

from pathlib import Path

from adapters.llm.openrouter import _with_cache_control
from core.reasoning.prompt_assembly import _STABLE_SECTIONS
from tests.unit.test_prompt_assembly import Harness

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"
USER = "u_demo_001"
SESSION = "s_test"


def test_cache_prefix_is_a_real_prefix_of_the_system_prompt() -> None:
    """The cache_prefix must be a byte-exact leading substring of the system prompt,
    or the provider can't cache it."""

    async def run() -> None:
        h = Harness()
        await h.seed()
        p = await h.assembler.assemble(USER, SESSION, "hey there")
        assert p.cache_prefix, "a stable prefix should be produced"
        assert p.system_prompt.startswith(p.cache_prefix)
        # The stable prefix holds the identity + traits, not the volatile time block.
        assert "voice-first personal companion" in p.cache_prefix
        assert "## Right now" not in p.cache_prefix  # time is volatile, excluded

    import asyncio

    asyncio.run(run())


def test_cache_prefix_is_stable_across_turns() -> None:
    """Same user, different utterances → identical cache_prefix (so it caches)."""

    async def run() -> None:
        h = Harness()
        await h.seed()
        a = await h.assembler.assemble(USER, SESSION, "what's the weather")
        b = await h.assembler.assemble(USER, SESSION, "tell me a joke")
        assert a.cache_prefix == b.cache_prefix

    import asyncio

    asyncio.run(run())


def test_anthropic_gets_cache_control_markers() -> None:
    prefix = "You are a companion. Stable stuff."
    messages = [
        {"role": "system", "content": prefix + " VOLATILE per-turn memory."},
        {"role": "user", "content": "hi"},
    ]
    out = _with_cache_control(messages, prefix, "anthropic/claude-sonnet-4.5")
    blocks = out[0]["content"]
    assert isinstance(blocks, list)
    assert blocks[0]["text"] == prefix
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == " VOLATILE per-turn memory."


def test_non_anthropic_is_left_as_plain_string() -> None:
    """Gemini/OpenAI cache implicitly; we don't restructure their messages."""
    prefix = "stable"
    messages = [{"role": "system", "content": prefix + " rest"}, {"role": "user", "content": "hi"}]
    out = _with_cache_control(messages, prefix, "google/gemini-2.5-flash")
    assert out[0]["content"] == prefix + " rest"  # unchanged (implicit caching)


def test_prefix_mismatch_falls_back_safely() -> None:
    """If the prefix isn't actually the leading text, leave messages untouched."""
    messages = [
        {"role": "system", "content": "totally different"},
        {"role": "user", "content": "x"},
    ]
    out = _with_cache_control(messages, "not-a-prefix", "anthropic/claude-sonnet-4.5")
    assert out[0]["content"] == "totally different"


def test_stable_sections_are_first_in_render_order() -> None:
    from core.reasoning.prompt_assembly import _SECTION_TITLES

    keys = list(_SECTION_TITLES.keys())
    assert keys[: len(_STABLE_SECTIONS)] == list(_STABLE_SECTIONS)
