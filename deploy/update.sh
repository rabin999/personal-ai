#!/usr/bin/env bash
#
# Idempotent update for an already-provisioned server (see deploy/setup.sh).
# Pulls latest main, re-syncs the Python env (incl. the voice extra), rebuilds
# the web UI, refreshes the systemd units, and restarts the services.
#
# Usage:  sudo bash /opt/companion/deploy/update.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/companion}"
PORT="${PORT:-8000}"
export PATH="/root/.local/bin:$PATH"

cd "$APP_DIR"

echo "==> pulling latest main"
if [ -d .git ] && git remote get-url origin >/dev/null 2>&1; then
  git fetch origin main
  git checkout main
  git pull --ff-only origin main
else
  echo "!! $APP_DIR is not a git checkout with an 'origin' remote."
  echo "   Update the code by rsync from your workstation instead, e.g.:"
  echo "   rsync -az --delete --exclude .venv --exclude web/node_modules \\"
  echo "     --exclude web/dist --exclude .env ./ root@SERVER:$APP_DIR/"
fi

# The 'voice' extra (pipecat/silero) powers /ws/voice VAD + the real-time
# session (spec §19/§24). faster-whisper STT is CPU-viable per the design doc,
# so voice runs on this CPU host - keep the extra installed on every deploy or
# /ws/voice returns {"type":"error","message":"voice extra not installed"}.
echo "==> uv sync (with voice extra)"
uv sync --extra voice

echo "==> rebuild web UI"
( cd web && npm install && npm run build )

echo "==> refresh systemd units"
cp deploy/systemd/companion-api.service    /etc/systemd/system/
cp deploy/systemd/companion-worker.service /etc/systemd/system/
systemctl daemon-reload

echo "==> restart services"
systemctl restart companion-api companion-worker

echo "==> health:"
sleep 5
curl -fsS "http://localhost:${PORT}/health" && echo || echo "(not ready - journalctl -u companion-api -f)"
