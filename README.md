# Personal AI Companion

A multi-user, voice-first AI companion — it listens, remembers you across
conversations, reasons over that memory, and talks back in a real voice. Built
as a provider-agnostic modular monolith with the real-time voice runtime,
background learning, and GPU emotion service split out where their runtime
characteristics genuinely differ.

> Design & scope live in [`docs/`](docs/): the **why** in
> `ai-companion-design-doc.md`, the **what** (26 module specs) in
> `ai-companion-mvp-build-spec.md`, and current progress in `docs/BUILD_STATUS.md`.

---

## What it does

- **Voice-first, continuous conversation.** Speak naturally — server-side VAD
  (Silero) and semantic endpointing decide when you've finished a thought, so
  there's no push-to-talk. Talk over it to barge in.
- **Remembers you.** Episodic memory (hybrid dense + BM25 → RRF), a temporal
  knowledge graph of facts/relationships (Graphiti/Neo4j), learned per-user
  rules, and resolved entities all feed each reply.
- **Reasons, doesn't parrot.** Prompt assembly gathers memory + traits + a
  psychological user-model, then a single agent loop generates a reply behind
  behavior gates (curiosity gate, pull-based honesty disclosure, overclaim
  rewrite).
- **Talks back** with Grok Voice TTS (inline delivery tags, streamed, interruptible).
- **Learns off the critical path.** Session-close consolidation updates rules,
  mood baseline, and correlation candidates in a background worker.
- **Multi-tenant from the foundation.** Every retrieval, write, and cost entry
  is `user_id`-scoped; one user's data never reaches another's context.

## Architecture

```
api/       thin FastAPI edge — token→user_id (§26), WebSocket voice + SSE/text, serves the UI
core/      provider-agnostic domain (memory, reasoning, tools, projects, psych, cost) → depends only on ports/
ports/     interfaces the core depends on (llm, stt, tts, ser, vector/graph/doc stores, queue, search)
adapters/  concrete, swappable implementations (OpenRouter, Grok TTS, faster-whisper, Qdrant, Neo4j, Mongo, Redis…)
voice/     real-time session runtime (VAD gate → STT → endpointing → reasoning → TTS, barge-in)
workers/   background process (consolidation/learning, web search) off the conversation-latency path
services/  ser_service — emotion2vec on a small GPU box
web/       Vite + React + Tailwind demo UI (mic picker, talking orb, live per-turn trace, replay)
```

The hard rule: **`core/` never imports `adapters/`** — adapters are wired to
the core through `ports/` in the single composition root (`api/composition.py`),
enforced in CI by `import-linter`.

## Tech stack

Python 3.11+ · FastAPI/asyncio · **uv** (lockfile-based) · MongoDB · Qdrant
(dense+BM25+RRF) · Neo4j + Graphiti · Redis · Pydantic · Pipecat/Silero VAD ·
faster-whisper (STT) · OpenRouter (LLM) · xAI Grok (TTS) · emotion2vec (SER) ·
ruff · mypy · pytest.

## Setup

```bash
uv sync --extra voice          # deps incl. the real-time voice stack (Silero)
cp .env.example .env           # then fill in the keys below
docker compose up -d           # Mongo, Qdrant, Neo4j, Redis (local dev defaults match)
```

Required in `.env`:

| Key | For |
|---|---|
| `OPEN_ROUTER_API_KEY` | LLM (chat/completions) |
| `X-AI-API` | Grok Voice TTS (§23, `https://api.x.ai/v1/tts`) |
| `SERPER_API_KEY` | web search (optional; Brave is the fallback) |
| `STT_MODEL_SIZE` | `tiny`/`base`/`small` (faster-whisper; `tiny` for a snappy demo) |

Datastore connection strings default to the docker-compose services.

## Running

```bash
# 1. serving edge (also serves the built UI at /)
STT_MODEL_SIZE=tiny uv run uvicorn api.app:app --port 8000
# 2. background worker (consolidation, web search)
uv run python -m workers.consolidation_worker
# 3. UI (dev — proxies /api and /ws to :8000)
cd web && npm install && npm run dev
```

Open the UI, pick a microphone and a Grok voice, press **Start conversation**,
and talk. The right sidebar shows each turn's pipeline start-to-finish, with a
button to replay the reply audio. Static demo users: token `static_token_abc`
(`u_demo_001`) and `static_token_xyz` (`u_demo_002`). See [`web/README.md`](web/README.md).

There's also a text endpoint for quick checks:

```bash
curl -s -X POST localhost:8000/api/chat -H "Authorization: Bearer static_token_abc" \
  -H "Content-Type: application/json" -d '{"text":"hey","session_id":"s1"}'
```

## Development & testing

```bash
uv run ruff check            # lint + format check
uv run mypy .                # types
uv run lint-imports          # core/ ↛ adapters/ boundary
uv run pytest                # unit + integration + acceptance
# FULL CHECK (pre-merge):
uv run ruff check && uv run mypy . && uv run lint-imports && uv run pytest
```

- **Unit** tests mock ports (fast, no infra). **Integration** tests run against
  the docker-compose datastores. **Acceptance** tests exercise full paths.
- **Golden sets** ([`tests/golden/`](tests/golden/)) are curated, committed
  evaluation assets: GS1 memory retrieval, GS2 entity resolution, GS3 behavior
  (+ an opt-in LLM-as-judge), GS4 learning, GS5 multi-tenant isolation. See the
  golden README for how to run them.

## Status

All 26 spec modules are built and tested, assembled into a running app + demo
UI. See `docs/BUILD_STATUS.md`. Not built (by design — backlog): presence
detection, custom wake words, encryption-at-rest, external MCP integrations,
real authentication.
