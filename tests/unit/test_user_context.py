"""Unit tests for real auth — AccountStore + SessionUserContext (spec §26).

Profile/doc store faked in memory; the Google userinfo is supplied directly
(the callback's real logic — create/lookup/session shape — without the browser).
"""

import pytest

from adapters.outbox import OutboxStore
from adapters.user_context.accounts import USERS_COLLECTION, AccountStore, GoogleIdentity
from adapters.user_context.session import SessionUserContext
from core.profile import ProfileService
from tests.fakes import FakeDocStore


@pytest.fixture
def docs() -> FakeDocStore:
    return FakeDocStore()


@pytest.fixture
def store(docs: FakeDocStore) -> AccountStore:
    return AccountStore(docs, ProfileService(docs), outbox=OutboxStore(docs))


def _identity(sub: str = "sub-123") -> GoogleIdentity:
    return GoogleIdentity(sub=sub, email=f"{sub}@example.com", name="Ada Lovelace")


async def test_first_google_login_creates_account_and_seeds_profile(
    store: AccountStore, docs: FakeDocStore
) -> None:
    result = await store.upsert_from_google(_identity())
    assert result.created is True
    assert result.account.user_id.startswith("u_")  # OUR internal id, not google sub
    assert result.account.google_sub == "sub-123"  # sub is only a mapping
    assert await docs.get("user_profile", result.account.user_id) is not None  # §2 seed


async def test_welcome_email_queued_on_signup(store: AccountStore, docs: FakeDocStore) -> None:
    result = await store.upsert_from_google(_identity())
    outbox = await docs.find("outbox", {"status": "pending"})
    mine = [o for o in outbox if o["payload"]["user_id"] == result.account.user_id]
    assert len(mine) == 1 and mine[0]["type"] == "welcome_email"  # brief §4


async def test_returning_login_signs_in_without_duplicate(
    store: AccountStore, docs: FakeDocStore
) -> None:
    first = await store.upsert_from_google(_identity())
    second = await store.upsert_from_google(_identity())
    assert second.created is False
    assert second.account.user_id == first.account.user_id
    assert len(await docs.find(USERS_COLLECTION, {"google_sub": "sub-123"})) == 1


async def test_session_context_builds_record_for_authenticated_user(
    store: AccountStore, docs: FakeDocStore
) -> None:
    result = await store.upsert_from_google(_identity())
    ctx = SessionUserContext(ProfileService(docs))
    record = await ctx.record_for(result.account.user_id)
    assert record.user_id == result.account.user_id


async def test_two_google_users_get_isolated_ids(store: AccountStore) -> None:
    a = await store.upsert_from_google(_identity("sub-aaa"))
    b = await store.upsert_from_google(_identity("sub-bbb"))
    assert a.account.user_id != b.account.user_id  # distinct internal keys
