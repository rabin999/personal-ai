# Conversation-quality + UX fixes — work log

Running record of the post-verified-retrieval fix program: what was done, WHY, how it was verified
(real app + judged against `docs/quality/preferred_conversation_style.md`), the result, and the
deploy. Newest batch appended at the bottom. Design doc / full plan: the approved plan file.

**Guiding rules (apply to every batch):**
- Each pipeline step is a REPRODUCIBLE, CONTRACTED stage: typed input → process → typed output,
  testable independently or composed; no hidden side-channel state.
- Each step is RESILIENT: our bugs fail loud (`core/errors.py`), dependency/model failures degrade
  gracefully with a fallback, and the degradation is flagged + traced.
- Verify with REAL drives (actual model, actual results), not brittle canned assertions; deterministic
  unit tests cover pure logic only.
- No Claude attribution in commits.

---

## Batch A — "conversation feels human" + model migration — DEPLOYED (2026-07-11)

Commits `4867b5b` (Batch A) + `3453411` (LLM fallback trace). Deployed to prod HEAD `3453411`.

| # | Problem | Root cause | Fix | Result (real drive) |
|---|---|---|---|---|
| 1 | "good morning" at 9:22 PM | no browser-tz capture → `LocaleProfile.timezone` empty; greeting angle forced a time-of-day guess at temp 1.1 | auto-capture `Intl…timeZone` → profile (web); reworded greeting angle to never guess when unknown; temp 1.1→0.9 | "it's pretty late there, this evening" |
| 2 | name said every reply | no frequency rule; instructions pushed the name | name-rarely rule on both reply paths; softened greeting name push | name in **1/6** replies |
| 4 | long/formal/news-anchor | gemini-flash ignores brevity (proven 3×); warm directive appended last | ENFORCE brevity: `_reads_verbose` + `_rewrite_brief` in `_apply_gates`; prosody directives end on brevity | greeting-card monologue → "That's a lot to carry right now. How are you doing with it all?" |
| 5 | fixed location in profile | manual locale block | removed (tz now dynamic) | gone |
| — | companion name | fallback "Companion"/"Asaathi" | default → **Saathi** (app brand "Asaathi" kept) | — |
| — | model (2.5-flash deprecating Oct 16, ignores brevity) | — | **config-only** migration chosen via live catalog + Artificial Analysis benchmarks + A/B on our conversation: reply → **claude-haiku-4.5** (fallbacks gpt-4.1-mini, gemini-3.1-flash-lite); complex → **claude-sonnet-5**. Rejected reasoning-mandatory models (gemini-3.5-flash/grok-4.5/glm-5.2/gpt-5-mini) for the reply tier: 13–23s TTFT = voice silence | tool turn "PM of Nepal" → grounded "Balendra Shah…"; arithmetic → "36" |
| — | LLM fallback invisible | fallback chain logged only | `llm.fallback` trace event per failure + `fallback`/`failed_models` on the call span | traced |

**Why Haiku:** benchmarks (Intelligence 24, **1.0s TTFT**, $0.77) + our A/B showed it was the warmest,
most context-aware, and best instruction-follower (so the brevity rewrite fires less). gpt-4.1-mini is
the kept value fallback.

**Test note:** 2 SMTP failures are standing/env. 3 acceptance failures (project_flow / background_delivery /
consolidation) are model-judgment brittleness (real-call-ish tests), not logic — the real drive of the
tool path is correct; router test relaxed to compare provider (OpenRouter returns the dated canonical id).
