#!/usr/bin/env bash
# Dev run: one command runs the API (+ optional in-process worker), and exits
# cleanly on a single Ctrl+C (graceful-shutdown timeout so an open voice
# WebSocket can't wedge shutdown).
set -euo pipefail
cd "$(dirname "$0")/.."
export run_worker_in_process="${run_worker_in_process:-true}"
exec uv run uvicorn api.app:app --host 0.0.0.0 --port 8000 \
  --timeout-graceful-shutdown 3 "$@"
