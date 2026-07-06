"""FastAPI serving edge (spec §0.6, design doc §17.3).

Thin edge: resolves bearer token → user_id (spec §26) and streams tokens/audio
(SSE/WebSocket). Adapters are wired onto ``app.state`` at startup — this is
the composition root; ``core/`` itself never imports ``adapters/``.
"""

from fastapi import FastAPI

from api.routes import health


def create_app() -> FastAPI:
    app = FastAPI(title="Personal AI Companion", version="0.1.0")

    # Wiring point: the UserContext adapter (adapters/user_context/static.py)
    # is attached here at startup once §26 is built. Until then requests that
    # need identity get 501 from api.deps.get_user_record.
    app.state.user_context = None

    app.include_router(health.router)
    return app


app = create_app()
