"""File log sink — JSON Lines to a local file (implements ports.log_sink, Part B).

The default transport: one JSON object per line, append-only, so logs are
machine-parseable (jq-able) and correlation-id searchable. Creates parent dirs.
Never raises into the caller.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class FileLogSink:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._file: Any = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8")
        except Exception:
            logger.exception("could not open log file %s; file sink disabled", path)
            self._file = None

    def write(self, record: dict[str, Any]) -> None:
        if self._file is None:
            return
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                finally:
                    self._file = None
