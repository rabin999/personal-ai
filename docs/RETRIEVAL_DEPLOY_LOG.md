# Verified-retrieval — integration & production deploy log

A step-by-step, traceable record of Phase A: taking the sub-agent's verified-retrieval
module from an isolated branch to **wired into the engine and running in production**.
Scope: **core engine only** — no voice I/O. Each step records what ran, the result, and any
decision made. Newest section appended at the bottom.

## Inputs (what the sub-agent delivered)

- Branch `feat/verified-retrieval-crawl4ai` (pushed, not merged). `ports/retrieval.py`
  **unchanged** — no contract change.
- Files: `adapters/retrieval/*` (10), `deploy/crawl4ai/docker-compose.yml`,
  `tests/retrieval/*`, `docs/VERIFIED_RETRIEVAL_REPORT.md`. **No new Python dependency**
  (the app talks to the Crawl4AI Docker service over HTTP via `httpx`).
- Crawl4AI: **Docker service on `127.0.0.1:11235`, pinned `0.8.6`** (0.9.1 image ships
  without the chromium-headless-shell binary and crashes; 0.8.6 has the identical API).
- Sub-agent harness: **31 deterministic + 8 real-call tests pass**; ruff/mypy/lint-imports
  clean; 4 cardinalities + live headline case ("Balendra Shah", corroborated across 2 recent
  domains) proven; NEPSE market fact → honest `status=error`, **no fabrication**.
- Honest limit: fetch latency **median ~13 s** → background/waiter path only (fits the
  existing `web_search` background tool, `inline_timeout_s=20`).
- Public API: `build_crawl4ai_retrieval(search=, llm=, user_id=, ledger=, session_id=,
  fetcher=, logs=, trace_store=) -> RetrievalPort`.

## Plan (gated)

1. Squash-merge the branch → `main` (clean commit, no Claude trailer); scrub the 2 reintroduced
   `CLAUDE.md` comment refs (`config.py`, `trace.py`).
2. **Integrate** — wire the engine's `web_search` tool to prefer verified retrieval, falling
   back to the existing Serper snippet search on `status="error"` (no regression: worst case =
   today's behaviour). Per-call builder (multi-tenant `user_id` from `ToolContext`), one shared
   `Crawl4AIClient`.
3. **Verify full flow locally** (real `/api/chat` engine turn → dispatch → verified retrieval →
   grounded answer). **GATE: deploy only if this works.**
4. **Deploy to prod** (`202.58.120.93`): Crawl4AI service per `deploy/crawl4ai/` + full app
   deploy (`update.sh`), with `CRAWL4AI_*` env set (token + jwt_enabled + loopback).
5. **Verify in production** for real via the core-engine path; report working-or-not honestly.

## Decisions

- **D1 — Integration shape:** `web_search` tool prefers `RetrievalPort.verify()`, degrades to
  the existing `WebSearch.run()` (Serper snippet) on error/crawler-down. Rationale: the design
  wants "read + verify, not snippets", but a hard cutover risks regressing live search if
  Crawl4AI is unhealthy in prod; the fallback bounds the worst case to today's behaviour.
- **D2 — Latency:** verified retrieval (~13 s) stays on the existing background/waiter path
  (`web_search` is already `type=background`, `inline_timeout_s=20`). No inline sub-second path
  (honest limit from the sub-agent).

---

## Execution log

### Step 1–3 — merge + integrate + verify (LOCAL) ✅

- **Merged** `feat/verified-retrieval-crawl4ai` → main (squash, clean), scrubbed 2 reintroduced
  CLAUDE.md refs. Commit `2ab05ef`.
- **Retrieval harness on my side:** deterministic **31 passed**; real-call **8 passed** (live
  Crawl4AI 0.8.6 container @127.0.0.1:11235 + real Serper + OpenRouter).
- **Integration wired:** `api/composition.py` (shared `Crawl4AIClient` + per-call
  `_build_retrieval(user_id, session_id)`), `core/tools/builtin/core_tools.py` (`web_search`
  tool prefers `RetrievalPort.verify()`, degrades to Serper snippet on error; our bugs raise).
  ruff + `lint-imports` KEPT (core→ports only) + mypy: all green.
- **FULL-FLOW GATE (real engine turn, both callers):** PASS.
  - `generate` and `generate_spoken`: "who is the current prime minister of Nepal?" →
    searched=True → reply **"Balendra Shah is currently the Prime Minister of Nepal."** (grounded).
  - **Verified retrieval confirmed engaged:** the Crawl4AI container logged live crawl requests
    during the turn (browser-pool activity @14:29) — not the snippet fallback.
- **Known cosmetic gap:** `retrieval.<stage>` spans aren't tagged with the turn number, so they
  don't surface in the per-turn trace view (they do reach logs/trace store). Functional path is
  unaffected; noted as a follow-up.

**Gate condition ("if it works with full flow") — met. Proceeding to deploy.**
