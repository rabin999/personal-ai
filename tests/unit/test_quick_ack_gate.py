"""The quick interjection must fire on ordinary conversational turns (the streamable path buffers
the whole reply before any audio, so without it the user waits in silence — reported: "quick
sentences not often triggering"), but NOT on short social pleasantries, and it must pick a
RECEIVING backchannel for a statement rather than "let me think" (a question ack)."""

from core.profile import ProfileService, TraitRegistry
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import ResponseGenerator, _is_trivial_turn, _wants_quick_ack
from core.reasoning.self_model import SelfModel
from tests.fakes import FakeDocStore, FakeLLM, FakeVectorStore

USER = "u_demo_001"


def _p(utterance: str, recall: str = "none") -> AssembledPrompt:
    return AssembledPrompt(
        user_id=USER,
        session_id="s1",
        utterance=utterance,
        system_prompt="c",
        messages=[{"role": "user", "content": utterance}],
        complexity_hint="simple",
        recall_source=recall,
    )


def test_fires_on_ordinary_conversational_turns() -> None:
    for utt in [
        "just been busy with work lately, lots of meetings",
        "I finally set up my new home office and it's so much better",
        "what causes the northern lights exactly?",
        "tell me about the history of Kathmandu",
    ]:
        assert _wants_quick_ack(_p(utt)), utt


def test_suppressed_on_short_social_pleasantries() -> None:
    for utt in ["hi", "hey there", "thanks!", "how are you?", "yeah", "cool", "I'm good"]:
        assert _is_trivial_turn(utt), utt
        assert not _wants_quick_ack(_p(utt)), utt


def test_recall_always_fires() -> None:
    assert _wants_quick_ack(_p("what did we talk about?", recall="past"))


async def _ack_for(utterance: str) -> str:
    docs = FakeDocStore()
    llm = FakeLLM([])
    gen = ResponseGenerator(
        llm, SelfModel(docs, FakeVectorStore(), llm), TraitRegistry(docs, ProfileService(docs))
    )
    spoken: list[str] = []

    async def speak(t: str) -> None:
        if t.strip():
            spoken.append(t.strip())

    await gen._dynamic_ack(_p(utterance), speak, is_lookup=False)
    return spoken[0]


async def test_statement_gets_a_backchannel_not_let_me_think() -> None:
    from core.phrases.defaults import ACK_BACKCHANNEL, ACK_THINKING

    line = await _ack_for("just been busy with work lately")
    assert line in ACK_BACKCHANNEL, line
    assert line not in ACK_THINKING


async def test_question_gets_a_thinking_ack() -> None:
    from core.phrases.defaults import ACK_BACKCHANNEL, ACK_THINKING

    line = await _ack_for("what causes the northern lights exactly?")
    assert line in ACK_THINKING, line
    assert line not in ACK_BACKCHANNEL
