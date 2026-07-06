"""Thin e2e for §1: the real startup path an app process will run.

Settings from the environment → Database → startup() (fail-loudly healthcheck
+ collection bootstrap) → handles usable → clean shutdown. Fuller e2e arrives
once modules connect on top.
"""

import pytest

from adapters.db import EPISODIC_COLLECTION, Database
from config.settings import get_settings
from tests.integration.conftest import wait_until_healthy

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]


async def test_process_startup_path_end_to_end() -> None:
    database = Database(get_settings())  # real env/.env, as a deployed process would
    try:
        await wait_until_healthy(database)
        await database.startup()
        health = await database.healthcheck()
        assert all(health.values())
        assert await database.qdrant().collection_exists(EPISODIC_COLLECTION)
        assert await database.mongo("_it_dbcheck").estimated_document_count() >= 0
    finally:
        await database.aclose()
