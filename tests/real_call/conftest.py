"""Fixtures for the real-call suite (plan §3): a live pipeline built ONCE per
module against the real model + real stores. Skipped loudly (not silently) when
the API key or datastores are unavailable, so a missing prerequisite is obvious.
"""

import os

import pytest
import pytest_asyncio

from core.errors import PROGRAMMING_ERRORS
from core.reasoning.orchestrator import OrchestratorContractError
from tests.support.real_pipeline import RealTurns

pytestmark = pytest.mark.real_call


def _missing_prereqs() -> str | None:
    if not os.getenv("OPEN_ROUTER_API_KEY"):
        return "OPEN_ROUTER_API_KEY not set (real model call)"
    return None


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def real_turns():
    reason = _missing_prereqs()
    if reason:
        pytest.skip(f"real_call skipped: {reason}")
    try:
        turns = await RealTurns.build()
    except OrchestratorContractError:
        # F3/F4: a mis-wired engine is a BUG, not a missing prerequisite. Skipping here
        # would reproduce the exact failure this suite exists to catch.
        raise
    except PROGRAMMING_ERRORS:
        raise  # a defect in our own wiring must fail the suite, never skip it
    except Exception as exc:  # docker stores down, model verify failed, etc.
        pytest.skip(f"real_call skipped: could not build pipeline ({type(exc).__name__}: {exc})")
    yield turns
    await turns.aclose()
