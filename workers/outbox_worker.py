"""Outbox worker — delivers pending welcome emails reliably (brief §4).

Polls the Mongo ``outbox`` collection off the signup path: pending → send via
fastapi-mail (Gmail SMTP) → ``sent``, or a failed attempt reschedules with
backoff until the attempt cap → ``failed``. If SMTP is unconfigured the record
is marked ``skipped`` (signup still succeeds). Idempotent on the outbox id, so a
briefly-down SMTP never loses or double-sends a message.
"""

import asyncio
import logging

from adapters.outbox import OutboxStore, WelcomeMailer
from adapters.outbox.mailer import is_deliverable
from adapters.outbox.store import WELCOME_EMAIL

logger = logging.getLogger("workers.outbox")

POLL_INTERVAL_S = 15.0


class OutboxWorker:
    def __init__(
        self, outbox: OutboxStore, mailer: WelcomeMailer, poll_interval_s: float = POLL_INTERVAL_S
    ) -> None:
        self._outbox = outbox
        self._mailer = mailer
        self._interval = poll_interval_s

    async def run_forever(self) -> None:
        logger.info(
            "outbox worker started (mail %s)", "enabled" if self._mailer.enabled else "DISABLED"
        )
        while True:
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one bad cycle kill the poller
                logger.exception("outbox poll cycle failed")
            await asyncio.sleep(self._interval)

    async def drain_once(self) -> int:
        """Process all currently-due records; returns how many were handled."""
        due = await self._outbox.due()
        for record in due:
            await self._deliver(record)
        return len(due)

    async def _deliver(self, record: "object") -> None:
        rec = record  # OutboxRecord
        if rec.type != WELCOME_EMAIL:  # type: ignore[attr-defined]
            return
        if not self._mailer.enabled:
            await self._outbox.mark_skipped(rec.id, "mail disabled (no SMTP configured)")  # type: ignore[attr-defined]
            return
        email = rec.payload.get("email")  # type: ignore[attr-defined]
        if not email:
            await self._outbox.mark_skipped(rec.id, "no recipient email")  # type: ignore[attr-defined]
            return
        # Never send to reserved/non-deliverable domains (example.com, .test, .local…):
        # they only bounce, and repeated bounces risk getting the sender blocked. This
        # also stops real tests (all use @example.com) from ever emailing.
        if not is_deliverable(str(email)):
            await self._outbox.mark_skipped(rec.id, f"non-deliverable address: {email}")  # type: ignore[attr-defined]
            return
        try:
            await self._mailer.send_welcome(email, rec.payload.get("name"))  # type: ignore[attr-defined]
            await self._outbox.mark_sent(rec.id)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("welcome email attempt failed for %s: %s", email, exc)
            await self._outbox.mark_attempt_failed(rec.id, f"{type(exc).__name__}: {exc}")  # type: ignore[attr-defined]
