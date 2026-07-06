"""Shared fixtures for integration tests — real datastores via docker-compose.

Start them first: ``docker compose up -d``. Tests FAIL (not skip) when stores
are unreachable, matching the module's own fail-loudly rule.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from adapters.db import Database
from config.settings import Settings

STARTUP_DEADLINE_S = 60


async def wait_until_healthy(db: Database) -> None:
    """Poll healthcheck until all stores respond (containers may still be booting)."""
    async with asyncio.timeout(STARTUP_DEADLINE_S):
        while True:
            health = await db.healthcheck()
            if all(health.values()):
                return
            await asyncio.sleep(1)


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database(Settings(_env_file=None))
    try:
        await wait_until_healthy(database)
    except TimeoutError:
        await database.aclose()
        pytest.fail("datastores unreachable — run `docker compose up -d` before integration tests")
    yield database
    await database.aclose()
