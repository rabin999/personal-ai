# Project: Personal AI Companion

A multi-user, voice-first AI companion built multi-tenant-ready. This file is your
persistent operating contract. Read it fully at the start of every session.

**Prime directive:** serve the REAL app requirements in the design doc and spec — a companion
that thinks before it replies, reflects on its own output, remembers properly, and sounds
human. These docs describe INTENT. Where a rule here or a line in the docs would block you
from actually satisfying that intent, the intent wins: make the sensible decision, log it,
and keep doing real work. Do NOT let a fixed line of process stop you from real
implementation or real testing.

---

## 1. Source of Truth (read before coding)

- `docs/ai-companion-design-doc.md` — the **WHY**: architecture, decisions, rationale.
- `docs/ai-companion-mvp-build-spec.md` — the **WHAT**: modules with interfaces, schemas,
  behavior rules, acceptance criteria.
- `docs/BUILD_STATUS.md` — current progress (state). Read first each session; update when a
  module is done. The ONLY file you routinely update while building.
- **Git history is the session log.** Each commit (referencing its spec module) is the record.

**Rules about the docs:**
- Before implementing ANY requirement, re-read the exact design/spec line and ask:
  "what REAL interaction would prove this is satisfied the way a human would judge it?"
  Build to pass THAT — never to pass a mock.
- If code and spec disagree, the spec wins.
- If the spec/design is vague, contradictory, or self-blocking (e.g. "YAML or JSON or DB"),
  make ONE sensible decision, note it in the commit body + REMEDIATION_LOG.md, and continue.
  Do not stall, and do not silently drift.
- Act on the docs; don't restate them back to me.

---

## 2. What this app IS — the behavior that must actually work

This is the point of the whole project. The companion must:

- **Think before it replies (ReAct).** Perceive the input -> reason about it -> decide whether
  it needs memory/tools -> act -> observe -> and only then respond. Not a reflexive one-shot
  generation. The reasoning step is real and happens every turn.
- **Self-reflect before finalizing.** Before the reply goes out, the agent critiques its own
  draft against: the response standard (warm, human, short, companion-not-assistant), whether
  it overclaimed feeling, whether it duplicated anything, whether the format is right, and
  whether it actually used the memory/context it should have. If the draft fails, it revises.
  Self-reflection is a first-class step, not a bolt-on.
- **Assemble context properly.** Every turn READS memory before reasoning (working + episodic
  + semantic + procedural + relevant project data), assembles it into the prompt with the
  active traits, reasons, responds, then WRITES (extraction/consolidation) after acting.
- **Control memory deliberately.** Decide what is worth storing and WHERE (episodic vs.
  semantic vs. procedural), distill episodes into durable facts over time, and NEVER re-store
  something that is merely being recalled. Memory correctness is core, not incidental.
- **Sound like the companion.** Warm, curious about the person, concise, human. NEVER generic
  assistant-speak ("How can I help you?", "What's on your mind?"). Disclosure is pull-based,
  one sentence, only when the question demands it. No ToS-style disclaimers, no overclaiming
  feeling.
- **Use its traits.** The behavioral traits we defined (curiosity policy, humor, emotional
  intelligence, moral framework, self-model, etc.) are composed into the system prompt from
  config and actually shape the response — not decorative.

If any process rule below gets in the way of the above, the above wins.

---

## 3. Non-Negotiable Invariants (these never bend — they are safety/correctness)

Unlike process rules, these are hard. Violating any is a defect even if tests pass:

1. **Multi-tenant isolation.** Every retrieval/write/cost entry is `user_id`-scoped. One
   user's data must NEVER appear in another user's context.
2. **`user_id` comes from the resolved User Context (§26)** — never hard-coded in `core/`.
3. **Ports & adapters boundary.** `core/` depends ONLY on `ports/`; never imports `adapters/`.
4. **Everything money-costing logs to the Cost Ledger**, async, never blocking the response.
   Cache hits log $0.
5. **Every LLM JSON output is Pydantic-validated.** Fail -> retry once -> safe fallback.
6. **Config over code.** Behavior params (thresholds, trait descriptions, provider/model
   choice) live in config, not hard-coded.
7. **The companion never speaks first** (except consent-gated project insight, which asks first).
8. **Idle is nearly free.** VAD gate blocks all paid calls during silence.
9. **Never diagnose; correlation != causation.** Emotion/psych inferences are signals, not claims.
10. **Async-first.** Slow work goes to the queue, never blocks the conversation path.

**Agentic where judgment helps; rigid where safety demands it.** The reasoning, memory
decisions, tool orchestration, and self-correction SHOULD be a deep agentic loop — not a
shallow fixed pipeline. But the invariants above are deterministic and always enforced —
never left to agent discretion. Get both halves right.

---

## 4. Architecture

Modular monolith (`core/`, provider-agnostic) + separated services where runtime differs:
- **voice/** — real-time session runtime (stateful, latency-critical)
- **workers/** — background/async (consolidation/learning, background search)
- **services/ser_service/** — SER (emotion2vec), GPU
- **api/** — thin FastAPI edge (SSE/WebSocket streaming; resolves token -> user_id)

`core/` -> `ports/` (interfaces) -> `adapters/` (concrete, swappable). Follow design doc §17.3.

---

## 5. Tech Stack — use the REQUIRED tools; do not reinvent them

- **Language:** Python 3.11+ · **Serving:** FastAPI · **Async:** asyncio · **Env:** `uv` (never bare pip)
- **Voice + barge-in:** **Pipecat** — use its pipeline/transport/FrameProcessor for the voice
  loop, VAD gate, endpointing, and barge-in. Do NOT hand-wire these (hand-rolling voice is a
  known past failure and why interruption is unreliable).
- **VAD:** Silero · **STT:** OpenRouter (or faster-whisper local) · **SER:** emotion2vec
- **LLM:** OpenRouter (complexity-tier routing, fallback) · **TTS:** Grok Voice TTS (inline tags)
- **Doc store:** MongoDB · **Vector (episodic):** Qdrant (dense+BM25+RRF, filtered-HNSW)
- **Semantic/temporal memory:** **Graphiti + Neo4j** — REQUIRED, not hand-rolled. Its
  retrieval MUST actually return facts (verify with a real call; do not assume).
- **Personalization memory:** **Mem0** — wire it into the live loop (prompt assembly), not
  just installed. Reconcile with the custom extraction step so they don't double-store.
- **Tracing:** full per-turn trace is CORE (not later-phase): per-LLM-call token/cost/latency,
  tool calls, retrieval steps, and the self-reflection step as its own span, grouped by
  session_id. Prefer **Langfuse**; a hand-rolled trace must be equally complete.
- **Queue/cache:** Redis · **Search:** Serper (primary) + Brave (fallback) + cache
- **Validation:** Pydantic · **Auth:** none — static bearer token -> static user record (§26)

**Dev toolchain (config in `pyproject.toml`):** ruff (lint+format), mypy, pytest
(+asyncio, +cov), import-linter (core !-> adapters). Pre-commit runs FAST checks only (ruff).

**Excluded unless you ask me first:** LangGraph, CrewAI, LlamaIndex, AutoGen, fine-tuning,
Bedrock/AgentCore (design is a custom single-agent loop). If one is genuinely the right fix,
STOP and make the case in REMEDIATION_LOG.md — don't silently add it. If it's named as
REQUIRED above (Graphiti/Mem0/Pipecat/Qdrant/Langfuse), wire it in properly — reinventing it
is a defect.

### 5a. Commands (use exactly)
```
Sync deps:              uv sync
Add dep / dev dep:      uv add <pkg>   /   uv add --dev <pkg>     # never pip install
Fast check (anytime):   uv run ruff check --fix && uv run ruff format
Types / boundary:       uv run mypy .   /   uv run lint-imports
Logic tests:            uv run pytest -m "not real_call"
Real-call GenAI tests:  uv run pytest -m real_call                # real model + real stores
FULL CHECK (per bundle):uv run ruff check && uv run mypy . && uv run lint-imports && uv run pytest
```

### 5b. Testing cadence (do not waste time)
- `ruff`: run freely, anytime — instant.
- FULL CHECK / real-call suite: run ONCE after a completed BUNDLE. NEVER per edit, per format,
  or per commit. Mid-bundle, keep building.

---

## 6. Definition of Done — REAL testing for a GenAI app (not mock theater)

This is a GenAI companion, not a CRUD app. Unit tests that mock the LLM and pass while the app
gives bad responses are worthless. A module/bundle is done only when:

- **Logic tests (mock ok):** pure logic (parsing, routing, clamping, schema) in isolation.
- **Integration tests (REAL stores):** against real Qdrant/Mongo/Neo4j/Redis via
  docker-compose — catches wiring bugs mocks hide (e.g. Graphiti writing malformed edges).
- **Real-call end-to-end (NO mocking the model or stores):** the core
  memory/reasoning/response/tool loop tested with REAL model calls + REAL datastores. Mocking
  the LLM here proves nothing. Mark `@pytest.mark.real_call`.
- **Response-QUALITY evaluation (the part that was missing):** for every behavioral
  requirement, send a REAL message, capture the REAL response, JUDGE it:
  - Deterministic assertions for hard rules: banned assistant-speak absent, disclosure never
    proactive, no duplication, no cross-user leak, no double-write on recall turns, self-
    reflection actually ran.
  - LLM-as-judge for subjective quality: a pinned separate model scores the response against
    the design's response standard (warm/human/short/companion). Keep a small human-calibration
    set. Use community-standard approaches (LLM-as-judge; RAGAS for retrieval) — don't invent
    a worse scheme.
  - Rule of thumb: **if a human can tell in one read that "hi -> 'How can I help you?'" is wrong,
    your test MUST catch it automatically.** Build exactly that.

**Invariant checks (every applicable module):** acceptance criteria pass · two-user isolation ·
cost logged · ports boundary clean · FULL CHECK green.

**Proof by conversation is the highest bar.** For core changes, capture a real conversation
(store a fact -> new session -> recall; "top 2 news" -> 2 distinct once) and show the actual
responses + the trace (did it read memory, reason, self-reflect, how many steps, which model,
cost). "Tests pass" without a captured real conversation is NOT done for the core loop. Never
report a timeout or skipped verification as success.

---

## 7. Behavioral modules — build the mechanism AND verify the quality yourself

Judgment-heavy modules (response/tone §12, psych §17, learning §18, prompt assembly §10,
TTS tags §23): build the mechanism AND verify the OUTPUT QUALITY with real judged calls (§6)
before claiming done. You are responsible for catching "this sounds like a generic assistant,
not our companion." Do NOT punt response quality to me — the automated quality tests must catch
bad output. I do FINAL fine-tuning of thresholds/wording; the baseline must already be good and
proven. Build the mechanical modules (DB, memory stores, cost, tools, queue) fully.

---

## 8. Session Rhythm

1. Read `docs/BUILD_STATUS.md` -> know current state.
2. I name the module (or you propose next per build order).
3. Read the spec/design section -> implement (REQUIRED tools, not reinvented; think before
   coding) -> write §6 tests (real-call + judged for the core loop) -> FULL CHECK once per
   bundle -> show results INCLUDING a captured real conversation for core modules.
4. On pass: update `BUILD_STATUS.md`; commit referencing the spec section
   (e.g. "feat(memory): §5 Episodic + real-call tests"). Git history is the session log.
5. Next module.

If a mid-build decision deviates from or clarifies the docs, note it in the commit body and
flag whether the design/spec itself needs updating — don't silently drift.

---

## 9. Discipline (read every session)

- **Think before you reply/implement.** Re-read the exact requirement; ask "what real
  interaction proves this?"; build to that.
- **ReAct + self-reflection are real steps**, every turn — reason before responding, critique
  the draft before finalizing, revise if it fails the standard.
- **Real calls, not mocks, for the core loop.** Judge the actual response. Bad responses FAIL a test.
- **Prove by conversation.** Capture and show real output + trace for core changes. No "fixed"
  without proof.
- **Use the required tools; don't reinvent** (Graphiti, Mem0, Pipecat, Qdrant, Langfuse-or-equal).
- **Requirements over rigid lines.** If a process rule blocks real work or real testing, make
  the sensible call, log it, and keep going. Serve the app's actual intent.
- **Efficient cadence.** Fast ruff checks anytime; heavy/real-call suite once per completed bundle.