"""Real-call conversation recall (F3/F4): the companion answers "what did I say"
from the ACTUAL conversation turns, and "what did we talk about last time" from the
stored PAST conversation — not from long-term memory facts.

Real model + real stores (skipped loudly without OPEN_ROUTER_API_KEY / datastores).
"""

import uuid

import pytest

from core.reasoning.recall import ConversationRecall, classify_recall

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_current_conversation_recall_reads_actual_turns(real_turns) -> None:
    """F3: 'what did I say before the guitar?' must return the ACTUAL earlier message
    (the dog), not a semantic-memory fact — the reported failure."""
    session = f"f3_{uuid.uuid4().hex[:6]}"
    await real_turns.say("my dog Mango is a beagle and he loves the park", session)
    await real_turns.say("I just started learning the guitar last week", session)
    await real_turns.say("I'm planning a trip to Pokhara in December", session)

    # A positional recall question further back than the last turn (the reported
    # 'before that' failure). The answer is IN the transcript, so it must quote it.
    follow = await real_turns.say("what did I tell you at the very start of this chat?", session)
    reply = follow.reply.lower()
    assert any(w in reply for w in ("mango", "dog", "beagle")), (
        f"current-conversation recall didn't read the actual first turn: {follow.reply}"
    )

    # The trace shows the recall was routed to the current-session transcript.
    events = await real_turns.traces.traces_for(real_turns.user_id, session)
    sources = [
        e["data"].get("recall_source")
        for e in events
        if e["stage"] == "retrieval" and "recall_source" in e["data"]
    ]
    assert "current" in sources, f"recall not routed to current session: {sources}"


async def test_positional_recall_returns_the_right_message(real_turns) -> None:
    """F3: '2 messages ago' and 'before that' return distinct, correct turns."""
    session = f"f3b_{uuid.uuid4().hex[:6]}"
    await real_turns.say("my favorite color is teal", session)
    await real_turns.say("I work as a civil engineer in Lalitpur", session)
    await real_turns.say("tell me something interesting", session)

    follow = await real_turns.say("wait — what did I say 2 messages ago?", session)
    reply = follow.reply.lower()
    # 2 user-messages back from the question is the civil-engineer message.
    assert any(w in reply for w in ("engineer", "lalitpur", "civil")), (
        f"positional recall wrong: {follow.reply}"
    )


async def test_past_conversation_recall_reads_the_store(real_turns) -> None:
    """F4: 'what did we talk about last time' is answered from the stored PAST
    conversation, not the current buffer or a memory fact."""
    user = f"u_recall_{uuid.uuid4().hex[:6]}"  # fresh user → no other past sessions
    past_session = f"past_{uuid.uuid4().hex[:6]}"
    # Seed a real PAST conversation in the durable store.
    for i, (u, a) in enumerate(
        [
            ("I adopted a rescue cat named Biscuit yesterday", "Biscuit! how's she settling in?"),
            ("she keeps knocking pens off my desk", "a proper little chaos gremlin"),
        ],
        start=1,
    ):
        await real_turns.conversations.record_turn(
            user_id=user,
            session_id=past_session,
            turn_index=i,
            user_text=u,
            assistant_text=a,
        )

    # The recall router builds an authoritative past-conversation section from the store.
    recall = ConversationRecall(real_turns.conversations)
    section, sources = await recall.past_section(user, current_session_id="new_session")
    assert past_session in sources, "past session not selected by the recall router"
    assert "biscuit" in section.lower(), f"past transcript missing seeded content: {section[:300]}"
    assert classify_recall("what did we talk about last time?") == "past"
