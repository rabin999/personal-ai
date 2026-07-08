"""Unit tests for the bundled prompt provider + defaults (F13).

The bundled provider is the safe fallback served when Langfuse is unreachable — it
must return the real prompt text and compile {{variables}} exactly like Langfuse's
mustache templating, so a fallback turn behaves identically to a served one.
"""

from adapters.prompt.defaults import BUNDLED_PROMPTS
from adapters.prompt.langfuse_prompt import BundledPromptProvider


def test_bundled_provider_returns_prompt_and_marks_fallback() -> None:
    p = BundledPromptProvider()
    r = p.get("context_intent")
    assert r.source == "fallback"
    assert r.name == "context_intent"
    assert "INTENT" in r.text and len(r.text) > 200


def test_bundled_provider_compiles_variables() -> None:
    p = BundledPromptProvider()
    r = p.get("self_reflection_rewrite", variables={"draft": "I'd be happy to help!"})
    assert "{{draft}}" not in r.text  # variable substituted
    assert "I'd be happy to help!" in r.text


def test_unknown_prompt_is_empty_not_an_error() -> None:
    r = BundledPromptProvider().get("does_not_exist")
    assert r.text == "" and r.source == "fallback"


def test_all_bundled_prompts_are_nonempty() -> None:
    assert BUNDLED_PROMPTS
    for name, text in BUNDLED_PROMPTS.items():
        assert text.strip(), f"bundled prompt {name} is empty"
