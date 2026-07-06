#!/usr/bin/env bash
#
# First-time server bootstrap for the Personal AI Companion.
# See docs/DEPLOYMENT.md for the full runbook.
#
# Idempotent: safe to re-run. Installs Docker + Node + uv, brings up the four
# datastores (Mongo/Qdrant/Neo4j/Redis) from the repo docker-compose.yml,
# syncs the Python env, builds the web UI, and installs + enables the systemd
# services. Run as root on the server, from the repo root (default /opt/companion).
#
# Prerequisites (done by the deploy operator, not this script):
#   - repo code present at $APP_DIR (git clone or rsync)
#   - $APP_DIR/.env present with the API keys (scp'd from the workstation)
#
# Usage:  sudo APP_DIR=/opt/companion bash deploy/setup.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/companion}"
PORT="${PORT:-8000}"
NODE_MAJOR="${NODE_MAJOR:-22}"

cd "$APP_DIR"
export DEBIAN_FRONTEND=noninteractive
export PATH="/root/.local/bin:$PATH"

echo "==> [1/8] base packages"
apt-get update -y
apt-get install -y ca-certificates curl gnupg git rsync

echo "==> [2/8] Docker Engine + compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
else
  echo "    Docker present: $(docker --version)"
fi

echo "==> [3/8] Node.js ${NODE_MAJOR} (for the Vite/React UI build)"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y nodejs
else
  echo "    Node present: $(node --version)"
fi

echo "==> [4/8] uv (Python env / lockfile manager)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
echo "    uv: $(uv --version)"

echo "==> [5/8] datastores (mongo, qdrant, neo4j, redis) via docker compose"
docker compose up -d
echo "    waiting for datastore health..."
for _ in $(seq 1 30); do
  unhealthy=$(docker compose ps --format '{{.Health}}' 2>/dev/null | grep -c "starting\|unhealthy" || true)
  [ "$unhealthy" = "0" ] && break
  sleep 3
done
docker compose ps

echo "==> [6/8] Python env (uv sync --extra voice). The 'voice' extra"
echo "    (pipecat/silero) powers /ws/voice; faster-whisper STT is CPU-viable"
echo "    (design doc) so voice runs on this CPU host - first model load is slow."
uv sync --extra voice

echo "==> [7/8] web UI build (web/dist served by FastAPI at /)"
( cd web && npm install && npm run build )

echo "==> [8/8] systemd services"
cp deploy/systemd/companion-api.service    /etc/systemd/system/
cp deploy/systemd/companion-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable companion-api companion-worker
systemctl restart companion-api companion-worker

echo "==> waiting for the API to come up..."
for _ in $(seq 1 20); do
  if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    echo "    health OK"
    break
  fi
  sleep 3
done

echo
echo "Done. Manage with:"
echo "  systemctl status companion-api companion-worker"
echo "  journalctl -u companion-api -f"
echo "Next: reverse proxy + TLS ->  sudo bash deploy/install-nginx.sh && sudo bash deploy/enable-https.sh"
curl -fsS "http://localhost:${PORT}/health" && echo || echo "(health not yet ready - check journalctl -u companion-api)"
