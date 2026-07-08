"""Real-store user-local time (brief U5) — REAL Mongo profile + real assembly.

Proves the assembled prompt anchors time-of-day to the USER's local clock (derived
from their city/country), not the server's — the fix for "good morning" at 6pm.
"""

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from core.reasoning.localtime import day_part

pytestmark = [pytest.mark.real_call, pytest.mark.asyncio(loop_scope="module")]


async def test_assembled_prompt_uses_user_local_day_part(real_turns) -> None:
    p = real_turns._p
    user = f"u_tz_{uuid.uuid4().hex[:8]}"
    await p.profiles.first_run_sync(user)  # create the per-user profile
    # Only city/country set (no IANA timezone) — the exact demo shape.
    await p.profiles.update(user, {"locale": {"city": "Kathmandu", "country": "Nepal"}})

    prompt = await p.assembler.assemble(user, "s_tz", "hey")
    # The expected day-part is whatever it currently IS in Nepal, not on the server.
    nepal_now = datetime.now(UTC).astimezone(ZoneInfo("Asia/Kathmandu"))
    expected = day_part(nepal_now)

    assert expected in prompt.system_prompt.lower(), (
        f"prompt should anchor to Nepal's current day-part '{expected}'"
    )
    # And the user-local time is recorded on the trace (proof it was used).
    assert any(s.startswith("localtime=") for s in prompt.user_context_signals)
    assert any("kathmandu" in s.lower() for s in prompt.user_context_signals)
