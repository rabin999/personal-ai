"""Structured-log sink adapters (pluggable transport, brief Part B)."""

from adapters.logging.file_sink import FileLogSink
from adapters.logging.stdout_sink import StdoutLogSink

__all__ = ["FileLogSink", "StdoutLogSink"]
