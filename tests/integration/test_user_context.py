"""Integration tests for real auth — accounts, sessions, outbox (design §18/§26)
against real MongoDB. Google's consent screen is the only un-automatable step;
everything the callback does afterwards is exercised here with a real userinfo.
"""

import uuid
from pathlib import Path

import pytest

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.outbox import OutboxStore, WelcomeMailer
from adapters.outbox.store import OUTBOX_COLLECTION
from adapters.user_context.accounts import USERS_COLLECTION, AccountStore, GoogleIdentity
from adapters.user_context.session import SessionUserContext
from config.settings import Settings
from core.profile import ProfileService
from workers.outbox_worker import OutboxWorker

pytestmark = pytest.mark.integration

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults"


def _identity() -> GoogleIdentity:
    sub = "sub-" + uuid.uuid4().hex[:12]
    return GoogleIdentity(sub=sub, email=f"{sub}@example.com", name="Test User")


@pytest.fixture
def accounts(db: Database) -> tuple[AccountStore, OutboxStore, MongoDocStore]:
    docs = MongoDocStore(db)
    outbox = OutboxStore(docs)
    return AccountStore(docs, ProfileService(docs), outbox=outbox), outbox, docs


async def test_signup_creates_user_profile_and_welcome_outbox(
    accounts: tuple[AccountStore, OutboxStore, MongoDocStore],
) -> None:
    store, _outbox, docs = accounts
    identity = _identity()

    result = await store.upsert_from_google(identity)
    assert result.created is True  # first sign-in == sign-up

    user = await docs.get(USERS_COLLECTION, result.account.user_id)
    assert user is not None
    assert user["google_sub"] == identity.sub  # sub mapped, not the internal key
    assert result.account.user_id.startswith("u_")  # OUR internal id is the key

    # §2 first-run profile seeded
    profile = await docs.get("user_profile", result.account.user_id)
    assert profile is not None

    # welcome email queued in the SAME operation (brief §4), pending — signup
    # never blocks on SMTP.
    pending = [
        o
        for o in await docs.find(OUTBOX_COLLECTION, {"status": "pending"}, limit=200)
        if o.get("payload", {}).get("user_id") == result.account.user_id
    ]
    assert len(pending) == 1 and pending[0]["type"] == "welcome_email"


async def test_second_login_signs_in_without_duplicate(
    accounts: tuple[AccountStore, OutboxStore, MongoDocStore],
) -> None:
    store, _outbox, docs = accounts
    identity = _identity()

    first = await store.upsert_from_google(identity)
    second = await store.upsert_from_google(identity)

    assert second.created is False
    assert second.account.user_id == first.account.user_id  # same user, signed in
    same_sub = await docs.find(USERS_COLLECTION, {"google_sub": identity.sub})
    assert len(same_sub) == 1  # no duplicate account


async def test_outbox_worker_delivers_or_skips(
    accounts: tuple[AccountStore, OutboxStore, MongoDocStore],
) -> None:
    store, outbox, docs = accounts
    result = await store.upsert_from_google(_identity())

    # Mailer disabled (no SMTP in the test env) → the record is marked skipped,
    # never left looping; signup already succeeded.
    mailer = WelcomeMailer(Settings(_env_file=None))
    handled = await OutboxWorker(outbox, mailer).drain_once()
    assert handled >= 1

    mine = [
        o
        for o in await docs.find(OUTBOX_COLLECTION, {}, limit=500)
        if o.get("payload", {}).get("user_id") == result.account.user_id
    ]
    assert mine[0]["status"] in ("sent", "skipped")  # resolved, not stuck pending


async def test_outbox_worker_sends_when_mail_is_configured(
    accounts: tuple[AccountStore, OutboxStore, MongoDocStore],
) -> None:
    """F16: the root cause of 'I got no signup email' is that mail is UNCONFIGURED
    (mailer disabled → records skipped). This proves the pipeline actually DELIVERS
    once credentials are set: with an enabled mailer, the worker calls send_welcome
    for the new user and marks the record 'sent' (FastMail stubbed — no real SMTP)."""
    store, outbox, docs = accounts
    result = await store.upsert_from_google(_identity())

    mailer = WelcomeMailer(Settings(_env_file=None))
    mailer._enabled = True  # simulate configured SMTP
    sent: list[tuple[str, str | None]] = []

    async def _fake_send(email: str, name: str | None) -> None:
        sent.append((email, name))

    mailer.send_welcome = _fake_send  # type: ignore[method-assign]

    handled = await OutboxWorker(outbox, mailer).drain_once()
    assert handled >= 1
    assert any(e == result.account.email for e, _ in sent), f"welcome not sent: {sent}"

    mine = [
        o
        for o in await docs.find(OUTBOX_COLLECTION, {}, limit=500)
        if o.get("payload", {}).get("user_id") == result.account.user_id
    ]
    assert mine and mine[0]["status"] == "sent", f"record not marked sent: {mine}"


def test_mailer_reports_disabled_without_credentials() -> None:
    """F16: an unconfigured mailer is disabled (the cause of the missing email)."""
    assert WelcomeMailer(Settings(_env_file=None)).enabled is False


async def test_session_context_isolates_users(
    accounts: tuple[AccountStore, OutboxStore, MongoDocStore],
) -> None:
    store, _outbox, docs = accounts
    ctx = SessionUserContext(ProfileService(docs))

    a = await store.upsert_from_google(_identity())
    b = await store.upsert_from_google(_identity())
    assert a.account.user_id != b.account.user_id

    profiles = ProfileService(docs)
    await profiles.update(a.account.user_id, {"companion_name": "OnlyForA"})

    record_b = await ctx.record_for(b.account.user_id)
    assert record_b.companion_name is None  # A's write never leaks into B
