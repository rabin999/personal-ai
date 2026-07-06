# Companion Web UI

Voice-first demo front-end: microphone picker, an amplitude-reactive talking
orb, and a right-hand **trace sidebar** that shows the pipeline start-to-finish
for every turn (§19 VAD → §20 STT → §21 endpointing → §10 assembly → §11/§12
generation → §23 TTS). Vite + React + TypeScript + Tailwind v4.

## Prerequisites

- Datastores up: `docker compose up -d` (from the repo root)
- `OPEN_ROUTER_API_KEY` set (LLM + TTS)
- The voice `extra` installed for server-side Silero VAD: `uv sync --extra voice`

## Run

**Two-process dev (hot reload):**

```bash
# terminal 1 — backend (serving edge)
uv run uvicorn api.app:app --port 8000

# terminal 2 — worker (background §14/§18)
uv run python -m workers.consolidation_worker

# terminal 3 — frontend (Vite proxies /api and /ws to :8000)
cd web && npm install && npm run dev
```

Open the Vite URL (default http://localhost:5173).

**Single-origin (backend serves the built UI):**

```bash
cd web && npm run build          # produces web/dist
uv run uvicorn api.app:app       # serves the UI at http://localhost:8000
```

## Use

1. Pick a microphone (grant permission once so device labels appear).
2. Token defaults to `static_token_abc` (spec §26 static user `u_demo_001`).
   The other seeded user is `static_token_xyz`.
3. **Connect**, then **hold** the button to talk, release to send.
4. Watch the trace sidebar; the orb reacts to your voice and the reply.
   Holding to talk while the companion is speaking is a barge-in (§24).
