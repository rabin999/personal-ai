"""Stdout log sink — JSON Lines to stdout (implements ports.log_sink, Part B).

For container/dev environments that collect stdout. Enabled alongside the file
sink via config (multiple transports can be active at once). Never raises.
"""

import json
import sys
from typing import Any


class StdoutLogSink:
    def write(self, record: dict[str, Any]) -> None:
        try:
            sys.stdout.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass  # logging must never break the app

    def close(self) -> None:
        pass
