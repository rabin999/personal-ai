"""Golden-set fixtures. Re-exports the real-datastore ``db`` fixture so the
integration-backed golden sets (GS1/GS2/GS4/GS5) run against docker-compose."""

from tests.integration.conftest import db, wait_until_healthy

__all__ = ["db", "wait_until_healthy"]
