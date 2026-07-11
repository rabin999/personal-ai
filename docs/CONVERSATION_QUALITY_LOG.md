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

## PROD HOTFIX + Batch B — DEPLOYED (2026-07-11, commit d6ea731)

**Prod freeze** (user: "let me check on that" then froze on a PM question): root cause = some
live-info turns escalate to the complex tier; **claude-sonnet-5** returned a response shape our
parser couldn't read (`response.choices[0]` → 'NoneType' object is not subscriptable), and the
fallback **gemini-3.5-flash** (reasoning-mandatory, ~23s TTFT) hung → the turn froze and took the
session with it. Intermittent = only some turns hit complex.
- Fix: complex tier → **claude-sonnet-4.5** (proven) + **gpt-4.1-mini** fallback; dropped sonnet-5 +
  gemini-3.5-flash. (provider_config v5 + DEFAULT_TIERS.)
- RESILIENCE: `_call` now surfaces an empty/None response as `LLMUnavailable` (never crashes), so the
  fallback chain works and the turn degrades to a safe reply.
- Verified on PROD with the user's exact query → full grounded reply "…Balendra Shah's the PM. What's
  going on…", not empty. complex tier routes to sonnet-4.5.

**Batch B** (fresh + trustworthy) shipped in the same deploy: killed the hardcoded stale "Boeing
cargo plane" example; source-quality preference (official/.gov/.edu + reference + major news first).
Real drive: "current PM of Nepal" fetches en.wikipedia.org + opmcm.gov.np (official). Unit-tested.

**NEXT (in progress): D-9 turn-level resilience** — guarantee ANY step failure (retrieval, memory,
non-LLMUnavailable deps) still yields a spoken reply; never silence. (User: "even one or any step
fails, user must get its response properly.")

## Resilience status (2026-07-11) — adequate for the reported freeze
- Exceptions already SPEAK an honest line: `_run_turn_inner` except → `_degrade` + `_say_step_failed`
  (voice/session.py:636-638); STT path too (:418). So a dependency EXCEPTION never = silence.
- Hangs bounded: llm_timeout_s=60, retrieval fetch_deadline_ms=20 → a slow step raises → fallback →
  speak. The freeze cause (reasoning-mandatory fallback + parser crash) is removed in the hotfix.
- Follow-up (optional): lower llm_timeout_s for faster degradation; a whole-turn deadline.

## Batch C trace-UI design (from user) — QUEUED
- Card TITLE = first N chars of the turn's input or output text (a snippet), NOT "Turn N". Title only
  at the top header of the same card design.
- Move the summary to the FOOTER: left = one-line summary showing ONLY total cost + time taken
  (nothing else); right side of footer = feedback actions.
- (Plus the earlier Batch C: live view = simple real-time transcript; detailed view keeps full spans;
  backend span fixes: retrieval turn-0 dup, ranked results.)

## Continuing: #14 remove profile settings (engine part first)

## SESSION STATUS (2026-07-11, end of autonomous run) — prod stable

**DEPLOYED to prod (in order):**
1. Batch A — greeting/localtime, name frequency, tone enforcement, profile-location removed,
   companion name "Saathi", model migration (Haiku reply / Sonnet complex). (4867b5b)
2. LLM fallback flag+trace; router test robustness. (3453411)
3. PROD HOTFIX — complex tier off sonnet-5 (parser crash) + off gemini-3.5-flash (23s hang);
   `_call` never crashes on a None/empty response. Verified: user's freezing query now replies. (d6ea731)
4. Batch B — killed stale "Boeing cargo plane" echo; official/authoritative source ranking
   (wikipedia + opmcm.gov.np for the PM query). (d6ea731)
5. No-silence guarantee — empty reply speaks an honest line; exceptions already speak; timeouts bound
   hangs. (6eeb669)

**REMAINING QUEUE (not started; designs captured above):**
- #14 Remove profile settings from UI+API+engine: directness + emotional_scaffolding (also stop
  rendering `comm_prefs` in prompt_assembly.py:331-333 / _STABLE_SECTIONS), thinking + fast model
  pickers (ModelPicker + /api/models), mimic_tone, health_checkins.
- #13 "Norsylinder" — clear the bad stored companion_name; fix the `set_companion_name` self-naming
  (the model naming ITSELF without the user asking).
- Batch C traces: (a) LIVE view = simple real-time transcript (fix the delay, only spoken/heard text
  in a loop); (b) DETAILED view redesign — card title = first-N chars of input/output snippet (not
  "Turn N"); footer = one-line summary showing ONLY cost + time, feedback actions on the right;
  (c) backend: retrieval turn-0 duplicate span + persist ranked search results (titles/snippets).
- Batch D — knowledge graph: replace ring layout with react-force-graph-2d + zoom/pan/drag/select/filter.
- #16 Progressive delivery — empathetic interjection (emotional input) + chunk-by-chunk streaming for
  multi-step turns; builds on _stream_reply + search-ack. Gate on slow turns; never split a tag.
- #8 (later) Phase B eval bundle: coverage matrix (done) + multi-turn harness + intent labeled set.

**Guiding rules to keep applying:** contracted independent stages (typed in→process→out); resilience
(any step fails → user still gets a response); verify with REAL drives; no Claude attribution in commits.

## More fixes — DEPLOYED (2026-07-11, commit 835ca9f)
- **Over-laughing** (even in greetings/first messages): `strip_inappropriate_tags` only stripped
  levity on down/stressed AND wasn't applied on the STREAMING path (greetings stream). Now levity is
  allowed ONLY on excited/upbeat; stripped on neutral/down/stressed; applied in `_stream_reply` too.
  Verified: neutral "[laugh] Hey there!" -> "Hey there!"; excited keeps its laugh.
- **Stale facts / old name**: prompt showed superseded facts (marked) and the model sometimes spoke
  the old value. Now only CURRENT facts (valid_to=None) reach the prompt. PARTIAL — deeper task
  queued: verify graph supersession actually fires + dedup redundant facts (else both old+new are
  "current"). Real-drive test: name A -> change to B -> only B used.

Cumulative validation suite: only the 3 known model-judgment-brittle acceptance tests + 2 SMTP fail;
no new breakage from any of the above.

## OPEN ISSUES (user, 2026-07-11 late) — diagnosed, NOT yet fixed (need fresh-context care)

**1. PM question still answers STALE first (Prachanda) then self-corrects.** Rule wanted: a live-info/
officeholder question must SEARCH before answering, never from training data.
- Diagnosis: `is_volatility_question` DOES catch "current prime minister of nepal" (via `_ROLE_OF` +
  `_DEIXIS`; `_EXTERNAL_MARKER` includes "prime minister" so the self-directed "you" exclusion doesn't
  fire). So `_requires_live_lookup`→True and the turn is NOT streamed. The stale answer therefore comes
  from the AGENTIC path shipping the model's PRE-SEARCH `draft_response` when the model (now Haiku)
  confidently answers "Prachanda" and sets `tool_request=null`. Fix direction: on a volatile turn, do
  NOT ship the pre-search draft — FORCE the search and answer only from `_REPAIR_INSTRUCTIONS`
  (verify-before-answer). Check `needs_search`/`_fallback` path in response_gen (~532-547) for where a
  volatile turn can still return the model draft. Nondeterministic → verify with N≥5 real runs, both callers.

**2a. Search ack "let me quickly check on that" is REPETITIVE / not short/dynamic.** The dynamic ack
(response_gen ~752 + the `_dynamic_ack`/holding-line path) is converging on one phrase. Make it short +
varied (or rotate a small set / dedup against the last ack), gate to once per search.

**2b. Recent plane-crash search returns not-found.** Retrieval/query quality for breaking news. Check the
search query build for news + Serper recency + whether crawl4ai fetched anything (or all bot-walled →
honest error). May need a snippet fallback answer when fetch fails but Serper has the story.

**3. Greeting must be DISCARDED if the user is already talking when they open.** Today `_greet_on_open`
speaks regardless. Fix: if user speech arrives before/at open, cancel the greeting task (barge-in on the
greeting) and process the user's query instead. Voice-session concurrency — do carefully.

**4. Two related sentences across 2 turns → engine only used the latest.** Input-side turn combining /
working-memory: either endpointing split one thought into 2 turns and they weren't merged, or the prompt
didn't include the immediately-prior user turn. Debug working memory (`WorkingMemory.append`/read) +
prompt assembly's recent-turns inclusion; consider a short merge window for consecutive user turns.

## Verify-before-answer + settings removal — DEPLOYED (2026-07-11 overnight)
- **Verify-before-answer** (63ac30c) — THE fix for the recurring stale-PM bug, per docs/RETRIEVAL_POLICY.md.
  A volatile turn whose forced search can't ground it now speaks an honest can't-verify line instead of
  the model's stale training-data draft (was leaking when the deterministic backstop flagged volatile but
  the LLM classifier was None). Verified N=12 real turns, both callers: 0 stale answers; every reply is
  fresh (Balendra Shah, wikipedia + official gov PM office) or honest-fail. NOTE: honest-fail share is
  higher than ideal — direct retrieval is 3/3 reliable, so the engine REPAIR PATH (query build for
  opinion-wrapped phrasings / inline timeout) needs a reliability follow-up (tracked in #18/RETRIEVAL_POLICY).
- **#14 settings removal** (4f0a1d9, via sub-agent, merged clean) — removed directness/emotional_scaffolding
  (also out of the prompt), thinking+fast model pickers (backend-only now; bundle shrank), mimic_tone,
  health_checkins from UI+API+engine. Backend tier routing untouched.
- **NEW policy doc**: docs/RETRIEVAL_POLICY.md — taxonomy of answer-from-training vs must-verify vs
  user-memory + the verify-before-answer invariant + how to measure it.

## Sub-agents in flight
- Knowledge-graph force-directed viz (worktree) — running; merge + deploy when done.

## Batch D knowledge graph — DEPLOYED (33b429d, via sub-agent, merged clean)
Ring layout → force-directed graph (react-force-graph-2d): zoom/pan/drag/click-focus/relation-filter/
superseded-toggle/text-search; labels no longer overlap; theme-aware; verified in a real browser
(Playwright). API unchanged.

## Remaining (sub-agent in flight + backend queue)
- Batch C traces (sub-agent running): live view = real-time user/companion transcript; detailed view =
  snippet title + footer (cost+time / feedback) + search block; backend turn-0 dup + ranked results.
- Backend queue (do next, carefully — subtle): repair-path reliability (reduce honest-fail — improve
  _build_search_query for opinion-wrapped phrasings / inline budget); #13 set_companion_name self-naming
  guard (needs the utterance wired to the tool handler — ToolContext lacks it) + clear stored "Norsylinder";
  #17 memory supersession/dedup; #18 remaining (repetitive ack, greeting-discard-if-speaking, 2-turn combine).
