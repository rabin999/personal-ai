"""Phase 0 smoke tests: the scaffold runs and the wiring points behave.

No module logic exists yet — these only pin the serving-edge skeleton and the
token → user_id wiring point (spec §26 rule 1) so later phases build on a
verified shell.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import get_user_record
from config.settings import Settings
from ports.user_context import UserRecord


def test_health_endpoint_runs_without_auth() -> None:
    with TestClient(create_app(wire_adapters=False)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class _FakeRequest:
    """Minimal stand-in exposing ``request.app.state.user_context`` + session."""

    class _State:
        user_context: object | None = None

    class _App:
        pass

    def __init__(self, user_context: object | None, session: dict | None = None) -> None:
        self.app = self._App()
        self.app.state = self._State()  # type: ignore[attr-defined]
        self.app.state.user_context = user_context  # type: ignore[attr-defined]
        self.session = session or {}


async def test_user_record_dependency_501_until_adapter_is_wired() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_user_record(_FakeRequest(None, {"user_id": "u_demo_001"}))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 501


class _SessionStub:
    """Stub UserContext: resolves an authenticated user_id (spec §26 shape)."""

    async def record_for(self, user_id: str) -> UserRecord:
        return UserRecord(user_id=user_id)


async def test_user_record_dependency_resolves_session_user() -> None:
    request = _FakeRequest(_SessionStub(), {"user_id": "u_demo_001"})
    record = await get_user_record(request)  # type: ignore[arg-type]
    assert record.user_id == "u_demo_001"


async def test_user_record_dependency_401_without_session() -> None:
    # No user_id in the session → not authenticated (real auth §26).
    with pytest.raises(HTTPException) as exc_info:
        await get_user_record(_FakeRequest(_SessionStub(), {}))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 401


def test_settings_load_from_env_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "test-key-from-env")
    settings = Settings(_env_file=None)
    assert settings.open_router_api_key == "test-key-from-env"
