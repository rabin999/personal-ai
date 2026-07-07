"""Transactional outbox — reliable, non-blocking side effects (brief §4)."""

from adapters.outbox.mailer import WelcomeMailer
from adapters.outbox.store import OUTBOX_COLLECTION, OutboxRecord, OutboxStore

__all__ = ["OUTBOX_COLLECTION", "OutboxRecord", "OutboxStore", "WelcomeMailer"]
