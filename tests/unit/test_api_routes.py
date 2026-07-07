"""Smoke tests for the serving edge routes — app built unwired, pipeline faked.

Exercises routing/auth/trace shaping without datastores or models: /health,
the text /api/chat turn (§10→§12 faked), and WS auth rejection (§26).
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.memory.extraction import ExtractionResult
from core.memory.working import WorkingMemory
from core.observability.logger import StructuredLogger
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
            user_id=user_id,
            session_id=session_id,
            utterance=utterance,
            system_prompt="sys",
            messages=[{"role": "user", "content": utterance}],
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


class FakeTTS:
    async def speak(
        self, text: str, voice: str | None = None, *, user_id: str, session_id: str | None = None
    ) -> AsyncIterator[bytes]:
        yield b"\x01\x00" * 240  # a little PCM16


class FakeExtractor:
    async def extract_and_store(
        self, user_id: str, session_id: str, user_text: str, assistant_text: str
    ) -> ExtractionResult:
        return ExtractionResult()


class FakeEpisodic:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, list[str]]] = []

    async def write(self, user_id: str, session_id: str, chunks: list[str]) -> None:
        self.writes.append((user_id, session_id, chunks))


class FakeConversations:
    async def list_conversations(self, user_id, *, offset=0, limit=20, start_ts=None, end_ts=None):  # type: ignore[no-untyped-def]
        rows = [{"session_id": "s1", "user_id": user_id, "turn_count": 2, "last_ts": 100.0}]
        return rows[offset : offset + limit], len(rows)

    async def turns(self, user_id, session_id, *, offset=0, limit=200):  # type: ignore[no-untyped-def]
        return [{"user_id": user_id, "session_id": session_id, "turn_index": 1, "user_text": "hi"}]


class FakeFeedback:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def record(self, *, user_id, session_id, rating, turn_id=None, trace_id=None, note=""):  # type: ignore[no-untyped-def]
        rec = {"user_id": user_id, "session_id": session_id, "rating": rating, "note": note}
        self.records.append(rec)
        return SimpleNamespace(id="fb_1", rating=rating)

    async def list_for_user(self, user_id, *, offset=0, limit=50, rating=None):  # type: ignore[no-untyped-def]
        rows = [r for r in self.records if r["user_id"] == user_id]
        return rows[offset : offset + limit], len(rows)


class _Fact:
    def __init__(self, fact: str) -> None:
        self.fact = fact
        self.valid_from = None
        self.valid_to = None


class FakeSemantic:
    async def profile_facts(self, user_id, limit=50):  # type: ignore[no-untyped-def]
        return [_Fact("takes meds at 8pm")]


class _EpiHit:
    def __init__(self, mid: str) -> None:
        self.id = mid
        self.text = "bought 10 SYPNL"
        self.timestamp = "2026-07-07"
        self.session_id = "s1"


class FakeEpisodicMem(FakeEpisodic):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    async def list_recent(self, user_id, limit=50):  # type: ignore[no-untyped-def]
        return [_EpiHit("m1"), _EpiHit("m2")]

    async def delete(self, user_id, memory_id):  # type: ignore[no-untyped-def]
        self.deleted.append(memory_id)
        return memory_id == "m1"


class _Rule:
    id = "r1"
    rule_text = "greet warmly"
    trigger = "session_start"
    confidence = 0.7
    evidence_count = 3
    updated_at = "2026-07-07"


class FakeProcedural:
    async def rules_for(self, user_id, context=None):  # type: ignore[no-untyped-def]
        return [_Rule()]


@pytest.fixture
def client() -> TestClient:
    app = create_app(wire_adapters=False)
    user_context = FakeUserContext()
    app.state.user_context = user_context
    app.state.pipeline = SimpleNamespace(
        user_context=user_context,
        working=WorkingMemory(),
        assembler=FakeAssembler(),
        generator=FakeGenerator(),
        dispatcher=None,
        delivery=FakeDelivery(),
        tts=FakeTTS(),
        conversations=FakeConversations(),
        episodic=FakeEpisodicMem(),
        extractor=FakeExtractor(),
        logs=StructuredLogger([]),
        feedback=FakeFeedback(),
        semantic=FakeSemantic(),
        procedural=FakeProcedural(),
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer static_token_abc"}


def test_voices_list_and_sample_wav(client: TestClient) -> None:
    voices = client.get("/api/voices", headers=_auth()).json()["voices"]
    assert "eve" in voices
    resp = client.get("/api/voices/eve/sample", headers=_auth())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"  # playable WAV container


def test_voice_sample_unknown_voice_404(client: TestClient) -> None:
    assert client.get("/api/voices/nope/sample", headers=_auth()).status_code == 404


def test_conversations_list_is_authed_and_paginated(client: TestClient) -> None:
    assert client.get("/api/conversations").status_code == 401
    body = client.get("/api/conversations", headers=_auth()).json()
    assert body["total"] == 1 and body["conversations"][0]["session_id"] == "s1"


def test_conversations_bad_date_range_400(client: TestClient) -> None:
    resp = client.get("/api/conversations?from=not-a-date", headers=_auth())
    assert resp.status_code == 400


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
        "/api/chat",
        json={"text": "hey"},
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 401


def test_voice_ws_rejects_unauthorized(client: TestClient) -> None:
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "auth", "token": "nope"})
        assert ws.receive_json() == {"type": "error", "message": "unauthorized"}


def test_feedback_submit_and_list(client: TestClient) -> None:
    assert (
        client.post("/api/feedback", json={"session_id": "s1", "rating": "down"}).status_code == 401
    )
    r = client.post(
        "/api/feedback",
        json={"session_id": "s1", "rating": "down", "note": "wrong"},
        headers=_auth(),
    )
    assert r.status_code == 200 and r.json()["rating"] == "down"
    listed = client.get("/api/feedback", headers=_auth()).json()
    assert listed["total"] == 1 and listed["feedback"][0]["rating"] == "down"


def test_memories_grouped_by_type(client: TestClient) -> None:
    assert client.get("/api/memories/semantic").status_code == 401
    sem = client.get("/api/memories/semantic", headers=_auth()).json()
    assert sem["type"] == "semantic" and "8pm" in sem["items"][0]["fact"]
    epi = client.get("/api/memories/episodic", headers=_auth()).json()
    assert epi["type"] == "episodic" and len(epi["items"]) == 2
    proc = client.get("/api/memories/procedural", headers=_auth()).json()
    assert proc["items"][0]["confidence"] == 0.7


def test_delete_episodic_memory(client: TestClient) -> None:
    assert client.delete("/api/memories/episodic/m1", headers=_auth()).status_code == 200
    assert client.delete("/api/memories/episodic/nope", headers=_auth()).status_code == 404
