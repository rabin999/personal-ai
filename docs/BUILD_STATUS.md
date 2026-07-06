# Build Status

**Purpose:** running progress tracker so any new session knows exactly where the
build is without re-explaining. Read this first each session; update it when a
module's acceptance criteria pass.

**Legend:** ⬜ not started · 🟨 in progress · ✅ done · ⏸️ blocked

**Done (✅) means:** unit + integration + e2e tests pass, all acceptance criteria pass,
isolation + cost-logging + ports-boundary checks pass, and `uv run ruff check && mypy .
&& lint-imports && pytest` is green. (See CLAUDE.md §6.) Track test levels per module
with the U/I/E markers in the Tests column (e.g. `U✅ I✅ E🟨`).

**Last updated:** 2026-07-06
**Current module:** _(§14 ✅ — next: §15 Web Search)_

---

## Phase 0 — Scaffold & Tooling
_(Setup items — verified by "does it run / does CI pass", not unit tests.)_

| Status | Item | Ref | Notes |
|---|---|---|---|
| ✅ | `uv` project init + `pyproject.toml` (deps + ruff/mypy/pytest config) | spec §0.3 | uv + lockfile; voice stack (Pipecat/Silero) declared as optional `voice` extra, installed in Phase 6 |
| ✅ | Pre-commit hooks + CI (ruff, mypy, lint-imports, pytest) | CLAUDE §4a | Local pre-commit hooks + `.github/workflows/ci.yml`, both run the FULL CHECK |
| ✅ | docker-compose for local deps (Mongo/Qdrant/Neo4j/Redis) | — | `docker compose up -d`; defaults in `config/settings.py` match it |
| ✅ | Project scaffold (dirs, empty `ports/`) | design §17.3 | All 10 ports stubbed; import-linter enforces core ↛ adapters; ports pure (no core/adapters imports) |
| ✅ | FastAPI serving edge skeleton (token→user_id wiring point) | spec §0.6, §26 | `api/deps.get_user_record` resolves bearer → UserRecord via port; 501 until §26 adapter wired; smoke-tested |

## Phase 1 — Foundation
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ✅ | §1 Database Layer | spec §1 | — | U✅ I✅ E✅ | `adapters/db.py`; pooled clients, fail-loud startup, Qdrant collections (dense+sparse, user_id index); Graphiti lazily wired to OpenRouter (models finalized in §6) |
| ✅ | §26 User Context (static auth) | spec §26 | §1, §2 | U✅ I✅ E✅ | Static map in `config/defaults/static_users.json` (2 users); resolve → first-run sync; e2e over HTTP via CurrentUser dep; app lifespan wires adapters |
| ✅ | §2 Config & User Profile | spec §2 | §1 | U✅ I✅ E✅ | Built before §26 (its dependency). `traits_enabled` stores overrides only (?? default per rule 3). Seed trait descriptions are DRAFT — human tuning pending (§7 hand-off) |
| ✅ | §3 Cost Ledger | spec §3 | §1 | U✅ I✅ E✅ | `core/cost`; fire-and-forget log (p95 latency test), $group aggregation + breakdown, project_spend w/ ISO range, user-scoped queries, failed writes swallowed |

## Phase 2 — Memory
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ✅ | §4 Working Memory | spec §4 | — | U✅ I– E✅ | `core/memory/working.py`; pure in-memory so integration n/a; e2e via §5 memory-flow test |
| ✅ | §5 Episodic Memory | spec §5 | §1 | U✅ I✅ E✅ | fastembed local embedder (bge-small, 384d) + Qdrant/bm25 sparse; RRF via query_points; turn-based chunking; gentle recency half-life; isolation verified |
| ✅ | §6 Semantic Memory | spec §6 | §1 | U✅ I✅ E✅ | Graphiti group_id=user_id; gemini-2.5-flash extraction (gpt-4.1-mini dropped edges), temp 0, json_object mode; fastembed embedder; LLM usage → Cost Ledger via httpx hook; paid tests skip loudly without OPEN_ROUTER_API_KEY |
| ✅ | §7 Procedural Memory | spec §7 | §1 | U✅ I✅ E✅ | Confidence 0.3 start / 0.6 injection threshold / ±delta clamp [0,1]; context filter by trigger words; e2e arrives with §10/§18 (integration covers today's full path — stated per contract §6) |
| ✅ | §8 Entity Resolution | spec §8 | §1, §5 | U✅ I✅ E— | Deterministic point ids (rename = in-place update); is_ambiguous helper (runner-up ≥0.8×top) for §12's disambiguation; e2e arrives with §10 step 2 (integration covers today's full path) |

## Phase 3 — Reasoning Core (text-only first)
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ✅ | §9 Self-Model (metacognition) | spec §9 | §1, §11 | U✅ I✅ E✅ | `self_statements` Qdrant collection (§9's namespace); heuristic overclaim patterns + judgment flag → LLM rewrite (retry once → safe fallback); patterns/wording flagged for human tuning; per-turn-log e2e green via §12 turn |
| ✅ | §10 Prompt Assembly | spec §10 | §2,4,5,6,7,8,9,16 | U✅ I✅ E✅ | Ordered pipeline + char-budget trimming (episodic→facts→self→project→rules; utterance/WM/entities untouchable); §16 injects via ProjectContextProvider protocol (stubbed); heuristic complexity hint; e2e green via §12 turn |
| ✅ | §11 LLM Router (OpenRouter) | spec §11 | §3, §2 | U✅ I✅ E✅ | Built before §9/§10 (their dependency). Tier chains in provider_config `llm_router`; exact cost via OpenRouter usage accounting; embed() is local fastembed; e2e via §12 turn |
| ✅ | §12 Response Gen + behavior gates | spec §12 | §10,§11,§9 | U✅ I✅ E✅ | ⚠️ mechanism complete, thresholds/wording await human tuning: curiosity gate (trait params), overclaim rewrite, pull-based disclosure, Pydantic retry→safe fallback; full-turn e2e green (memory recall via real LLM) |

## Phase 4 — Tools & Projects
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ⬜ | §13 Tool Dispatcher | spec §13 | §14,§3,§11 | ⬜ | inline/background/action; MCP-shaped; context-scoped |
| ✅ | §14 Background Task Queue | spec §14 | Redis | U✅ I✅ E✅ | Built before §13 (its dependency). Redis lists+JSON records; TaskWorker handler registry; DeliveryComposer: LLM judges relevance → interject or suppress (never templated) |
| ⬜ | §15 Web Search | spec §15 | §11,§14,§3 | ⬜ | Serper→Brave; cache; summarize; detached call |
| ⬜ | §16 Projects | spec §16 | §1,§8,§3,§13 | ⬜ | Types vs instances; ledger; consent-gated insight |

## Phase 5 — Learning
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ⬜ | §17 Psychological User-Model | spec §17 | §1 | ⬜ | ⚠️ behavioral — OCEAN/mood/stage; confidence-scored; never diagnose |
| ⬜ | §18 Learning & Adaptation | spec §18 | §6,§7,§17,§9,§14 | ⬜ | ⚠️ behavioral — two-loop; confidence update; confirmation gate |

## Phase 6 — Voice
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ⬜ | §19 Audio Input Pipeline | spec §19 | Pipecat/LiveKit | ⬜ | Toggleable stages; VAD clamp; idle-is-free gate |
| ⬜ | §20 STT Adapter | spec §20 | §6,§3 | ⬜ | Streaming; vocab boost; word confidence |
| ⬜ | §21 Semantic Endpointing | spec §21 | §20,§2 | ⬜ | Silence + completeness; filler-aware |
| ⬜ | §22 SER Service | spec §22 | emotion2vec | ⬜ | ⚠️ behavioral input — GPU; latency-tolerant; one turn behind |
| ⬜ | §23 TTS Adapter | spec §23 | §11,§12,§3 | ⬜ | Grok tags; clause chunking; interruptible |
| ⬜ | §24 Barge-in & Interruption | spec §24 | §19,§23,§11,§13 | ⬜ | Write-safety on action tools |

---

## Cross-cutting checks (verify periodically, not one-time)
- [ ] No `core/` file imports from `adapters/` (grep / lint-imports to confirm).
- [ ] Every store query includes a `user_id` filter (isolation).
- [ ] Two-user isolation test passes (§26 acceptance): user A never sees user B's data.
- [ ] Every paid-provider call has a corresponding Cost Ledger entry.
- [ ] No backlog items were built (presence, custom wake word, encryption, external MCP, real auth).

---

## Notes on history
This file tracks **current state only** (what's done, right now). The record of *what
happened each session* is the **git commit history** — each module is committed with a
message referencing its spec section. There is no separate session-log file to keep in
sync. Update this file when a module's status changes; let git carry the history.