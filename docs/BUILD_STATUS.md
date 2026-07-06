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
**Current module:** _(Phase 0 scaffold ✅ — next: §1 Database Layer)_

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
| ⬜ | §1 Database Layer | spec §1 | — | ⬜ | Mongo/Qdrant/Neo4j clients + healthcheck; create Qdrant collections |
| ⬜ | §26 User Context (static auth) | spec §26 | §1, §2 | ⬜ | Static token→UserRecord; ≥2 tokens for isolation test |
| ⬜ | §2 Config & User Profile | spec §2 | §1 | ⬜ | First-run sync; VAD clamp; trait registry |
| ⬜ | §3 Cost Ledger | spec §3 | §1 | ⬜ | Append-only; per-user/project aggregation; cache-hit $0 |

## Phase 2 — Memory
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ⬜ | §4 Working Memory | spec §4 | — | ⬜ | In-session buffer |
| ⬜ | §5 Episodic Memory | spec §5 | §1 | ⬜ | Qdrant hybrid dense+BM25+RRF, user-filtered, chunking |
| ⬜ | §6 Semantic Memory | spec §6 | §1 | ⬜ | Graphiti; temporal validity (supersede, don't delete) |
| ⬜ | §7 Procedural Memory | spec §7 | §1 | ⬜ | Confidence-scored rules |
| ⬜ | §8 Entity Resolution | spec §8 | §1, §5 | ⬜ | Qdrant entity pointers; disambiguation path |

## Phase 3 — Reasoning Core (text-only first)
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ⬜ | §9 Self-Model (metacognition) | spec §9 | §1, §11 | ⬜ | Overclaim rewrite; self-reference recall |
| ⬜ | §10 Prompt Assembly | spec §10 | §2,4,5,6,7,8,9,16 | ⬜ | 12-step pipeline; user-scoped; budgeting |
| ⬜ | §11 LLM Router (OpenRouter) | spec §11 | §3, §2 | ⬜ | Complexity tiering; fallback; cost logging |
| ⬜ | §12 Response Gen + behavior gates | spec §12 | §10,§11,§9 | ⬜ | ⚠️ behavioral — mechanism only, human-tuned |

## Phase 4 — Tools & Projects
| Status | Module | Spec ref | Depends on | Tests (U/I/E) | Notes |
|---|---|---|---|---|---|
| ⬜ | §13 Tool Dispatcher | spec §13 | §14,§3,§11 | ⬜ | inline/background/action; MCP-shaped; context-scoped |
| ⬜ | §14 Background Task Queue | spec §14 | Redis | ⬜ | Pull-at-pause delivery |
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