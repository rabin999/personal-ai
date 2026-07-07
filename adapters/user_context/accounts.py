"""Account store — real user records mapped from Google identity (design §18/§26).

Replaces the static token map. The ``users`` collection holds
``{_id: user_id, google_sub, email, name, picture, created_at, last_login_at}``.
OUR internal ``user_id`` stays the stable key everywhere (memory/cost/projects
are user_id-scoped); Google's ``sub`` is only a lookup mapping — we never switch
the internal key to it.

Sign-in and sign-up are the same flow: on the OAuth callback we look up by
``google_sub``; found → sign in; not found → create the account (sign-up), seed
the §2 first-run profile, and queue the welcome email in the SAME operation
(transactional outbox, brief §4) so signup never blocks on SMTP.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from adapters.outbox import OutboxStore
from core.profile import ProfileService
from ports.doc_store import DocStore

USERS_COLLECTION = "users"


class GoogleIdentity(BaseModel):
    """The subset of Google's OIDC userinfo we persist."""

    sub: str
    email: str
    name: str | None = None
    picture: str | None = None


class Account(BaseModel):
    user_id: str
    google_sub: str
    email: str
    name: str | None = None
    picture: str | None = None
    created_at: str
    last_login_at: str


class AuthResult(BaseModel):
    account: Account
    created: bool  # True = new sign-up (welcome email queued)


class AccountStore:
    def __init__(
        self,
        docs: DocStore,
        profiles: ProfileService,
        outbox: OutboxStore | None = None,
    ) -> None:
        self._docs = docs
        self._profiles = profiles
        self._outbox = outbox

    async def upsert_from_google(self, identity: GoogleIdentity) -> AuthResult:
        """Sign in an existing Google account or create a new one (sign-up)."""
        now = datetime.now(UTC).isoformat()
        existing = await self._find_by_sub(identity.sub)
        if existing is not None:
            # Sign IN: refresh profile fields + last login; never duplicate, never
            # re-send the welcome email.
            existing.update(
                {
                    "email": identity.email or existing.get("email", ""),
                    "name": identity.name or existing.get("name"),
                    "picture": identity.picture or existing.get("picture"),
                    "last_login_at": now,
                }
            )
            await self._docs.put(USERS_COLLECTION, existing["_id"], existing)
            return AuthResult(account=_account(existing), created=False)

        # Sign UP: mint OUR internal user_id (the stable multi-tenant key).
        user_id = "u_" + uuid.uuid4().hex[:16]
        doc = {
            "_id": user_id,
            "google_sub": identity.sub,
            "email": identity.email,
            "name": identity.name,
            "picture": identity.picture,
            "created_at": now,
            "last_login_at": now,
        }
        await self._docs.put(USERS_COLLECTION, user_id, doc)
        # Seed the companion profile (§2 first-run sync) so a new user gets full
        # traits / model prefs / audio prefs from defaults.
        await self._profiles.first_run_sync(user_id)
        # Queue the welcome email in the SAME operation (brief §4) — best-effort,
        # so a queue hiccup never fails signup.
        if self._outbox is not None:
            # signup must not fail on the outbox write
            with contextlib.suppress(Exception):
                await self._outbox.enqueue_welcome(user_id, identity.email, identity.name)
        return AuthResult(account=_account(doc), created=True)

    async def get(self, user_id: str) -> Account | None:
        doc = await self._docs.get(USERS_COLLECTION, user_id)
        return _account(doc) if doc else None

    async def _find_by_sub(self, sub: str) -> dict[str, Any] | None:
        rows = await self._docs.find(USERS_COLLECTION, {"google_sub": sub}, limit=1)
        return rows[0] if rows else None


def _account(doc: dict[str, Any]) -> Account:
    fields = {k: v for k, v in doc.items() if k != "_id"}
    return Account.model_validate({"user_id": doc["_id"], **fields})
