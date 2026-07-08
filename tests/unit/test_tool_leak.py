"""Unit tests for the tool-syntax leak scrubber (user report: 'web_search::' spoken)."""

from core.reasoning.style import strip_tool_leak


def test_strips_tool_id_with_double_colon() -> None:
    out = strip_tool_leak("Sure, let me web_search:: latest NEPSE news for you")
    assert "web_search" not in out
    assert "Sure, let me" in out


def test_strips_bare_tool_mentions() -> None:
    assert "tool_request" not in strip_tool_leak("Okay tool_request here")
    assert "functions.web_search" not in strip_tool_leak("calling functions.web_search now")


def test_strips_stray_tool_json() -> None:
    out = strip_tool_leak('Let me check {"tool_id": "web_search", "args": {"q": "x"}} okay')
    assert "tool_id" not in out and "web_search" not in out


def test_leaves_normal_reply_untouched() -> None:
    reply = "It's around 22 degrees in Kathmandu right now, with some thunderstorms."
    assert strip_tool_leak(reply) == reply


def test_leaves_the_word_search_in_normal_prose() -> None:
    # "search" as an ordinary English word must survive — only the tool tokens go.
    reply = "You could search your feelings on that one, honestly."
    assert strip_tool_leak(reply) == reply
