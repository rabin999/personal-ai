# Project: Personal AI Companion

A multi-user, voice-first AI companion built multi-tenant-ready. This file is your
persistent operating contract. Read it fully at the start of every session.

---

## 1. Source of Truth (read before coding)

- `docs/ai-companion-design-doc.md` — the **WHY**: architecture, decisions, rationale.
- `docs/ai-companion-mvp-build-spec.md` — the **WHAT**: 26 modules, each with
  interface, data schema, behavior rules, and acceptance criteria.
- `docs/BUILD_STATUS.md` — current progress (state). Read it first each session;
  update it when a module is completed. This is the ONLY file you routinely update
  as you build.
- **Git history is the session log.** Each commit (referencing its spec module) is the
  record of what was done. There is no separate session-log file to maintain.

**Rules about the docs:**
- ALWAYS read the relevant spec module section **before** writing any code for it.
- If code and spec disagree, **the spec wins**.
- If the spec itself seems wrong, incomplete, or ambiguous, **STOP and ask** —
  do not silently deviate or invent behavior.
- Do not restate large chunks of the docs back to me; act on them.

---

## 2. Non-Negotiable Invariants

These are enforced across every module (spec §0.5 and §25). Violating any of these
is a defect even if tests pass:

1. **Multi-tenant isolation.** Every retrieval, write, and cost entry is
   `user_id`-scoped. One user's data must NEVER appear in another user's context
   (prompt bleed). This is a correctness invariant, not an optimization.
2. **`user_id` comes from the resolved User Context (spec §26) — never hard-code it**
   anywhere in `core/`. Take it from the request/session context.
3. **Ports & adapters boundary.** `core/` depends ONLY on interfaces in `ports/`.
   `core/` must NEVER import from `adapters/`. Adapters are wired at startup.
4. **Everything money-costing logs to the Cost Ledger (§3)**, async, after the call
   resolves, never blocking the user-facing response. Cache hits log as $0.
5. **Every LLM JSON output is Pydantic-validated.** On validation failure: retry
   once, then fall back to a safe response. Never trust unvalidated model JSON.
6. **Config over code.** Behavior params (thresholds, trait descriptions, provider
   choices) live in the profile/registry/config, not hard-coded in logic.
7. **The companion never speaks first.** No process emits user-facing output except
   in response to user input — the one exception is consent-gated project insight
   (§16), which still asks permission before speaking.
8. **Idle is nearly free.** The VAD gate (§19) must block all paid calls during
   silence. Verify no STT/LLM/TTS fires when there's no speech.
9. **Never diagnose; correlation ≠ causation** (§17, §18). Emotion/psych inferences
   are probabilistic signals, never clinical claims.
10. **Async-first.** Use `asyncio`. Slow work goes to the queue (§14), never blocks
    the conversation path.

---

## 3. Architecture (spec §0.6, design doc §17)

**Shape:** modular monolith (`core/`, provider-agnostic) + separated services where
runtime characteristics genuinely differ:
- **voice/** — real-time session runtime (stateful, latency-critical)
- **workers/** — background/async (consolidation/learning, background search); off
  the conversation-latency path
- **services/ser_service/** — SER (emotion2vec), needs GPU
- **api/** — thin FastAPI serving edge (SSE/WebSocket streaming; resolves token → user_id)

**Follow the directory scaffold in design doc §17.3 exactly.** Key rule again:
`core/` → `ports/` (interfaces) → `adapters/` (concrete, swappable). Never shortcut this.

---

## 4. Tech Stack (spec §0.3) — ask before adding anything not listed

- **Language:** Python 3.11+ · **Serving:** FastAPI (ASGI) · **Async:** asyncio
- **Package/env manager:** **`uv`** (deterministic, lockfile-based — always use it, never bare pip)
- **Voice runtime:** Pipecat (or LiveKit Agents) — AEC, noise suppression, VAD, barge-in
- **VAD:** Silero · **STT:** OpenRouter `/audio/transcriptions` (or faster-whisper local)
- **SER:** emotion2vec (self-hosted GPU service)
- **LLM:** OpenRouter `/chat/completions` (complexity-tier routing, fallback)
- **TTS:** Grok Voice TTS via OpenRouter `/audio/speech` (inline tags)
- **Doc/relational store:** MongoDB · **Vector:** Qdrant (dense+BM25+RRF, filtered-HNSW)
- **Graph:** Neo4j + Graphiti (temporal validity) · **Queue/cache:** Redis
- **Search:** Serper (primary) + Brave (fallback) + query cache
- **Validation:** Pydantic
- **Auth:** NONE built — static bearer token → static user record (§26)

**Dev toolchain (standardized — all config in `pyproject.toml`):**
- **`ruff`** — linting + formatting (replaces black/flake8/isort)
- **`mypy`** — static type checking
- **`pytest`** (+ `pytest-asyncio`, `pytest-cov`) — testing
- **`import-linter`** (or a grep check) — enforce the `core/` ↛ `adapters/` boundary
- **pre-commit** hooks + CI — run the full check so bad code can't be committed/merged

Do not introduce LangGraph/CrewAI/LlamaIndex (design is a custom single-agent loop),
fine-tuning, or AWS Bedrock/AgentCore. These are explicitly out (design doc §19).

## 4a. Commands (use these EXACTLY — never improvise tool usage)

```
Install / sync deps:   uv sync
Add a dependency:      uv add <pkg>            # NEVER `pip install`
Add a dev dependency:  uv add --dev <pkg>
Run the app:           uv run <entrypoint>
Run all tests:         uv run pytest
Run tests w/ coverage: uv run pytest --cov
Lint + auto-fix:       uv run ruff check --fix
Format:                uv run ruff format
Type check:            uv run mypy .
Boundary check:        uv run lint-imports        # core/ must not import adapters/
FULL CHECK (pre-merge): uv run ruff check && uv run mypy . && uv run lint-imports && uv run pytest
```

Every agent (Claude Code, Codex, human) uses these exact commands. Dependencies are
added via `uv add` so the lockfile stays authoritative — the environment never drifts
between agents or sessions.

---

## 5. How to Work

**Build in the spec's build order (§0.4). One module at a time.**

For EACH module:
1. Read its spec section fully (interface, schema, behavior rules, acceptance criteria).
2. Check its dependencies are already built (per BUILD_STATUS.md).
3. Implement against the interface (respect the ports/adapters boundary).
4. Write **unit + integration + end-to-end tests** covering the module's acceptance
   criteria AND the invariant checks (§6). These ARE the definition of done.
5. Run the FULL CHECK (§4a): ruff + mypy + lint-imports + pytest. All green.
6. Only when everything in §6 passes: update `docs/BUILD_STATUS.md`, commit, move on.

**Do NOT:**
- Build multiple modules before verifying the earlier ones.
- Build backlog items: presence detection, per-user custom wake words, encryption
  at rest, external MCP integrations (e.g. OpenClaw), real authentication, per-user
  trait override admin UI. (The MCP-shaped registry and per-user override *storage*
  are in; the external integrations and admin surface are not.)
- Add dependencies not in §4 without asking.
- Guess at the fuzzy behavioral modules — see §7 below.

---

## 6. Definition of Done (per module) — FULL testing required

A module is NOT done until **all of the following pass**, not just "the code looks complete":

**All three test levels must exist and pass:**
1. **Unit tests** — module logic in isolation, dependencies/ports **mocked**. Fast, no
   real DB/API. Covers the module's behavior rules and most acceptance criteria.
2. **Integration tests** — module against **real dependencies** (real Qdrant/Mongo/
   Neo4j/Redis via docker-compose, real adapter). Catches store/provider wiring bugs
   that mocks hide.
3. **End-to-end tests** — module exercised through the **full path** it participates in
   (e.g. a real conversation turn that writes then retrieves memory through the
   assembled pipeline). Thin for early modules, fuller as modules connect.

**Plus these invariant checks (cross-cutting, every module that applies):**
- [ ] **Every acceptance-criteria checkbox** in the module's spec section passes as a test.
- [ ] **Multi-tenant isolation** — two-user test: user A's data never appears for user B
      (at integration + e2e level for any module touching user data).
- [ ] **Cost logging** — every paid op produced a Cost Ledger entry.
- [ ] **Ports boundary** — `lint-imports` confirms `core/` did not import `adapters/`.
- [ ] **Full check passes:** `uv run ruff check && uv run mypy . && uv run lint-imports && uv run pytest`.

Do not mark a module ✅ in BUILD_STATUS.md until every box above is green. If a test
level genuinely doesn't apply to a module, state why explicitly rather than skipping.

---

## 7. Modules that need human iteration (flag, don't decide)

Some modules are judgment-heavy — their correctness is about *feel and behavior*,
not passing a unit test. For these, implement the mechanism per spec, then STOP and
let me test/tune rather than deciding behavior yourself:
- Response Generation behavior gates (§12): curiosity gate, pull-based disclosure,
  overclaim rewrite — get the mechanism right; I tune the thresholds/wording.
- Psychological User-Model (§17) and Learning/Consolidation (§18): build the
  confidence-update and correlation machinery; I validate the inferences.
- Prompt Assembly (§10) budgeting/trimming and TTS tag placement (§23): mechanism
  yes, final tuning mine.

Build the mechanical modules (DB, memory stores, cost, tools, projects, queue,
audio pipeline) fully. Hand-off the behavioral/psychological tuning to me.

---

## 8. Session Rhythm

1. Read `docs/BUILD_STATUS.md` to see current state → know where we are.
2. I'll name the module (or you propose the next per build order §0.4).
3. Read that spec section → implement → write unit + integration + e2e tests → run the
   FULL CHECK (§4a) → show results.
4. On pass:
   - Update `docs/BUILD_STATUS.md` (status, Tests U/I/E column, Current module, Last updated).
   - Commit with a clear message referencing the spec section
     (e.g. "feat(memory): implement §5 Episodic Memory + unit/integration/e2e tests").
     **The commit history IS the session log** — no separate log file.
5. Next module.

`BUILD_STATUS.md` is the only file you routinely update while building. Git history
carries the "what happened" record. Keep commits scoped to one module where possible.
If a mid-build decision deviates from or clarifies the design doc / spec, note it in
the commit body AND flag whether the spec/design doc itself needs updating (don't
silently drift).