"""Unit tests for the pluggable structured logging transport (brief Part B)."""

import json
from pathlib import Path
from typing import Any

from adapters.logging.factory import build_log_sinks
from adapters.logging.file_sink import FileLogSink
from config.settings import Settings
from core.observability.logger import StructuredLogger


class _CapturingSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)

    def close(self) -> None:
        pass


def test_structured_record_has_level_event_and_fields() -> None:
    sink = _CapturingSink()
    log = StructuredLogger([sink])
    log.info("turn.response", action="respond", reply_chars=42)
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["level"] == "info" and rec["event"] == "turn.response"
    assert rec["action"] == "respond" and rec["reply_chars"] == 42
    assert "ts" in rec


def test_bind_attaches_correlation_ids_within_scope_only() -> None:
    sink = _CapturingSink()
    log = StructuredLogger([sink])
    with log.bind(trace_id="s1", turn_id=3, user_id="u_demo_001"):
        log.info("inside")
    log.info("outside")
    inside, outside = sink.records
    assert inside["trace_id"] == "s1" and inside["turn_id"] == "3"
    assert inside["user_id"] == "u_demo_001"
    assert "trace_id" not in outside  # correlation cleared after the scope


def test_fans_out_to_multiple_sinks() -> None:
    a, b = _CapturingSink(), _CapturingSink()
    StructuredLogger([a, b]).info("evt")
    assert len(a.records) == 1 and len(b.records) == 1


def test_a_failing_sink_never_breaks_logging() -> None:
    class _Boom:
        def write(self, record: dict[str, Any]) -> None:
            raise RuntimeError("sink down")

        def close(self) -> None:
            pass

    good = _CapturingSink()
    StructuredLogger([_Boom(), good]).info("evt")  # must not raise
    assert len(good.records) == 1  # the healthy sink still got it


def test_file_sink_writes_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "log.jsonl"  # parent dir auto-created
    sink = FileLogSink(str(path))
    sink.write({"event": "a", "user_id": "u1"})
    sink.write({"event": "b", "user_id": "u1"})
    sink.close()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "a"
    assert json.loads(lines[1])["event"] == "b"


def test_factory_builds_configured_sinks() -> None:
    sinks = build_log_sinks(Settings(log_sinks="file,stdout", log_file_path="/tmp/x.jsonl"))
    names = {type(s).__name__ for s in sinks}
    assert names == {"FileLogSink", "StdoutLogSink"}
    for s in sinks:
        s.close()
