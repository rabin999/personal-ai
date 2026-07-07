"""Build the configured log sinks from settings (Part B — config over code).

``log_sinks`` (comma list of "file"/"stdout") selects which transports are active;
multiple may be enabled at once. Wired in the composition root so ``core/`` only
ever sees the ``StructuredLogger`` built over these sinks.
"""

import logging

from adapters.logging.file_sink import FileLogSink
from adapters.logging.stdout_sink import StdoutLogSink
from config.settings import Settings
from ports.log_sink import LogSink

logger = logging.getLogger(__name__)


def build_log_sinks(settings: Settings) -> list[LogSink]:
    names = [n.strip().lower() for n in settings.log_sinks.split(",") if n.strip()]
    sinks: list[LogSink] = []
    for name in names:
        if name == "file":
            sinks.append(FileLogSink(settings.log_file_path))
        elif name == "stdout":
            sinks.append(StdoutLogSink())
        else:
            logger.warning("unknown log sink %r; skipping", name)
    return sinks
