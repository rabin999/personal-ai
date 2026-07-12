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

| Layer | Tools |
|---|---|
| **Language / runtime** | Python 3.11+ · asyncio |
| **Serving** | FastAPI (SSE/WebSocket streaming edge) · **uv** (lockfile-based deps) |
| **Voice loop** | **Pipecat** (pipeline, transport, barge-in) · **Silero** VAD (idle-is-free cost gate) · semantic endpointing |
| **STT** | Grok STT (default, fast) · faster-whisper (local, $0 fallback) |
| **LLM** | **OpenRouter** — complexity-tier routing + provider fallback, exact-usage cost accounting |
| **TTS** | **xAI Grok Voice TTS** — inline delivery tags, PCM streamed, interruptible |
| **SER (emotion)** | emotion2vec on a GPU microservice (`services/ser_service`) |
| **Episodic memory** | **Qdrant** — dense + BM25 sparse → RRF, filtered-HNSW, `user_id`-scoped |
| **Semantic / temporal memory** | **Graphiti + Neo4j** — temporal knowledge graph of facts/relationships |
| **Personalization memory** | **Mem0** — wired into live prompt assembly, reconciled with custom extraction |
| **Doc store** | **MongoDB** — profiles, conversations, cost ledger, search cache, traces |
| **Queue / cache** | **Redis** — background task queue + cache |
| **Web search** | **Serper** (primary) · **Brave** (fallback) · Mongo cache · Crawl4AI verified-retrieval pipeline *(in progress)* |
| **Tracing / eval** | **Langfuse** — per-turn trace (per-LLM token/cost/latency, tool calls, self-reflection span), LLM-as-judge |
| **Validation** | **Pydantic** — every LLM JSON output validated (retry once → safe fallback) |
| **UI** | Vite · React · TypeScript · Tailwind (mic picker, talking orb, live per-turn trace, replay) |
| **Auth** | static bearer token → static user record (local/dev); Google OAuth SSO on the deployed server |
| **Dev toolchain** | ruff (lint + format) · mypy · pytest (+asyncio, +cov) · **import-linter** (`core/ ↛ adapters/`) · pre-commit |

Concrete providers sit behind `ports/` and are swappable — the composition root
(`api/composition.py`) is the single place they're wired to the core.

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

## Evaluating the LLM & its output

This is a GenAI companion, so a test that mocks the model and passes while the app
gives bad replies is worthless. The rule: **the core reasoning/memory/response loop is
judged on REAL model calls against REAL stores** — mocking the model there proves nothing.
Tests marked `@pytest.mark.real_call` need `OPEN_ROUTER_API_KEY` + the docker stores.

```bash
uv run pytest -m "not real_call"    # deterministic logic (mock ports) — CI default
uv run pytest -m real_call          # REAL model + REAL stores — the GenAI loop
```

**How each behavior is evaluated**

| Concern | Asset | What it measures |
|---|---|---|
| Live-info gating (volatile vs stable) | [`tests/labeled/volatility.jsonl`](tests/labeled/volatility.jsonl) (174 queries, 87/87, 22 classes) + `tests/engine/test_e2_volatility_classifier.py` | Per-class **precision/recall** of the search-gate classifier; false-negatives pinned so any drift shows |
| Verify-before-answer invariant | `tests/real_call/test_verify_before_answer_freshness.py` | A volatile officeholder/news question **searches first** and never ships a stale/guessed answer; a search that returns nothing yields an honest miss, never the training-data draft |
| Multi-turn conversation ("proof by conversation") | [`tests/real_call/multiturn_scenarios.jsonl`](tests/real_call/multiturn_scenarios.jsonl) + `test_multiturn_scenarios.py` | Data-driven scenarios drive real turns with per-turn assertions: did it search, is banned assistant-speak absent, are list items distinct, is context carried across turns |
| Response quality (warm / human / short / companion) | GS3-judge ([`tests/golden/gs3_judge.json`](tests/golden/)) + `tests/support/judge.py` | **LLM-as-judge** on a *pinned* model, human-calibrated, scores the reply against the design's response standard; negative examples must fail. Opt-in (`RUN_GS3_JUDGE=1`) so it never destabilizes the deterministic suite |
| Retrieval quality | verified-retrieval pipeline tests (`tests/retrieval/`) | Cross-checked, corroboration ≥2, honest "not found" — never fabricates a source |
| No silent failure (D-9) | `tests/engine/test_e1_enforcement.py` | Any dependency failure degrades to a reply (never `reply=""`); a total model outage is honest that the system is down; programming errors still re-raise loudly |
| Never invents user facts (D-19) | `tests/acceptance/test_core_engine_e2e.py` | Asked about an unknown personal fact, the engine admits it doesn't know rather than fabricating |
| Every LLM JSON output | Pydantic validation in the engine | Invalid JSON → retry once → safe fallback (invariant); measured on the real judgment path |

**Choosing & benchmarking models.** Model choice is config (`config/defaults/provider_config.json`
→ `llm_router` tiers), never hard-coded, and is picked from **live measurement, not names**: each
candidate is scored on time-to-first-token (voice needs low TTFT), JSON reliability (the judgment
path), tool-calling support (native + our `draft_response`/`tool_request` envelope), and conversational
quality. That's how the reply tier landed on `claude-haiku-4.5` and the last-resort **free** fallback
on `openai/gpt-oss-20b:free` (so a credit outage still answers) — the latter verified end-to-end
through the adapter, including its reasoning-mandatory quirk. Per-turn token/cost/latency and the
self-reflection span are captured in the Langfuse trace for every call.

## Status

All 26 spec modules are built and tested, assembled into a running app + demo
UI. See `docs/BUILD_STATUS.md`. Not built (by design — backlog): presence
detection, custom wake words, encryption-at-rest, external MCP integrations,
real authentication.
