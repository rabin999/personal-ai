"""Unit tests for conversation-recall routing (F3/F4): does the classifier send
'what did I say before that' to the current transcript and 'what did we talk about
yesterday' to the past-conversation store — and NOT treat statements as recall?"""

from core.memory.working import Turn
from core.reasoning.recall import (
    classify_recall,
    render_current_transcript,
)


class TestClassifyRecall:
    def test_current_conversation_questions(self) -> None:
        for q in [
            "what did I just say?",
            "what did I say 2 messages ago?",
            "what did I say before that?",
            "before that?",
            "what were we just talking about?",
            "what did we discuss at the start of this chat?",
            "remind me what I said earlier",
        ]:
            assert classify_recall(q) == "current", q

    def test_past_conversation_questions(self) -> None:
        for q in [
            "what did we talk about yesterday?",
            "what did we discuss last time?",
            "did I mention my trip in a previous chat?",
            "what were we talking about the other day?",
            "what did I tell you last week?",
        ]:
            assert classify_recall(q) == "past", q

    def test_statements_are_not_recall(self) -> None:
        # A past-time word in a STATEMENT (not a recall question) stays none.
        for q in [
            "I felt awful yesterday",
            "let's talk about my trip",
            "tell me a joke",
            "what's the weather like?",
            "I want to discuss my finances",
            "how are you today?",
        ]:
            assert classify_recall(q) == "none", q

    def test_past_cue_beats_current_cue(self) -> None:
        # "we were talking" (current-ish) but "last week" (explicit past) → past.
        assert classify_recall("what were we talking about last week?") == "past"


class TestRenderCurrentTranscript:
    def test_numbered_ordered_and_authoritative(self) -> None:
        turns = [
            Turn(role="user", text="my dog's name is Mango"),
            Turn(role="assistant", text="Mango is a lovely name!"),
            Turn(role="user", text="I'm learning the guitar"),
            Turn(role="assistant", text="what song first?"),
        ]
        out = render_current_transcript(turns, companion_name="Trishul")
        assert "authoritative" in out.lower()
        assert "1. user: my dog's name is Mango" in out
        assert "3. user: I'm learning the guitar" in out
        assert "Trishul:" in out  # companion name used, not "assistant"
        # ordering preserved: dog before guitar
        assert out.index("Mango is a lovely") < out.index("I'm learning the guitar")

    def test_elides_when_over_cap(self) -> None:
        turns = [Turn(role="user", text=f"msg {i}") for i in range(60)]
        out = render_current_transcript(turns)
        assert "omitted" in out  # long session is capped with an elision marker
