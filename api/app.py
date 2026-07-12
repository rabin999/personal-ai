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
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api.auth import build_oauth
from api.composition import Pipeline, build_pipeline
from api.routes import (
    auth,
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


def _quiet_pipecat_logs() -> None:
    """Pipecat logs via loguru at DEBUG/INFO (the banner, 'Loading Silero VAD'
    on every connection). Raise its floor to WARNING so those don't spam the
    console or mislead ('am I on Pipecat?') — warnings/errors still show."""
    try:
        import sys

        from loguru import logger as _loguru

        _loguru.remove()
        _loguru.add(sys.stderr, level="WARNING")
    except Exception:  # loguru not installed / already configured — non-fatal
        pass


_quiet_pipecat_logs()

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
    # Real-auth edge state (design §18): the OAuth client + account store the
    # auth routes use. Built here (not in core) so the identity source stays in
    # the adapter/edge layer.
    app.state.accounts = pipeline.accounts

    # Warm the STT model + local embedder off the request path so the first
    # conversation doesn't pay their cold-load spike (§8.12 latency).
    async def _warm() -> None:
        # Warm each INDEPENDENTLY. They were chained, so when `stt.preload` raised
        # AttributeError (GrokSTT never implemented it) the embedder was never warmed
        # either — silently, inside the best-effort `except`. Every first turn then paid
        # the fastembed cold load.
        for name, warm in (("STT", pipeline.stt.preload), ("embedder", pipeline.llm.preload)):
            try:
                await asyncio.to_thread(warm)
                logger.info("%s warmed", name)
            except Exception:  # warmup is best-effort — never block/kill startup
                logger.warning("%s warmup failed (non-fatal)", name, exc_info=True)

    # Also pre-establish the OpenRouter HTTPS connection so the first turn's first LLM call
    # (context_intent) doesn't pay cold TLS setup. Async + best-effort, off the request path.
    bg_tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_warm()),
        asyncio.create_task(pipeline.llm.warmup()),
    ]
    if settings.run_worker_in_process:
        from workers.consolidation_worker import build_worker
        from workers.outbox_worker import OutboxWorker

        worker = build_worker(pipeline)
        bg_tasks.append(asyncio.create_task(worker.run_forever()))
        # Outbox poller delivers welcome emails off the signup path (§4).
        outbox_worker = OutboxWorker(pipeline.outbox, pipeline.mailer)
        bg_tasks.append(asyncio.create_task(outbox_worker.run_forever()))
        logger.info("background worker + outbox poller running in-process (dev; §14/§4)")

    # §8.12 dynamic phrases: seed the in-memory catalog with whatever the worker last stored
    # (best-effort, off the request path), then keep it fresh on a slow tick. In dev the worker
    # runs in-process, so also drive regeneration here; in prod the separate worker owns regen.
    if settings.phrases_dynamic_enabled:
        from core.phrases.refresh import refresh_forever, regenerate_forever

        async def _seed_phrases() -> None:
            try:
                stored = await pipeline.phrase_store.load()
                if stored:
                    pipeline.phrases.apply({k: tuple(v) for k, v in stored.items()})
            except Exception:  # defaults stand — never block startup on the catalog
                logger.warning("phrase catalog seed failed (non-fatal)", exc_info=True)

        bg_tasks.append(asyncio.create_task(_seed_phrases()))
        bg_tasks.append(
            asyncio.create_task(
                refresh_forever(
                    pipeline.phrase_store, pipeline.phrases, settings.phrase_refresh_interval_s
                )
            )
        )
        if settings.run_worker_in_process:
            bg_tasks.append(
                asyncio.create_task(
                    regenerate_forever(
                        pipeline.phrase_generator,
                        pipeline.phrase_store,
                        pipeline.phrases,
                        settings.phrase_regen_interval_s,
                    )
                )
            )

    try:
        yield
    finally:
        for task in bg_tasks:
            task.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)
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
    settings = get_settings()
    # Auth edge (design §18): OAuth client + settings live on app.state so the
    # auth routes work; the account store is attached at startup (needs Mongo).
    app.state.settings = settings
    app.state.oauth = build_oauth(settings)
    app.state.accounts = None
    if not wire_adapters:
        app.state.pipeline = None
        app.state.user_context = None

    # Trust the reverse proxy's forwarded headers so generated URLs are https
    # behind TLS termination (brief §0, belt-and-suspenders to PUBLIC_BASE_URL).
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
    # Signed session cookie carrying our user_id (brief §3). Secure (HTTPS-only)
    # in prod, httponly always; same_site=lax so the cookie survives the top-level
    # OAuth redirect back from Google.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        max_age=settings.session_max_age_s,
        https_only=settings.cookie_secure,
        same_site="lax",
    )

    app.include_router(auth.router)
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

    @app.get("/og.png")
    async def _og() -> FileResponse:
        # Social-share preview image (LinkedIn/WhatsApp/Twitter). Served explicitly so the
        # SPA fallback doesn't return index.html for it.
        return FileResponse(WEB_DIST / "og.png", media_type="image/png")

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
            and not path.startswith(("/api", "/ws", "/auth", "/debug", "/assets", "/health"))
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
