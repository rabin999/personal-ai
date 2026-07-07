#!/usr/bin/env bash
# Full pre-merge check — run ONCE per completed bundle of work, not per commit.
# (Pre-commit only runs fast ruff; this is the heavy gate.)
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== ruff ==";        uv run ruff check
echo "== mypy ==";        uv run mypy .
echo "== lint-imports =="; uv run lint-imports
echo "== pytest (no paid) =="; uv run pytest -m "not paid" "$@"
echo "ALL GREEN"
