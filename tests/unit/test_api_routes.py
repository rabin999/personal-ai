"""Smoke tests for the serving edge routes — app built unwired, pipeline faked.

Exercises routing/auth/trace shaping without datastores or models: /health,
the text /api/chat turn (§10→§12 faked), and WS auth rejection (§26).
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.memory.working import WorkingMemory
from core.reasoning.prompt_assembly import AssembledPrompt
from core.reasoning.response_gen import GenerationResult
from ports.user_context import Unauthorized, UserRecord


class FakeUserContext:
    async def resolve(self, token: str) -> UserRecord:
        if token != "static_token_abc":
            raise Unauthorized("unknown token")
        return UserRecord(user_id="u_demo_001", companion_name="Bro")


class FakeAssembler:
    async def assemble(
        self, user_id: str, session_id: str, utterance: str, emotion: object = None
    ) -> AssembledPrompt:
        return AssembledPrompt(
            user_id=user_id, session_id=session_id, utterance=utterance,
            system_prompt="sys", messages=[{"role": "user", "content": utterance}],
            complexity_hint="simple",
        )


class FakeGenerator:
    async def generate(
        self, prompt: object, dispatcher: object = None, context: object = None
    ) -> GenerationResult:
        return GenerationResult(final_text="hi there", action="respond", turn_id="t1")


class FakeDelivery:
    async def deliveries_for_pause(
        self, session_id: str, user_id: str, recent: str
    ) -> list[object]:
        return []


@pytest.fixture
def client() -> TestClient:
    app = create_app(wire_adapters=False)
    user_context = FakeUserContext()
    app.state.user_context = user_context
    app.state.pipeline = SimpleNamespace(
        user_context=user_context,
        working=WorkingMemory(), assembler=FakeAssembler(), generator=FakeGenerator(),
        dispatcher=None, delivery=FakeDelivery(),
    )
    return TestClient(app)


def test_health_is_open(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_chat_turn_returns_reply_and_trace(client: TestClient) -> None:
    resp = client.post(
        "/api/chat",
        json={"text": "hey", "session_id": "s1"},
        headers={"Authorization": "Bearer static_token_abc"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "hi there" and body["action"] == "respond"
    stages = [e["stage"] for e in body["trace"]]
    assert "assembly" in stages and "generation" in stages and "response" in stages


def test_chat_rejects_missing_token(client: TestClient) -> None:
    assert client.post("/api/chat", json={"text": "hey"}).status_code == 401


def test_chat_rejects_unknown_token(client: TestClient) -> None:
    resp = client.post(
        "/api/chat", json={"text": "hey"},
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 401


def test_voice_ws_rejects_unauthorized(client: TestClient) -> None:
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "auth", "token": "nope"})
        assert ws.receive_json() == {"type": "error", "message": "unauthorized"}
