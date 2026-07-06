"""FastAPI serving edge (spec §0.6, design doc §17.3).

Thin edge: resolves bearer token → user_id (spec §26) and streams tokens/audio
(SSE/WebSocket). This module is the composition root — the one place adapters
are constructed and wired; ``core/`` itself never imports ``adapters/``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from adapters.db import Database
from adapters.doc.mongo import MongoDocStore
from adapters.user_context.static import StaticUserContext
from api.routes import health
from config.settings import get_settings
from core.profile import ProfileService, TraitRegistry

DEFAULTS_DIR = Path(__file__).parents[1] / "config" / "defaults"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire adapters at startup; fail loudly if any datastore is unreachable (§1)."""
    db = Database(get_settings())
    await db.startup()

    store = MongoDocStore(db)
    profiles = ProfileService(store)
    registry = TraitRegistry(store, profiles)
    await registry.seed_defaults(DEFAULTS_DIR)

    app.state.db = db
    app.state.profiles = profiles
    app.state.registry = registry
    app.state.user_context = StaticUserContext.from_defaults(DEFAULTS_DIR, profiles)
    try:
        yield
    finally:
        await db.aclose()


def create_app(*, wire_adapters: bool = True) -> FastAPI:
    """Build the serving edge.

    ``wire_adapters=False`` skips the startup wiring (no datastores touched) —
    for tests that exercise routes or dependencies in isolation.
    """
    app = FastAPI(
        title="Personal AI Companion",
        version="0.1.0",
        lifespan=_lifespan if wire_adapters else None,
    )
    if not wire_adapters:
        app.state.user_context = None
    app.include_router(health.router)
    return app


app = create_app()
