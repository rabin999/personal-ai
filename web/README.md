# Companion Web UI

Voice-first demo front-end: microphone picker, an amplitude-reactive talking
orb, a single **Start/Stop conversation** control (continuous — the server
takes turns on its own, no push-to-talk), and a right-hand **trace sidebar**
of **collapsible per-turn cards** (newest expanded) — each showing the pipeline
start-to-finish (§19 VAD → §20 STT → §21 endpointing → §10 assembly → §11/§12
generation → §23 Grok TTS) with a **replay** button for that turn's reply
audio. Talk over the companion to barge in (§24). Vite + React + TS + Tailwind v4.

## Prerequisites

- Datastores up: `docker compose up -d` (from the repo root)
- `OPEN_ROUTER_API_KEY` set (LLM) and `X-AI-API` set (Grok Voice TTS, §23)
- The voice `extra` installed for server-side Silero VAD: `uv sync --extra voice`
- For a snappier demo, set `STT_MODEL_SIZE=tiny` (base is more accurate but slower on CPU)

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

1. Pick a microphone (grant permission once so device labels appear) and a Grok voice.
2. Token defaults to `static_token_abc` (spec §26 static user `u_demo_001`).
   The other seeded user is `static_token_xyz`.
3. Press **Start conversation** and just talk — the companion detects when you
   finish (§21) and replies on its own. Press **Stop conversation** to end.
4. Watch the trace sidebar: each turn is a collapsible card; tap to expand, and
   hit **play** to replay that reply's audio. Talk over the companion to interrupt (§24).
