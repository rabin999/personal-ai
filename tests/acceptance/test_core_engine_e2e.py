"""Core-engine end-to-end tests (brief Part A): the companion actually works.

These drive the REAL assembled pipeline (real memory stores + real LLM) from the
text boundary inward — the pragmatic workaround for un-testable audio: STT/TTS are
bypassed, everything from prompt assembly → generation → tools → memory → learning
is real. They prove, across WHOLE conversations and SEPARATE sessions:

- cross-session memory recall (teach a fact, recall it in a new session),
- "record X then recall X later" (a trade persists to the portfolio ledger),
- human-like response style (no forbidden assistant-speak) across every turn,
- multi-tenant isolation (user B never sees user A's memories),
- no duplicated replies within a conversation.

Paid + integration: needs docker-compose datastores AND OPEN_ROUTER_API_KEY; both
are skipped loudly when absent. Run: ``uv run pytest -m "paid and acceptance" -q``.
"""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from api.composition import Pipeline, build_pipeline
from config.settings import Settings
from core.memory.working import Turn
from core.reasoning.prompt_assembly import AssembledPrompt, DisambiguationRequest
from core.reasoning.response_gen import GenerationResult
from core.reasoning.style import find_forbidden
from core.tools.registry import ToolContext

# All tests + the module-scoped pipeline fixture share ONE event loop: the pipeline's
# AsyncMongoClient binds to the loop it was built on, so a per-test loop would break it.
pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.paid,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        not os.getenv("OPEN_ROUTER_API_KEY"), reason="core e2e needs OPEN_ROUTER_API_KEY (paid)"
    ),
]

FINANCE_TYPE = "finance_portfolio"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pipeline() -> AsyncIterator[Pipeline]:
    pipe = await build_pipeline(Settings())
    yield pipe
    await pipe.aclose()


class Driver:
    """Runs real turns for one user across sessions and records every reply."""

    def __init__(self, pipe: Pipeline, user_id: str) -> None:
        self._p = pipe
        self.user_id = user_id
        self.replies: list[str] = []
        self.last_write: object = None

    async def say(self, session_id: str, text: str) -> GenerationResult:
        p = self._p
        p.working.append(session_id, Turn(role="user", text=text))
        prompt = await p.assembler.assemble(self.user_id, session_id, text)
        if isinstance(prompt, DisambiguationRequest):
            # Resolve deterministically by re-asserting; not under test here.
            prompt = await p.assembler.assemble(self.user_id, session_id, text)
        context = _ctx(self.user_id, session_id, prompt)
        result = await p.generator.generate(prompt, p.dispatcher, context)
        p.working.append(session_id, Turn(role="assistant", text=result.final_text))
        # WRITE step (§1): the explicit extraction decides what/where to persist.
        # Awaited here so the next session deterministically sees the write.
        self.last_write = await p.extractor.extract_and_store(
            self.user_id, session_id, text, result.final_text
        )
        self.replies.append(result.final_text)
        return result


def _ctx(user_id: str, session_id: str, prompt: object) -> ToolContext:
    project_id = None
    if isinstance(prompt, AssembledPrompt):
        for c in prompt.resolved_entities:
            if c.entity_type == "project":
                project_id = c.entity_id
                break
    return ToolContext(user_id=user_id, session_id=session_id, project_id=project_id)


async def test_records_a_trade_then_recalls_the_portfolio(pipeline: Pipeline) -> None:
    user = f"e2e_trade_{uuid.uuid4().hex[:8]}"
    drv = Driver(pipeline, user)
    s1 = f"{user}_s1"

    await drv.say(s1, "Hey, quick thing — I just bought 12 shares of AAPL at 150 dollars each.")

    # The engine must have persisted the trade to the finance ledger (§16).
    project = await pipeline.projects.find_or_create(user, FINANCE_TYPE, "My portfolio")
    state = await pipeline.projects.state(project.id, user)
    assert state.metrics.get("entry_count", 0) >= 1, "the trade was not recorded to the ledger"
    assert state.metrics.get("net_invested") == pytest.approx(1800.0, rel=0.01)


async def test_extracts_a_routine_then_recalls_it_next_session(pipeline: Pipeline) -> None:
    # brief §4.2-4.3: the medication example. The WRITE step must distill a
    # durable semantic fact, and a NEW session must recall the time.
    user = f"e2e_meds_{uuid.uuid4().hex[:8]}"
    drv = Driver(pipeline, user)

    await drv.say(
        f"{user}_s1", "I take my blood-pressure prescription every day at 8pm, don't let me forget."
    )
    write = drv.last_write
    assert write is not None and write.semantic_written >= 1, (  # type: ignore[attr-defined]
        f"no durable fact was distilled from the routine (wrote: {write})"
    )

    result = await drv.say(f"{user}_s2", "hey, when do I take my medication again?")
    assert "8" in result.final_text, (
        f"routine not recalled in a new session — reply was: {result.final_text!r}"
    )


async def test_recalls_a_fact_in_a_separate_session(pipeline: Pipeline) -> None:
    user = f"e2e_recall_{uuid.uuid4().hex[:8]}"
    drv = Driver(pipeline, user)

    # Session 1: share a distinctive, checkable fact.
    await drv.say(f"{user}_s1", "By the way, my dog's name is Trishul and he's a husky.")

    # Session 2 (brand-new session id): the fact must be recalled from memory.
    result = await drv.say(f"{user}_s2", "what's my dog's name again?")
    assert "trishul" in result.final_text.lower(), (
        f"cross-session recall failed — reply was: {result.final_text!r}"
    )


async def test_never_talks_like_a_service_desk_across_a_conversation(
    pipeline: Pipeline,
) -> None:
    user = f"e2e_style_{uuid.uuid4().hex[:8]}"
    drv = Driver(pipeline, user)
    s = f"{user}_s1"
    for line in ["hi", "hello?", "so, not sure where to start", "anyway how are you"]:
        await drv.say(s, line)
    for reply in drv.replies:
        assert find_forbidden(reply) == [], f"assistant-speak slipped through: {reply!r}"


async def test_memory_is_isolated_between_users(pipeline: Pipeline) -> None:
    a = f"e2e_iso_a_{uuid.uuid4().hex[:8]}"
    b = f"e2e_iso_b_{uuid.uuid4().hex[:8]}"
    await Driver(pipeline, a).say(f"{a}_s1", "Remember: my secret project is called Nightingale.")

    # User B, fresh, must NOT know user A's secret (prompt-bleed / isolation).
    result = await Driver(pipeline, b).say(f"{b}_s1", "what's my secret project called?")
    assert "nightingale" not in result.final_text.lower(), "isolation breach: A's data reached B"
