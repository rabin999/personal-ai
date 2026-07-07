"""FastAPI serving edge (spec §0.6, design doc §17.3).

Thin edge: resolves bearer token → user_id (spec §26) and streams tokens/audio
(SSE/WebSocket). The object graph is built by ``api.composition`` — the single
composition root — so ``core/`` itself never imports ``adapters/``.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.composition import Pipeline, build_pipeline
from api.routes import (
    chat,
    conversations,
    debug,
    feedback,
    health,
    memories,
    profile,
    voice,
    voice_pipecat,
    voices,
)
from config.settings import get_settings

logger = logging.getLogger(__name__)

WEB_DIST = Path(__file__).parents[1] / "web" / "dist"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire the full pipeline at startup; fail loudly if a datastore is down (§1).

    In dev (``run_worker_in_process``) the §14 background worker runs as an
    in-process task so one command runs everything; production keeps it separate.
    """
    settings = get_settings()
    pipeline = await build_pipeline(settings)
    app.state.pipeline = pipeline
    app.state.user_context = pipeline.user_context

    worker_task: asyncio.Task[None] | None = None
    if settings.run_worker_in_process:
        from workers.consolidation_worker import build_worker

        worker = build_worker(pipeline)
        worker_task = asyncio.create_task(worker.run_forever())
        logger.info("background worker running in-process (dev; §14)")

    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        await pipeline.aclose()


def create_app(*, wire_adapters: bool = True) -> FastAPI:
    """Build the serving edge.

    ``wire_adapters=False`` skips startup wiring (no datastores touched) — for
    tests that exercise routes or dependencies in isolation.
    """
    app = FastAPI(
        title="Personal AI Companion",
        version="0.1.0",
        lifespan=_lifespan if wire_adapters else None,
    )
    if not wire_adapters:
        app.state.pipeline = None
        app.state.user_context = None
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(profile.router)
    app.include_router(voice.router)
    app.include_router(voice_pipecat.router)
    app.include_router(debug.router)
    app.include_router(conversations.router)
    app.include_router(voices.router)
    app.include_router(feedback.router)
    app.include_router(memories.router)
    _mount_web(app)
    return app


def _mount_web(app: FastAPI) -> None:
    """Serve the built UI with explicit paths (not a greedy ``/`` mount, which
    would shadow API/WS routes). Skipped until ``npm run build`` produces
    web/dist — dev uses the Vite proxy instead."""
    if not WEB_DIST.is_dir():
        return
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/pcm-worklet.js")
    async def _worklet() -> FileResponse:
        return FileResponse(WEB_DIST / "pcm-worklet.js", media_type="text/javascript")

    @app.get("/")
    async def _index() -> FileResponse:
        return FileResponse(WEB_DIST / "index.html")

    # SPA fallback via a 404 handler (NOT a catch-all route): real routes always
    # match first, so this only fires for genuinely-unmatched paths. A GET for a
    # non-API client route (/conversations, /memories, /traces, …) returns the app
    # shell so a refresh survives; everything else keeps its original error
    # (status + headers, e.g. the 401 WWW-Authenticate challenge).
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    async def _spa_fallback(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        path = request.url.path
        if (
            exc.status_code == 404
            and request.method == "GET"
            and not path.startswith(("/api", "/ws", "/debug", "/assets", "/health"))
        ):
            return FileResponse(WEB_DIST / "index.html")
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    app.add_exception_handler(StarletteHTTPException, _spa_fallback)


def get_pipeline(app: FastAPI) -> Pipeline:
    """Return the wired pipeline, or raise if the edge was built unwired."""
    pipeline: Pipeline | None = app.state.pipeline
    if pipeline is None:
        raise RuntimeError("pipeline not wired (create_app(wire_adapters=False))")
    return pipeline


app = create_app()
