"""Thin e2e for §3: cost attribution through the assembled user-scoped path.

A resolved user (token → §26 → user_id) incurs an LLM cost; the ledger
records it under that user and the other demo user's view stays empty —
per-user cost attribution works end to end before any AI logic exists.
"""

from pathlib import Path

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.user_context.accounts import AccountStore, GoogleIdentity
from config.settings import Settings
from core.cost import CostEntry, CostLedger, CostMetadata
from core.profile import ProfileService
from tests.integration.conftest import wait_until_healthy

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


async def test_resolved_user_cost_attribution_end_to_end() -> None:
    database = Database(Settings(_env_file=None))
    try:
        await wait_until_healthy(database)
        store = MongoDocStore(database)
        accounts = AccountStore(store, ProfileService(store))
        ledger = CostLedger(store)

        # Two real Google sign-ups → two isolated internal user_ids (§26).
        speaker = (
            await accounts.upsert_from_google(GoogleIdentity(sub="cost-a", email="a@x.io"))
        ).account
        bystander = (
            await accounts.upsert_from_google(GoogleIdentity(sub="cost-b", email="b@x.io"))
        ).account

        ledger.log(
            CostEntry(
                user_id=speaker.user_id,  # from resolved context, never hard-coded
                component="llm",
                provider="openrouter",
                units={"input_tokens": 1200, "output_tokens": 300},
                cost_usd=0.0061,
                metadata=CostMetadata(session_id="s_e2e_001"),
            )
        )
        await ledger.flush()

        speaker_summary = await ledger.get(speaker.user_id, session_id="s_e2e_001")
        assert speaker_summary.count == 1
        assert speaker_summary.total_usd == pytest.approx(0.0061)

        bystander_summary = await ledger.get(bystander.user_id, session_id="s_e2e_001")
        assert bystander_summary.count == 0
    finally:
        await database.mongo("cost_ledger").delete_many({"metadata.session_id": "s_e2e_001"})
        await database.aclose()
