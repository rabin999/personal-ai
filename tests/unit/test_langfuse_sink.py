"""Langfuse trace sink contract (A8 / design: tracing + isolation, spec §26).

Mocks the Langfuse client so we can assert — without a Langfuse server — that a
per-turn record becomes an observation that:
- is attributed to the user (propagate_attributes(user_id=..., session_id=...)),
  so multi-tenant per-user filtering/cost works;
- carries the REAL prompt (system + messages) as `input` and the reply as `output`
  on the generation, so the system prompt is visible (not an empty span);
- reports model / token usage / cost on the generation.
These three were the reported gaps: no user, no prompts visible.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest


class _FakeSpan:
    def __init__(self) -> None:
        self.updates: dict[str, Any] = {}

    def __enter__(self) -> _FakeSpan:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def update(self, **kwargs: Any) -> None:
        self.updates.update(kwargs)


class _FakeLangfuse:
    def __init__(self, **_: Any) -> None:
        self.span = _FakeSpan()
        self.observation_calls: list[dict[str, Any]] = []

    def create_trace_id(self, *, seed: str) -> str:
        import hashlib

        return hashlib.sha256(seed.encode()).hexdigest()[:32]

    def start_as_current_observation(self, **kwargs: Any) -> _FakeSpan:
        self.observation_calls.append(kwargs)
        return self.span


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> Any:
    import langfuse

    monkeypatch.setattr(langfuse, "Langfuse", _FakeLangfuse)
    from adapters.tracing.langfuse_sink import LangfuseTraceSink

    return LangfuseTraceSink("pk", "sk", "http://lf:3000")


def test_generation_record_carries_user_prompt_output_and_cost(
    sink: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    propagated: dict[str, Any] = {}

    @contextmanager
    def fake_propagate(**kwargs: Any) -> Any:
        propagated.update(kwargs)
        yield

    monkeypatch.setattr("langfuse._client.propagation.propagate_attributes", fake_propagate)

    sink.write(
        {
            "user_id": "u_demo_001",
            "trace_id": "sessX",
            "turn_id": 3,
            "stage": "llm",
            "event": "llm.call",
            "model": "anthropic/claude",
            "input_tokens": 123,
            "output_tokens": 45,
            "cost_usd": 0.0012,
            "messages": [
                {"role": "system", "content": "You are a warm companion."},
                {"role": "user", "content": "hi"},
            ],
            "completion": "hey, good to see you!",
        }
    )

    # #4 user tracking: the user (and session) is propagated onto the trace.
    assert propagated == {"user_id": "u_demo_001", "session_id": "sessX"}
    # It was a generation observation (gets model/usage/cost columns).
    assert sink._lf.observation_calls[0]["as_type"] == "generation"
    u = sink._lf.span.updates
    # #3 prompts visible: the real system prompt + messages are the input; reply out.
    assert u["input"][0]["content"] == "You are a warm companion."
    assert u["output"] == "hey, good to see you!"
    assert u["model"] == "anthropic/claude"
    assert u["usage_details"] == {"input": 123, "output": 45}
    assert u["cost_details"] == {"total": 0.0012}
    # prompt/reply are promoted to input/output, not duplicated into metadata.
    assert "messages" not in u["metadata"]
    assert "completion" not in u["metadata"]


def test_non_generation_record_is_a_plain_span_with_user(
    sink: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    propagated: dict[str, Any] = {}

    @contextmanager
    def fake_propagate(**kwargs: Any) -> Any:
        propagated.update(kwargs)
        yield

    monkeypatch.setattr("langfuse._client.propagation.propagate_attributes", fake_propagate)

    sink.write(
        {
            "user_id": "u2",
            "trace_id": "sessY",
            "turn_id": 1,
            "stage": "memory",
            "event": "recall",
            "hits": 4,
        }
    )

    assert propagated == {"user_id": "u2", "session_id": "sessY"}
    assert sink._lf.observation_calls[0]["as_type"] == "span"
    # memory span keeps its fields as metadata.
    assert sink._lf.span.updates["metadata"]["hits"] == 4


def test_record_without_user_is_skipped(sink: Any) -> None:
    # A record not bound to a turn (no user/trace) must not create an observation.
    sink.write({"event": "boot", "stage": "startup"})
    assert sink._lf.observation_calls == []
