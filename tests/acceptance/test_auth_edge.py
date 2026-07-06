"""E2E for §26 through the HTTP edge: real app wiring, real datastores.

Boots the app exactly as production would (lifespan wires Database → Mongo
doc store → profile service → static user context), then exercises the
token → user_id path over HTTP via a route protected by the CurrentUser
dependency.
"""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import CurrentUser

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]


def _client() -> TestClient:
    app = create_app()
    probe = APIRouter()

    @probe.get("/whoami")
    async def whoami(user: CurrentUser) -> dict[str, str]:
        return {"user_id": user.user_id}

    app.include_router(probe)
    return TestClient(app)


def test_bearer_token_resolves_to_user_over_http() -> None:
    with _client() as client:
        response = client.get("/whoami", headers={"Authorization": "Bearer static_token_abc"})
    assert response.status_code == 200
    assert response.json() == {"user_id": "u_demo_001"}


def test_unknown_token_gets_401_over_http() -> None:
    with _client() as client:
        response = client.get("/whoami", headers={"Authorization": "Bearer stolen"})
    assert response.status_code == 401


def test_missing_token_gets_401_over_http() -> None:
    with _client() as client:
        response = client.get("/whoami")
    assert response.status_code == 401


def test_two_tokens_are_two_isolated_users_over_http() -> None:
    with _client() as client:
        a = client.get("/whoami", headers={"Authorization": "Bearer static_token_abc"})
        b = client.get("/whoami", headers={"Authorization": "Bearer static_token_xyz"})
    assert a.json()["user_id"] != b.json()["user_id"]
