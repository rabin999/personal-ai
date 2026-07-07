# Personal AI Companion — MVP Build Specification

**Format:** LLM-consumable build spec. Each module is self-contained (purpose, store, schema, interface, behavior rules, dependencies, acceptance criteria) and can be built and verified independently. Build in the order given in §0.4.

**Companion design doc:** this spec assumes the reasoning/decisions in the separate *AI Companion Design Document*. This file is the *what to build*, not the *why*. Where a rule seems arbitrary, the design doc explains it.

---

# 0. Top Matter

## 0.1 What the MVP is

A **multi-user**, voice-first AI companion (Python), built **multi-tenant-ready**. Each user starts the app and talks to it; it listens with noise-robust audio handling, understands speech, detects emotional tone, remembers across sessions (episodic + semantic + procedural memory), learns the user's patterns over time, adapts its tone, manages long-lived "projects" (e.g. tracking stock trades) with consent-gated proactive insight, uses tools (including background web search), and speaks back with emotionally-appropriate delivery. Every money-costing operation is metered per user. Idle is nearly free.

**"MVP" here = the complete companion minus the backlog items** (§0.2) — not a thin slice. Full voice I/O, all memory layers, learning, psychological modeling, projects, tools, and cost tracking are all in scope, built multi-tenant-ready.

**Authentication (UPDATED — real Google SSO).** Originally a static bearer token
resolved to a static user record; that stub has been **replaced by real Google
OAuth2/OIDC (Authlib) + signed sessions**. On first sign-in a real `users` record
is created (our internal `user_id` mapped from the Google `sub`), the §2 profile is
seeded, and a welcome email is queued via a transactional outbox. Identity now
flows from a signed session cookie → `SessionUserContext` → `UserRecord`; the
`user_id`-scoped pipeline downstream is unchanged (exactly the §18 seam — only the
identity source swapped, `core/` untouched). See §26 and `docs/DEPLOYMENT.md §10`.

## 0.2 Scope

**IN (MVP):** User Context (static auth stub), audio input pipeline, VAD gating, streaming STT, semantic endpointing, barge-in, SER (voice emotion), all four memory layers, entity resolution, psychological user-model, learning & adaptation, self-model, prompt assembly, LLM routing via OpenRouter, response generation with behavior gates, Grok TTS output, tool dispatch (inline/background/action), web search (Serper + Brave fallback + cache), projects (types/instances/ledger/insight), background task queue, cost ledger, config & per-user profile with first-run sync, MCP-shaped tool registry. **All multi-tenant-ready (`user_id`-scoped everywhere).**

**OUT (backlog):** (1) presence detection, (2) per-user custom wake words (wake word dropped entirely — app is manually started), (3) encryption at rest, (4) external MCP integrations (e.g. OpenClaw) — the registry is MCP-shaped so these hook in later without rework, (5) **real authentication** (the static User Context §26 stands in), (6) per-user trait *override* admin/UI (the trait system is built to accept per-user overrides, but the management surface is deferred).

## 0.3 Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | **Python 3.11+** | Voice/memory/ML ecosystem is Python-first |
| Voice runtime | **Pipecat** (or LiveKit Agents) | Provides AEC, noise suppression, VAD, barge-in as pipeline stages |
| VAD | **Silero VAD** | Local, CPU, ~free |
| STT | via **OpenRouter** `/audio/transcriptions` (Whisper-class); or faster-whisper local | Streaming; vocab boosting |
| SER (voice emotion) | **emotion2vec** (self-hosted, small GPU instance) | Latency-tolerant; runs one turn behind |
| LLM | via **OpenRouter** `/chat/completions` | Complexity-tier routing, fallback |
| TTS | **Grok Voice TTS** via OpenRouter `/audio/speech` | Inline tags `[laugh] [sigh] [whisper] <emphasis> <slow> <pause>` |
| Doc/relational store | **MongoDB** | Config, profiles, projects, tasks, ledger, cost |
| Vector store | **Qdrant** | Episodic memory + entity pointers; hybrid dense+BM25 + RRF |
| Graph store | **Graphiti + Neo4j** | Semantic memory / relationships with temporal validity |
| Task queue | **Redis** (or Mongo-backed for MVP) | Background tasks |
| Search | **Serper** primary, **Brave** fallback | + query cache |
| Structured output | **Pydantic** | Validate every LLM JSON block |
| Auth (stub) | **Static bearer token → static user record** | No auth system built; see §26. Real auth = swap one adapter |
| Serving edge | **FastAPI (ASGI)** + SSE/WebSocket | Streams tokens + audio; resolves token → user_id |
| Deploy | Core app + voice runtime + background worker + SER (GPU) + 4 datastores | Modular monolith + separated edge services; no self-hosted large LLM |

## 0.4 Build Order

0. **Scaffold:** ports/adapters skeleton + FastAPI edge (see §0.6 Architecture). Stand up ports and the doc/vector/graph adapters as empty-but-wired shells first.
1. **Foundation:** Database Layer (§1) → **User Context / static auth (§26)** → Config & Profile (§2) → Cost Ledger (§3)
2. **Memory:** Working (§4) → Episodic (§5) → Semantic (§6) → Procedural (§7) → Entity Resolution (§8)
3. **Reasoning core (text-only first):** Self-Model (§9) → Prompt Assembly (§10) → LLM Router (§11) → Response Generation + behavior gates (§12)
4. **Tools & projects:** Tool Dispatcher (§13) → Background Queue (§14) → Web Search (§15) → Projects (§16)
5. **Learning:** Psychological User-Model (§17) → Learning & Adaptation / Consolidation (§18)
6. **Voice:** Audio Input Pipeline (§19) → STT (§20) → Endpointing (§21) → SER (§22) → TTS (§23) → Barge-in wiring (§24)

Rationale: user-scoping and cost tracking exist before any AI logic; get "remembers me and talks like a person" correct in text before adding voice; add learning once memory exists; add voice last (hardest to debug, benefits from a working core).

## 0.6 Architecture (summary — see design doc §17 for full)

**Shape:** modular monolith (the provider-agnostic `core/`) + separated services where runtime differs — **voice session runtime** (stateful, latency-critical), **background worker(s)** (async/slow, off the conversation path), **SER service** (GPU) — plus the four datastores and a thin **FastAPI serving edge**.

**Ports & adapters (hexagonal):** `core/` depends only on interfaces (`ports/`); providers (OpenRouter, Grok, Serper, Qdrant, Graphiti, and the static user-context) are `adapters/` wired at startup. `core/` never imports `adapters/`. This is why every module below is written as an interface — implementations are swappable via config.

**Directory scaffold:** `core/` (memory, reasoning, psych, projects, tools, cost, profile) · `ports/` · `adapters/` · `voice/` · `workers/` · `api/` · `services/ser_service/` · `tests/`. Full tree in design doc §17.3.

**Critical-path discipline:** the conversation path (voice ↔ core ↔ LLM) is short and synchronous; all slow work (search, consolidation/learning) is pushed to the workers/queue lane. This is what keeps latency low and cost bounded.

## 0.5 Global Conventions

- **Everything money-costing calls `CostLedger.log()`** (§3) after the call resolves, async, never blocking the response.
- **Every LLM JSON output is Pydantic-validated**; on validation failure, retry once, then fall back (§12).
- **Every retrieval, write, and cost entry is filtered/tagged by `user_id`.** This is a **multi-tenant isolation invariant**, not an optimization — one user's data must NEVER appear in another's context (prompt bleed). Matches Qdrant filtered-HNSW. The static user context (§26) does not weaken this; the path is fully user-scoped, only the identity source is stubbed.
- **`user_id` comes from the User Context (§26).** Every request resolves its bearer token → `UserRecord` at the API edge; `user_id` flows into the pipeline. Never hard-code `user_id` inside core logic — always take it from the resolved context.
- **Config is data, not code.** Behavior params live in the profile/registry (§2), tunable without code change.
- **Ports/adapters:** `core/` depends on interfaces only; providers are swappable adapters (§0.6).
- **The companion never speaks first.** No process emits user-facing output except in response to user input (one exception: consent-gated project insight, §16, which still asks before speaking).
- **Async-first.** Use `asyncio`. Slow work goes to the queue (§14), never blocks conversation.
- IDs are UUID strings. Timestamps are ISO-8601 UTC.

---

# 1. Module: Database Layer

### Purpose
Provide connection clients and a thin repository interface for the three stores. All other modules use this, never raw drivers directly.

### Stores
- MongoDB (config, profiles, projects, tasks, ledger, cost)
- Qdrant (episodic vectors + entity pointers)
- Neo4j via Graphiti (semantic graph)

### Interface
```
db.mongo(collection: str) -> Collection            # returns configured async collection handle
db.qdrant() -> QdrantClient                         # configured client
db.graphiti() -> Graphiti                           # configured Graphiti instance
db.healthcheck() -> {mongo: bool, qdrant: bool, graph: bool}
```

### Behavior Rules
1. Connections are pooled and reused (no per-call connect).
2. `healthcheck` MUST be callable at startup and fail loudly if any store is unreachable.
3. Qdrant collections created at startup if absent: `episodic` (dense+sparse vectors), `entities` (dense+sparse vectors). Both with `user_id` payload index for filtered search.

### Dependencies
- Config for connection strings (§2) — or env vars at bootstrap.

### Acceptance Criteria
- [ ] `healthcheck` returns all-true against running Mongo/Qdrant/Neo4j.
- [ ] Qdrant `episodic` and `entities` collections exist with sparse+dense vector config and a `user_id` payload index.
- [ ] Repeated calls reuse pooled connections (no connection leak under load test).

---

# 2. Module: Config & User Profile

### Purpose
Hold app-level defaults (trait registry, project types, provider config) and the per-user profile that is the live source of truth for one user's settings. Perform first-run sync (defaults → profile).

### Stores
MongoDB collections: `trait_defs`, `project_types`, `provider_config`, `user_profile`.

### Data Schema
```json
// user_profile
{ "_id": "user_id",
  "companion_name": "string | null",
  "audio_prefs": {
    "vad_threshold": 0.6, "vad_min": 0.4, "vad_max": 0.8,
    "endpoint_short_pause_ms": 700, "endpoint_long_pause_ms": 2500,
    "aec": true, "noise_suppress": true, "agc": true
  },
  "traits_enabled": { "curiosity_policy": true, "humor": true, "...": true },
  "comm_prefs": { "directness": 0.0-1.0, "emotional_scaffolding": 0.0-1.0 },
  "created_at": "iso", "onboarded": false }

// trait_defs (one per trait)
{ "_id": "curiosity_policy", "version": 3, "default_enabled": true,
  "description": "natural-language behavior spec injected into system prompt",
  "params": { "T_intent": 0.55, "T_novel": 0.7, "T_emotion": 0.6, "T_ambig": 0.65 } }
```

### Interface
```
profile.get(user_id) -> UserProfile
profile.first_run_sync(user_id) -> UserProfile   # instantiate from defaults if absent; set onboarded=false
profile.update(user_id, patch) -> UserProfile    # clamp audio values to [min,max] (§19)
registry.enabled_traits(user_id) -> list[TraitDef]  # resolve default_enabled ?? profile override
registry.project_types() -> list[ProjectType]
```

### Behavior Rules
1. On a user's first run, `first_run_sync` creates the profile from `trait_defs`/defaults; the DB profile is source of truth thereafter.
2. `profile.update` MUST clamp `vad_threshold` to `[vad_min, vad_max]`; a caller cannot set it outside the range.
3. Trait resolution: `effective_enabled = profile.traits_enabled[id] ?? trait_def.default_enabled`. Per-user override storage exists (multi-tenant-ready); the management surface/admin is backlog. Defaults enabled for now.
4. Changing a trait's behavior = editing `trait_defs.description`/`params` + bump `version`. No code change.

### Dependencies
- Database Layer (§1).

### Acceptance Criteria
- [ ] First run creates a profile with `onboarded=false`; second run returns the existing profile unchanged.
- [ ] `profile.update` rejects/clamps a VAD value above `vad_max`.
- [ ] `registry.enabled_traits` returns all default-enabled traits with their description + params.

---

# 3. Module: Cost Ledger

*(Full spec — see the example the author already approved.)*

### Purpose
Record every money-costing computation as one append-only entry; query total spend by any dimension; enable per-project budget caps.

### Store
MongoDB collection `cost_ledger` (append-only).

### Data Schema
```json
{ "_id": "ObjectId", "user_id": "string (required)",
  "component": "llm|stt|tts|tool|search (required)",
  "provider": "string (required)",
  "units": "object (required)  // {input_tokens,output_tokens}|{characters}|{seconds}|{queries}",
  "cost_usd": "number (required, >=0)  // 0 valid, e.g. cache hit",
  "timestamp": "iso (required)",
  "metadata": { "session_id": "string|null", "project_id": "string|null",
                "task_id": "string|null", "cache_hit": "bool (default false)" } }
```

### Interface
```
CostLedger.log(entry) -> None      # async, non-blocking, fire-and-forget
CostLedger.get(filter) -> { total_usd, count, breakdown? }   # $group aggregation
CostLedger.project_spend(user_id, project_id, from?, to?) -> float
```

### Behavior Rules
1. Every paid provider call MUST `log` after resolving.
2. Cache hit → `cost_usd: 0`, `metadata.cache_hit: true`.
3. `metadata` fields nullable.
4. Logging MUST NOT delay the user-facing response.

### Dependencies
- Database Layer (§1). Called by §11, §15, §20, §22, §23, §13.

### Acceptance Criteria
- [ ] One LLM call → exactly one entry with correct token units + cost.
- [ ] Cache hit → entry with cost 0 and cache_hit true.
- [ ] `project_spend` returns correct sum for a project over a date range.
- [ ] Ledger writes leave p95 response latency unchanged.

---

# 4. Module: Working Memory

### Purpose
Hold the current session's recent turns (the immediate conversational context) in memory for the duration of a session.

### Store
In-process (per active session), not persisted long-term. Flushed to episodic (§5) at session end.

### Interface
```
wm.append(session_id, turn: {role, text, timestamp, emotion?, meta?}) -> None
wm.recent(session_id, n=8) -> list[Turn]
wm.all(session_id) -> list[Turn]
wm.close(session_id) -> list[Turn]   # returns full transcript, clears buffer
```

### Behavior Rules
1. `recent` returns the last N turns for prompt assembly (§10 step 3).
2. Buffer is per-session; a new session starts empty.
3. On close, the full transcript is handed to Episodic Memory (§5) and Consolidation (§18); buffer cleared.

### Dependencies
- None (pure in-memory), but its close output feeds §5 and §18.

### Acceptance Criteria
- [ ] `recent(n=3)` returns exactly the last 3 turns in order.
- [ ] `close` returns the full transcript and subsequent `recent` returns empty.

---

# 5. Module: Episodic Memory

### Purpose
Store every conversation as timestamped, embedded chunks for later similarity retrieval ("when did we talk about X"). Ground-truth history.

### Store
Qdrant collection `episodic`: each point = dense vector (semantic) + sparse BM25 vector (keyword) + payload `{user_id, session_id, timestamp, text, emotion?}`.

### Interface
```
episodic.write(user_id, session_id, chunks: list[str], meta) -> None   # embed + upsert
episodic.retrieve(user_id, query_text, k=6) -> list[EpisodicHit]
   # hybrid: dense + BM25, RRF-fused, filtered by user_id, recency-weighted
```

### Behavior Rules
1. At session end, the transcript is **chunked** (semantic/turn-based chunking, not fixed-size) then embedded and written.
2. Retrieval MUST run dense + sparse (BM25) sub-queries and fuse with **RRF**; MUST filter by `user_id`; SHOULD weight recency.
3. Embedding model is configurable via provider_config.

### Dependencies
- Database Layer (§1), embedding model (via §11 provider config or a dedicated embedder).

### Acceptance Criteria
- [ ] Writing a transcript produces retrievable chunks scoped to the user.
- [ ] A query with an exact keyword (e.g. "SYPNL") retrieves the chunk via BM25 even if semantically distant.
- [ ] A paraphrased query retrieves the semantically relevant chunk via dense search.
- [ ] Results are RRF-fused (verify both signals contribute) and never include another user_id.

---

# 6. Module: Semantic Memory

### Purpose
Store durable facts and relationships about the user, with temporal validity (when a fact became true / was superseded). Answers "who is X", "what changed".

### Store
Graphiti + Neo4j. Entities, relationships, facts with `valid_from`/`valid_to`.

### Interface
```
semantic.add_episode(user_id, text, timestamp) -> None   # Graphiti extracts entities/relations
semantic.facts_for(user_id, entity_ids) -> list[Fact]    # facts + relationships + validity
semantic.profile_facts(user_id) -> list[Fact]            # stable facts about the user
```

### Behavior Rules
1. Fact extraction runs during consolidation (§18), not in the live path.
2. Superseded facts are marked with `valid_to`, NOT deleted — history of "what was true when" is preserved.
3. Retrieval returns validity windows so the reasoning layer knows a fact's current status.

### Dependencies
- Database Layer (§1), Graphiti.

### Acceptance Criteria
- [ ] Adding "my brother is X" then later "my brother is now called Y" yields the new fact as current and the old as superseded (valid_to set), both retrievable.
- [ ] `facts_for` returns relationships for a resolved entity with validity windows.

---

# 7. Module: Procedural Memory

### Purpose
Store learned behavioral rules ("when user says 'need a win', offer a concrete task"), updated over time with confidence.

### Store
MongoDB collection `procedural` (or graph nodes): `{user_id, rule_text, trigger, action, confidence, evidence_count, updated_at}`.

### Interface
```
procedural.rules_for(user_id, context?) -> list[Rule]   # high-confidence rules relevant to context
procedural.reinforce(user_id, rule, delta) -> None      # raise/lower confidence
procedural.add_candidate(user_id, rule) -> None          # new low-confidence rule
```

### Behavior Rules
1. A new rule starts low-confidence; `reinforce` moves confidence up on confirming evidence, down on contradicting.
2. Only rules above a confidence threshold are injected into prompts (§10 step 7).
3. Updated by Consolidation (§18), read by Prompt Assembly (§10).

### Dependencies
- Database Layer (§1).

### Acceptance Criteria
- [ ] A rule reinforced 5x consistently crosses the injection threshold; a contradicted rule drops below it.
- [ ] `rules_for` returns only above-threshold rules.

---

# 8. Module: Entity Resolution

### Purpose
Resolve vague references ("my trading thing", a person's name, a topic) to concrete stored IDs (project/person/topic). This is what makes "how's my trading thing" pull the right project.

### Store
Qdrant collection `entities`: dense + sparse vectors of each entity's name+description, payload `{user_id, entity_type, entity_id, name}`. Canonical data lives in Mongo/Graphiti; this is only a searchable pointer.

### Interface
```
entities.index(user_id, entity_type, entity_id, name, description) -> None
entities.resolve(user_id, phrase, k=3) -> list[EntityCandidate]  # hybrid dense+BM25, user-filtered
```

### Behavior Rules
1. When a project/person/topic is created or renamed, index/update its pointer here.
2. `resolve` runs hybrid (dense + BM25) filtered by `user_id`.
3. If top candidates are close in score → caller triggers a disambiguation question (§12); if one dominates → resolve silently.

### Dependencies
- Database Layer (§1). Feeds Prompt Assembly (§10 step 2).

### Acceptance Criteria
- [ ] "my trading thing" resolves to the finance project when only one exists.
- [ ] Two similar projects produce two close candidates (→ disambiguation path).
- [ ] Exact name match ("NEPSE Portfolio") resolves via BM25 even with semantic noise.

---

# 9. Module: Self-Model (Metacognition)

### Purpose
Track the system's own confidence, its past statements (for consistency), and catch/rewrite responses that overclaim feeling/consciousness before they reach the user.

### Store
MongoDB collection `self_model_log` + a Qdrant namespace (or reuse episodic with a flag) for the system's own prior statements.

### Data Schema
```json
{ "turn_id": "uuid", "user_id": "...", "timestamp": "iso",
  "confidence": 0.82, "facts_used": ["entity_id"],
  "novel_claim": false, "capability_boundary_flag": "null | overclaim_empathy | overclaim_consciousness",
  "self_reference": ["prior_turn_id"] }
```

### Interface
```
selfmodel.recall(user_id, query) -> list[PriorStatement]   # own prior relevant statements
selfmodel.log(turn_record) -> None
selfmodel.check_boundary(draft_text, judgment) -> {flagged: bool, rewritten_text?: str}
```

### Behavior Rules
1. `check_boundary` runs on every draft BEFORE TTS. If it detects overclaiming ("I understand exactly how you feel", implying felt emotion/consciousness), it rewrites to a validating-but-honest form ("that sounds really hard").
2. `recall` lets the companion reference its own past suggestions ("last time I suggested X") for consistency.
3. This is a *functional* self-model only — never labeled or surfaced as consciousness.

### Dependencies
- Database Layer (§1), LLM (§11) for the rewrite pass (can be same call as generation).

### Acceptance Criteria
- [ ] A draft containing "I understand exactly how you feel" is flagged and rewritten.
- [ ] `recall` surfaces a relevant prior statement for a repeated topic.
- [ ] Every turn produces one self_model_log entry.

---

# 10. Module: Prompt Assembly

### Purpose
Turn a resolved user utterance into the final LLM prompt by gathering all context layers in priority order and trimming to the context budget.

### Interface
```
assemble(user_id, session_id, utterance, emotion?) -> AssembledPrompt
```

### Behavior Rules (ordered pipeline)
1. **Transcript** already resolved (from STT §20 or text input) + confidence + optional emotion label.
2. **Entity resolution** (§8): resolve vague references → concrete IDs. Close candidates → return a disambiguation request instead of a prompt (halt).
3. **Working memory** (§4): recent turns.
4. **Episodic** (§5): hybrid RRF retrieval, top-k, recency-weighted.
5. **Semantic** (§6): facts/relationships for resolved entities + user profile facts.
6. **Project data** (§16): if a project resolved, fetch canonical ledger/metrics/pending insights from Mongo.
7. **Traits + config** (§2): enabled trait `description` blocks + comm prefs + gate params.
8. **Self-model** (§9): own relevant prior statements + confidence posture.
9. **Assemble + budget:** compose in priority order; trim to context window. Non-negotiable: current utterance + working memory + resolved entities. Trim first: older facts, extra episodic snippets.
10. Attach **complexity hint** for routing (§11) and the **emotion signal** for tone.

### Dependencies
- §2, §4, §5, §6, §7, §8, §9, §16.

### Acceptance Criteria
- [ ] A referenced project's ledger appears in the assembled prompt.
- [ ] Ambiguous entity → returns a disambiguation request, not a prompt.
- [ ] Over-budget input trims episodic snippets before dropping the current utterance/working memory.
- [ ] Assembled prompt includes enabled trait descriptions and any high-confidence procedural rules.

---

# 11. Module: LLM Router (OpenRouter Gateway)

### Purpose
Single gateway for LLM + STT + TTS via OpenRouter, with complexity-based model routing, fallback, and cost logging.

### Store
`provider_config` (Mongo): model IDs per tier, fallback order.

### Interface
```
llm.complete(prompt, tier: "simple|moderate|complex") -> {text, judgment, usage}
llm.embed(texts) -> vectors
router.route(complexity) -> model_id
```

### Behavior Rules
1. `tier` selects the model: simple→cheap, moderate→mid, complex→strong. Tier comes from the LLM's own `complexity_tier` judgment (§12) or a cheap first-pass classifier.
2. On provider error/timeout → fallback to next model in config; if all fail → structured error to caller.
3. Every call logs to Cost Ledger (§3) with token units + cost from OpenRouter's response.
4. STT/TTS also go through OpenRouter endpoints (see §20, §23) unless latency testing forces direct-to-provider for TTS.

### Dependencies
- OpenRouter API, Cost Ledger (§3), provider_config (§2).

### Acceptance Criteria
- [ ] A "simple" tier request hits the cheap model; "complex" hits the strong model.
- [ ] Simulated primary-provider failure triggers fallback and still returns a result.
- [ ] Every completion produces a cost-ledger entry with correct usage.

---

# 12. Module: Response Generation & Behavior Gates

### Purpose
Generate the companion's reply with the dual output (response + judgment block), then apply the behavior gates (curiosity, overclaim rewrite, disclosure) before handoff to TTS.

### Interface
```
generate(assembled_prompt) -> {final_text_with_tags, action}   # action = respond | clarify | curious_followup | disambiguate
```

### Data Schema (LLM judgment block — Pydantic-validated)
```json
{ "draft_response": "string (may contain TTS tags)",
  "judgment": { "intent_confidence": 0.0-1.0, "novelty_score": 0.0-1.0,
                "emotional_salience": 0.0-1.0, "ambiguity": 0.0-1.0,
                "complexity_tier": "simple|moderate|complex",
                "capability_boundary_flag": "null|overclaim_empathy|overclaim_consciousness" } }
```

### Behavior Rules
1. LLM returns draft + judgment in one call. **Validate with Pydantic**; on failure retry once, then fall back to a safe direct response.
2. **Curiosity gate:** `intent_confidence < T_intent` → CLARIFY; `novelty > T_novel AND salience > T_emotion` → CURIOUS_FOLLOWUP; `ambiguity > T_ambig AND high stakes` → CLARIFY; else DIRECT_RESPONSE. Thresholds from trait params (§2). (Baseline social warmth is always on — it's in the trait description, not gated.)
3. **Overclaim rewrite** (§9): if `capability_boundary_flag` set, rewrite before output.
4. **Disclosure (pull-based):** only if the utterance's intent requires honesty about the system's nature → one short sentence folded into the reply. Never volunteered.
5. Output text carries **TTS tags** at chosen positions (§23) based on the emotion signal + intended register.
6. Log the turn to Self-Model (§9) and Cost Ledger (§3).

### Dependencies
- §10, §11, §9, §2, §3. Emits to §13 (if tool calls) or §23 (TTS).

### Acceptance Criteria
- [ ] A low-confidence-parse utterance triggers CLARIFY, not a guessed answer.
- [ ] A familiar restated topic (low novelty) does NOT trigger a forced follow-up.
- [ ] "do you actually care about me" triggers a one-sentence disclosure; ordinary chat does not.
- [ ] An overclaiming draft is rewritten before it leaves the module.
- [ ] Malformed judgment JSON is caught by Pydantic and retried.

---

# 13. Module: Tool Dispatcher

### Purpose
Run the agentic loop: dispatch tool calls the LLM requests, by class (readonly inline / background queued / action confirmed), and feed results back until a direct response.

### Store
Tool registry (Mongo `tools` or in-code, MCP-shaped): `{id, type, latency_class, requires_confirmation, interruptible, scope}`.

### Interface
```
dispatch(tool_call, context) -> ToolResult | QueuedHandle | ConfirmRequest
loop(assembled_prompt) -> final_response   # ReAct-style until direct answer
```

### Behavior Rules
1. **Class handling:** `readonly` → run inline (fast, block briefly); `background` (latency_class slow) → enqueue (§14), continue conversation; `action` → return a confirmation request first, execute on user yes in a **non-interruptible** window.
2. **Latency dispatch:** `fast` → inline; `slow` → queue; `variable` → run with ~800ms budget, promote to queue if it overruns.
3. **Context-scoped injection:** only tools relevant to the current context (resolved project's tools + core set) are offered to the LLM — not all tools.
4. Registry is **MCP-shaped** (name, description, input schema, handler) so external MCP servers slot in later (backlog).
5. Each tool call logs to Cost Ledger (§3).
6. **Barge-in safety:** an in-flight `action` (write) tool MUST NOT be cancelled mid-execution; queued interruption handled after the write completes (§24).

### Dependencies
- §14 (queue), §3 (cost), §16 (project tools), §11 (loop LLM calls).

### Acceptance Criteria
- [ ] A readonly tool runs inline and its result feeds back into the same turn.
- [ ] A slow tool is enqueued and conversation continues without blocking.
- [ ] An action tool requests confirmation before executing.
- [ ] Only the referenced project's tools (plus core) are injected, not the full registry.
- [ ] A variable tool that overruns 800ms is promoted to the queue.

---

# 14. Module: Background Task Queue

### Purpose
Run slow/async tasks (web search, deep research) off the conversation path; deliver results back at a natural pause.

### Store
Redis (or Mongo) — task records: `{task_id, session_id, user_id, type, params, status, result, delivery_state, created_at, resolved_at}`.

### Interface
```
queue.enqueue(task) -> task_id
queue.status(task_id) -> Task
queue.pending_deliveries(session_id) -> list[Task]   # resolved but undelivered
queue.mark_delivered(task_id) -> None
```

### Behavior Rules
1. A worker executes queued tasks and updates `status` → `completed`/`failed`, sets `result`.
2. Delivery is **pull-at-pause:** when the conversation hits a natural pause, `pending_deliveries` is checked; a resolved task's result is handed to the LLM to compose a fresh interjection (§12 style, never templated), respecting relevance (drop if user moved on).
3. Cost of the task logs to Cost Ledger (§3).

### Dependencies
- Redis/Mongo, §15 (search runs here), §12 (interjection composed by LLM), §3.

### Acceptance Criteria
- [ ] A queued web search resolves without blocking the live conversation.
- [ ] Its result is delivered at the next natural pause via an LLM-composed (non-templated) line.
- [ ] A result whose topic the user abandoned can be suppressed.

---

# 15. Module: Web Search

### Purpose
Provide background web search with a cheap primary provider, a fallback, a cache, and a summarization pass.

### Store
`search_cache` (Redis/Mongo): `{query_hash, results, cached_at, ttl}`.

### Interface
```
search.run(query, user_id, session_id) -> {summary, sources}
```

### Behavior Rules
1. **Provider: Serper primary, Brave fallback** (config-swappable). Executed as a **separate detached call** in the background worker (§14) — NEVER inside the conversational LLM's generation (avoids blocking-generation trap).
2. **Cache first:** check `search_cache` by query hash + TTL (per-query-type TTL; short for time-sensitive like "market open today", long for stable facts). Hit → return cached, log cost 0 + `cache_hit:true` (§3).
3. **Summarize:** raw SERP → cheap-LLM pass (§11 simple tier) → distilled summary. Don't dump raw results into main context.
4. On primary failure → Brave fallback.
5. Log real search cost on a miss (§3).

### Dependencies
- Serper/Brave APIs, §11 (summarizer), §14 (runs as background task), §3.

### Acceptance Criteria
- [ ] A repeated query within TTL returns from cache with a $0 ledger entry.
- [ ] A miss hits Serper, summarizes, and logs real cost.
- [ ] Simulated Serper outage falls back to Brave.
- [ ] Search never blocks the conversational turn.

---

# 16. Module: Projects

### Purpose
Manage long-lived user workspaces (e.g. stock portfolio): dynamic types, per-user instances with an append-only ledger, derived metrics, and consent-gated proactive insight.

### Stores
Mongo: `project_types` (blueprints), `projects` (instances), `ledger_entries` (append-only), `pending_insights`. Entity pointer indexed in Qdrant (§8).

### Data Schema
```json
// project (instance)
{ "_id": "proj_id", "user_id": "...", "type": "finance_portfolio",
  "name": "My Stocks", "created_at": "iso" }
// ledger_entry
{ "_id": "...", "project_id": "...", "user_id": "...",
  "data": { "...fields per type schema..." }, "timestamp": "iso" }
// project_type (blueprint)
{ "_id": "finance_portfolio", "ledger_fields": [...], "derived_metrics": [...],
  "actions": [ {id, type, latency_class, requires_confirmation, schema} ],
  "insight_triggers": [...], "consent_required": true }
```

### Interface
```
projects.create(user_id, type, name) -> Project           # + index entity pointer (§8)
projects.log_entry(project_id, data) -> LedgerEntry         # action tool; confirm first
projects.state(project_id) -> {metrics, recent_entries, open_tasks}
projects.run_insight(project_id) -> Insight | None          # computes; stores as pending
```

### Behavior Rules
1. **Types are blueprints, instances are user data.** No hardcoded domain tools; a type declares its own actions (e.g. `log_entry`), registered dynamically only when an instance exists.
2. **Derived metrics** (P&L, drawdown, etc.) are computed from the ledger, not stored raw.
3. **Consent-gated insight:** on trigger (new entry / session start if referenced), `run_insight` computes and stores a `pending_insight` — NOT delivered. The companion asks permission first; on yes, delivers with factual framing + domain caveat (e.g. "not a financial advisor") + hands control back.
4. Creating/renaming a project updates its entity pointer (§8).
5. Per-project cost is queryable via Cost Ledger (§3).

### Dependencies
- Database Layer (§1), §8, §3, §13 (actions), §12 (consent phrasing).

### Acceptance Criteria
- [ ] Creating a finance project registers its `log_entry` action and an entity pointer.
- [ ] Logging a sell computes updated P&L from the ledger.
- [ ] An insight is stored as pending and only spoken after the user consents.
- [ ] `projects.state` returns metrics + recent entries for prompt assembly (§10 step 6).

---

# 17. Module: Psychological User-Model

### Purpose
Maintain a running, confidence-scored model of the user: personality (OCEAN), mood (valence/arousal) with a baseline, per-utterance emotion, and stage-of-change for behavior patterns.

### Store
Mongo `psych_model` (per user): trait estimates + confidence, mood baseline, current stage-of-change per tracked pattern.

### Data Schema
```json
{ "user_id": "...",
  "ocean": { "openness": {value, confidence}, "...": {} },
  "mood_baseline": { "valence": 0.0, "arousal": 0.0, "samples": 0 },
  "stages": { "social_withdrawal": "precontemplation|contemplation|preparation|action|maintenance" },
  "updated_at": "iso" }
```

### Interface
```
psych.get(user_id) -> PsychModel
psych.update_mood(user_id, valence, arousal) -> None      # rolls into baseline
psych.update_trait(user_id, trait, evidence) -> None       # confidence-weighted
psych.stage(user_id, pattern) -> Stage
```

### Behavior Rules
1. All trait estimates carry **confidence**; low early, rising only with consistent evidence. Never acted on above their confidence weight.
2. Mood updates maintain a **baseline** so deviations ("lower energy than usual") are detectable.
3. **Never diagnoses.** Correlations/inferences are hints, not clinical claims.
4. Stage-of-change gates behavior-nudge style (§12/design): don't push action-advice at a contemplation-stage user.
5. Updated by Consolidation (§18); read by Prompt Assembly (§10) and behavior gates.

### Dependencies
- Database Layer (§1); fed by §22 (emotion), §18 (consolidation).

### Acceptance Criteria
- [ ] A single signal moves a trait's confidence only slightly; repeated consistent signals raise it.
- [ ] Mood baseline updates and a below-baseline session is detectable.
- [ ] No output path emits a diagnosis.

---

# 18. Module: Learning & Adaptation (Consolidation)

### Purpose
The learning engine. After each session (async, off the critical path), extract facts, update patterns, refresh the mood baseline, run correlation analysis, and adjust confidence — turning raw events into learned, confidence-scored knowledge.

### Interface
```
consolidate(user_id, session_id, transcript, turn_signals) -> ConsolidationReport
```

### Behavior Rules (the two-loop model)
1. **Fast loop (in-session, cheap):** already handled — per-turn signals recorded by Self-Model (§9); immediate preferences applied within the session.
2. **Slow loop (this module, post-session, async):**
   a. **Extract semantic facts** from transcript → Semantic Memory (§6) with validity windows.
   b. **Detect interaction patterns** → Procedural Memory (§7): `reinforce` existing rules (up if consistent, down if contradicted), `add_candidate` for new ones.
   c. **Update mood baseline** (§17) with this session's emotional data.
   d. **Correlation analysis:** did a topic/time/context correlate with a mood/engagement shift? Store as a candidate pattern (needs repeated confirmation before it's acted on).
   e. **Confidence update:** adjust all inferred traits (§17) up/down based on new evidence (Bayesian-flavored — nudge, not overwrite).
3. **Confirmation gate:** a candidate pattern/rule is NOT injected into prompts until confirmed enough times (evidence_count threshold).
4. **Guardrails:** correlation flagged as correlation not causation; contradicting evidence lowers confidence rather than being ignored; never produces a diagnosis.
5. MUST run off the conversation critical path (triggered at session close; queue it).

### Dependencies
- §6, §7, §17, §9, §4 (transcript), §14 (runs async).

### Acceptance Criteria
- [ ] After a session stating a new fact, that fact is retrievable from Semantic Memory.
- [ ] A behavior repeated across sessions raises a procedural rule's confidence past the injection threshold.
- [ ] A contradicted prior belief has its confidence lowered, not left unchanged.
- [ ] A single correlation is stored as a candidate but not yet acted upon.
- [ ] Consolidation runs after session close without delaying any live response.

---

# 19. Module: Audio Input Pipeline

### Purpose
Capture mic audio and clean it (echo cancel, denoise, gain) before VAD/STT, with each stage independently toggleable for quality A/B testing, and a clamped-range VAD.

### Interface
```
audio.stream(session_id) -> async generator of clean frames
audio.set_stage(session_id, stage, enabled)   # aec | noise_suppress | agc
```

### Behavior Rules
1. Pipeline: `mic → AEC → noise_suppress → AGC → Silero VAD`. Built on **Pipecat/LiveKit** stages, not hand-wired.
2. Each of AEC / noise_suppress / AGC is **independently toggleable** (from profile §2) so STT WER can be measured with a stage on vs. off.
3. **AEC + barge-in dependency:** if barge-in is on and AEC is off, the config validator MUST warn (companion would transcribe its own TTS).
4. **VAD gate:** no speech detected → pipeline idle downstream, nothing paid runs (cost gate). VAD `threshold` is user-tunable but **clamped to `[vad_min, vad_max]`** (§2).
5. **On-demand ambient:** default ignores non-speech; on explicit user request ("what's that sound"), temporarily relax the gate, capture a bounded window, route to an audio-understanding model (§11 multimodal), then return to gated idle. Never continuous.

### Dependencies
- Pipecat/LiveKit, Silero VAD, §2 (prefs), §11 (ambient understanding).

### Acceptance Criteria
- [ ] With no speech, no STT/LLM/TTS calls fire (verify $0 ledger during silence).
- [ ] Toggling noise_suppress off changes the audio reaching STT (measurable WER delta on a noisy sample).
- [ ] Setting VAD threshold above `vad_max` is clamped.
- [ ] AEC-off + barge-in-on raises a config warning.

---

# 20. Module: STT Adapter

### Purpose
Transcribe speech to text with streaming partials, vocabulary boosting for the user's names/terms, and confidence signals.

### Interface
```
stt.transcribe_stream(audio_frames, vocab: list[str]) -> async partials + final {text, word_conf[], is_final}
```

### Behavior Rules
1. **Streaming** (partials feed endpointing §21). Via OpenRouter `/audio/transcriptions` (or faster-whisper local).
2. **Vocabulary boosting:** seed with the user's names/terms from Semantic Memory (§6) (e.g. Trishul, NEPSE, contact names) to reduce name errors.
3. Emit per-word confidence; low confidence on a critical word → clarification trigger (§12).
4. Prefer native `is_final`/turn-complete signal to assist endpointing.
5. Log cost to Cost Ledger (§3).

### Dependencies
- OpenRouter/faster-whisper, §6 (vocab), §3.

### Acceptance Criteria
- [ ] Streaming partials are emitted before the final transcript.
- [ ] A user-specific term (e.g. "NEPSE") is transcribed correctly with vocab boosting.
- [ ] Low per-word confidence is surfaced to the caller.

---

# 21. Module: Semantic Endpointing

### Purpose
Decide when the user has actually finished (vs. paused to think), combining silence duration with semantic completeness — so the companion never cuts the user off mid-thought.

### Interface
```
endpoint.should_respond(partial_transcript, silence_ms, prosody?) -> bool
```

### Behavior Rules
1. Combine **acoustic** (silence duration) + **semantic completeness** (is the sentence a complete thought?).
2. Dynamic threshold: **incomplete** sentence (trailing "and/because/so", filler) → wait `long_pause_ms` (~2500); **complete** → respond after `short_pause_ms` (~700). Thresholds per-user (§2), learnable (§18).
3. Never endpoint immediately after a filler word ("um", "uh").
4. Optional prosody signal (rising pitch = not done) once SER (§22) provides it.
5. The completeness check uses a cheap/fast signal (STT `is_final` or a cheap model), not the strong LLM.

### Dependencies
- §20 (partials), §2 (thresholds), optionally §22 (prosody).

### Acceptance Criteria
- [ ] "I was thinking about the parser and…" + a 2s pause does NOT trigger a response.
- [ ] "I was thinking about the parser." + a 0.8s pause DOES trigger a response.
- [ ] A filler word right before silence does not trigger endpointing.

---

# 22. Module: SER (Speech Emotion Recognition)

### Purpose
Detect the user's emotional tone from voice (prosody), producing an emotion/valence-arousal label to feed the reasoning and mood model.

### Store
Self-hosted **emotion2vec** microservice (small GPU instance), exposed internally.

### Interface
```
ser.analyze(audio_window) -> {valence, arousal, label, confidence}
```

### Behavior Rules
1. Runs on the user's utterance audio, producing a label combined with text-sentiment for a fuller emotion read.
2. **Latency-tolerant:** MAY run one turn behind (analyze utterance, feed label into the *next* turn) rather than blocking the live path.
3. Output feeds Prompt Assembly (§10 emotion signal), the mood model (§17), and optionally endpointing prosody (§21).
4. Treated as a probabilistic **signal, not ground truth** (design caveat) — never a diagnosis.

### Dependencies
- emotion2vec service; feeds §10, §17, §21.

### Acceptance Criteria
- [ ] An audibly low/tired utterance yields low valence/arousal.
- [ ] SER lagging one turn does not delay the live response.
- [ ] The emotion label reaches the psychological model (§17) and prompt assembly (§10).

---

# 23. Module: TTS Adapter

### Purpose
Speak the response with emotionally-appropriate delivery using inline tags, streamed with correct chunking.

### Interface
```
tts.speak(text_with_tags, voice) -> async audio stream
```

### Behavior Rules
1. **Grok Voice TTS** via OpenRouter `/audio/speech` (verify streaming latency; if too high for barge-in, allow direct-to-xAI).
2. Use **explicit inline tags** (`[laugh] [sigh] [whisper] <emphasis> <slow> <pause>`) at chosen word positions — NOT global emotion sliders (which are often inaudible). Tags are placed by §12 based on the emotion signal + intended register.
3. **Chunk at clause/sentence boundaries** before synthesis (never mid-tag) so tags aren't split; small latency cost, preserves prosody.
4. Interruptible: on barge-in (§24), stop the stream immediately.
5. Log cost (characters) to Cost Ledger (§3).

### Dependencies
- OpenRouter/xAI TTS, §12 (tagged text), §24 (interrupt), §3.

### Acceptance Criteria
- [ ] A `[whisper]`-tagged span is audibly different from surrounding speech.
- [ ] Tags are never split across synthesis chunks.
- [ ] A barge-in stops playback immediately.
- [ ] Character cost is logged per utterance.

---

# 24. Module: Barge-in & Interruption Handling

### Purpose
Let the user interrupt mid-response naturally, stopping output cleanly — while protecting in-flight write operations from corruption.

### Interface
```
interrupt.on_user_speech(session_id) -> None   # triggered by VAD during TTS
```

### Behavior Rules
1. On user speech detected during TTS playback: immediately **stop TTS** (§23) and **cancel in-flight LLM generation** for that turn.
2. New input processed fresh (§10) — may continue or shift topic.
3. **Write-safety:** if an `action` tool (§13) is mid-execution, the write MUST complete uninterrupted; the barge-in is queued and handled the instant the write finishes. Readonly/background work is safe to cancel.
4. Relies on AEC (§19) being on — otherwise the companion's own audio triggers false interrupts.

### Dependencies
- §19 (VAD/AEC), §23 (stop TTS), §11 (cancel generation), §13 (action-write protection).

### Acceptance Criteria
- [ ] Speaking over the companion stops its speech within a short bound.
- [ ] Interrupting during an action-tool write does NOT corrupt the write; the interruption is handled after.
- [ ] The companion's own TTS does not trigger a self-interrupt (AEC on).

---

# 25. Behavior Rules Reference (cross-cutting)

Collected non-negotiable rules enforced across modules (from the design doc):

1. **User speaks first** — no unprompted output (except consent-gated project insight, which still asks). Enforced in §12, §14, §16.
2. **Disclosure is pull-based** — one sentence, intent-triggered, never volunteered (§12).
3. **Warmth is always on; epistemic curiosity is gated** (§12).
4. **No overclaiming feeling/consciousness** — auto-rewritten (§9, §12).
5. **No forced dependency positioning** — contextual, never templated (§12).
6. **Correlation ≠ causation; never diagnose** (§17, §18).
7. **Idle is nearly free** — VAD gate blocks paid calls during silence (§19).
8. **Everything money-costing is logged, per user** (§3, global convention).
9. **All retrieval/write/cost is `user_id`-scoped — multi-tenant isolation invariant** (global convention). One user's data must never reach another's context.
10. **`user_id` always comes from the resolved User Context (§26), never hard-coded in core logic.**
11. **Config over code** — behavior tunable via profile/registry without code change (§2).

---

# 26. Module: User Context (static auth stub)

### Purpose
Resolve an incoming bearer token to a `UserRecord` (`user_id` + profile schema), so the whole pipeline runs fully `user_id`-scoped **without building any authentication**. Real auth later = swap this adapter.

### Store
None of its own. Reads static config for the token→user mapping; the resolved `user_id` keys into the Profile store (§2) and everything else.

### Data Schema
```json
// static token map (config/defaults or env)
{ "static_token_abc": "u_demo_001",
  "static_token_xyz": "u_demo_002" }

// UserRecord (returned by resolve)
{ "user_id": "u_demo_001",
  "companion_name": "Bro | null",
  "audio_prefs": { "vad_threshold": 0.6, "vad_min": 0.4, "vad_max": 0.8, "aec": true, "noise_suppress": true, "agc": true },
  "traits_enabled": { "curiosity_policy": true, "humor": true },
  "comm_prefs": { "directness": 0.8, "emotional_scaffolding": 0.2 } }
```

### Interface (Port: `ports/user_context.py`)
```
user_context.resolve(bearer_token: str) -> UserRecord   # raises Unauthorized if token unknown
```
Static adapter (`adapters/user_context/static.py`) implements `resolve` against the static token map, then loads/first-run-syncs the profile (§2) for that `user_id`.

### Behavior Rules
1. Every API request (§ serving edge) MUST carry a bearer token; the edge calls `resolve` and injects `user_id` into the request/session context. All downstream modules take `user_id` from there.
2. Unknown token → structured `Unauthorized` (no partial processing).
3. The resolved `UserRecord`'s profile fields are the same shape §2 uses — so the AI pipeline is identical to production; only identity is stubbed.
4. Provide ≥2 static tokens (e.g. `u_demo_001`, `u_demo_002`) so multi-tenant isolation can be verified by hand.
5. **This is the ONLY place identity is stubbed.** No other module may assume a single user or hard-code a `user_id`.

### Dependencies
- Config (§2 profile), Database Layer (§1). Used by the API serving edge and voice session bootstrap.

### Acceptance Criteria
- [ ] A known static token resolves to the correct `UserRecord` with `user_id` and profile.
- [ ] An unknown token returns `Unauthorized` and no pipeline work runs.
- [ ] Two different tokens resolve to two different `user_id`s; their memories/projects never cross (isolation test).
- [ ] Swapping the static adapter for a stub returning a different `user_id` requires zero changes in `core/`.

---

*End of MVP build specification.*