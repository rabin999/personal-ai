"""E2E for §26 through the HTTP edge: real app wiring, real datastores.

Boots the app exactly as production would (lifespan wires Database → Mongo doc
store → session user-context), then exercises the real session-auth path over
HTTP: a signed session cookie (minted exactly as Starlette SessionMiddleware
does) carrying our internal user_id resolves through the CurrentUser dependency
to a per-user record. Google's consent screen is the only un-automatable step;
everything after the callback establishes the session is covered here.
"""

import base64
import json

import itsdangerous
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import CurrentUser
from config.settings import get_settings

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]


def _app():  # type: ignore[no-untyped-def]
    app = create_app()
    probe = APIRouter()

    @probe.get("/whoami")
    async def whoami(user: CurrentUser) -> dict[str, str]:
        return {"user_id": user.user_id}

    app.include_router(probe)
    return app


def _session_cookie(user_id: str) -> str:
    """Sign a session cookie exactly as Starlette SessionMiddleware does."""
    signer = itsdangerous.TimestampSigner(get_settings().session_secret)
    data = base64.b64encode(json.dumps({"user_id": user_id}).encode())
    return signer.sign(data).decode()


def test_session_resolves_to_user_over_http() -> None:
    with TestClient(_app()) as client:
        client.cookies.set("session", _session_cookie("u_edge_abc"))
        response = client.get("/whoami")
    assert response.status_code == 200
    assert response.json() == {"user_id": "u_edge_abc"}


def test_forged_session_cookie_gets_401_over_http() -> None:
    with TestClient(_app()) as client:
        client.cookies.set("session", "not-a-valid-signed-cookie")
        response = client.get("/whoami")
    assert response.status_code == 401


def test_missing_session_gets_401_over_http() -> None:
    with TestClient(_app()) as client:
        response = client.get("/whoami")
    assert response.status_code == 401


def test_two_sessions_are_two_isolated_users_over_http() -> None:
    with TestClient(_app()) as client:
        client.cookies.set("session", _session_cookie("u_edge_a"))
        a = client.get("/whoami").json()["user_id"]
        client.cookies.set("session", _session_cookie("u_edge_b"))
        b = client.get("/whoami").json()["user_id"]
    assert a == "u_edge_a" and b == "u_edge_b" and a != b
