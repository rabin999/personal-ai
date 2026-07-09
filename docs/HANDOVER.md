# HANDOVER — resume here

**Last session:** 2026-07-09 (later). **Branch:** `main`. **Deployed:** NO (server still on `7e779c7`).

> **The quality gate was re-run and FAILED AGAIN: `chatbot_like` 2/11 (target 0/11),
> `tone min_fit` 2 (target ≥3), and one turn produced NO AUDIO AT ALL.** Nothing deployed.
> The three "unverified fixes" from last session are now verified: two landed, one didn't.

Read first: `docs/SRC1_AND_TONE_REPORT.md`, then this file.

---

## 1. Where things stand

| Item | State | Evidence |
|---|---|---|
| **S1** — current-affairs questions never searched | ✅ **9/9 probes** | `docs/quality/live_info_{before,after}.json` |
| **S2** — search query built from resolved entity | ✅ `OP` → `OP NEPSE LTP` → **NPR 308.90** | §3 of the report |
| **S3** — sample-user fixture | ✅ seeded | `scripts/seed_demo_user.py` |
| **S4** — tone / `chatbot_like` | ❌ **FAILED: 2/11**, tone `min_fit=2`, tone variants **not distinct** | `docs/quality/after_character3.json` |
| **S5** — judge on the live path | ✅ enabled; scores land in Langfuse | §5 of the report |
| **§10 new-user first turn** | ✅ **FIXED this session** (`6829504`) — was a hard crash | 7 red tests now green |
| **Deploy** | ⬜ **blocked on S4** | — |

`ruff` clean · `mypy` 204 (standing baseline) · `lint-imports` 2/2 ·
**594 passed, 2 failed** (both known SMTP-credential env failures).

> ⚠️ The previous handover claimed "**513 tests pass**". That was **false**. Seven tests were
> red at that commit — 5 `core_engine_e2e`, `test_full_text_conversation_turn`, and
> `test_full_assembly_over_real_stores` — all from the `ProfileNotFound` regression below.
> Re-run the suite yourself; do not trust a pass count you did not watch.

---

## 2. What this session found and fixed

**A real production crash, unrelated to S4.** `629a500` (the L1 latency commit) moved
`first_run_sync` and `enabled_traits` into the same `asyncio.gather`. `first_run_sync` *creates*
the profile; `enabled_traits` *reads* it. As siblings they race, the trait read loses, and
`assemble()` raises `ProfileNotFound` — **every brand-new user's first turn crashed**. The quality
gate never caught it because it only ever speaks as the seeded demo user, who already has a
profile. Fixed in `6829504`.

---

## 3. NEXT ACTION — three real defects, in priority order

Gate artifact: `docs/quality/after_character3.json`.

**1. `indirect_intent[1]` returned an EMPTY reply** (`ReadTimeout`, `llm_calls=0`, `first_audio=None`).
The user heard **nothing**. A silent turn is worse than a chatbot-like one and there is no
fallback. This is the highest-severity item and it is *new* — it was not in the last run.

**2. `live_search` — the engine ships a reply it has already flagged as bad.** Final spoken text
was exactly *"Oh, you're looking for the current Last Traded Price for OP again. I'll check that
for you right now."* — the search **ack**, with no answer, after `searches=3`, `llm_calls=11`,
`discarded_drafts=5`. Its own `style_flags` was `['assistant offer']`. So the detector fix from
last session **works** (it detects), and `_strip_query_echo` **works** (no query echo), but
nothing *enforces* the flag: when the repair loop exhausts, the ack is emitted as the final reply.
Two bugs to separate: (a) a flagged draft must never be spoken; (b) the ack must never become the
final reply. Note `live_search[0]` was clean — this is intermittent.

**3. Tone gate: `distinct=False`, `min_fit=2`.** The `excited` and `neutral` variants produced a
*byte-identical* reply, and `sad` differed only in its opening word. Dynamic prosody still does not
change delivery. Consistent with the standing U8 finding (`ser_service_url` is empty in prod).

`blunt_frustrated` **is fixed** (was `chatbot_like`, now `False`, score 4.5). `followup_reference[1]`
is newly flagged, but for *over-explaining*, not service-desk speak — likely judge noise on a
borderline reply; check before treating it as a code defect.

**When the gate passes:** run `scripts/judge_contention.py`, fill §7 of
`docs/SRC1_AND_TONE_REPORT.md`, update `docs/TEST_REPORT.md`, then
`sudo bash /opt/companion/deploy/update.sh` on `root@202.58.120.93`.

**If it still fails, report it as failed.** A green checkmark with an unchanged `chatbot_like`
count is a failed task.

---

## 3. Known-unresolved / open

- **`indirect_intent` is borderline.** It scored 4/`False` in one gate run and 2/`True` in
  another *with a near-identical reply*. The judge is noisy on this scenario. `--repeats 2` may
  not be enough to call it; consider `--repeats 3`.
- **Latency regressed and has NOT been measured properly.** `first_audio` in the gate runs is
  7–22 s, vs 4.3–6.2 s in `baseline_live.json`. Causes are known but unquantified:
  C1's draft buffering (measured: median **177 ms**, p95 669 ms), the `context_intent` retry on a
  **stronger tier**, the `style_rewrite` second attempt, and **more turns now legitimately
  searching**. The judge was also enabled during those runs and may have contended.
  **Do not report any of those numbers as a latency result** — they are single samples, and
  `context_intent` alone varied 1872 ms → 6985 ms on identical input. Re-measure with
  `scripts/latency_trace_capture.py` (N ≥ 5, `realtime=True`, median + p95) against
  `docs/LATENCY_BASELINE_REAL.md`.
- **`context_intent` returns unusable JSON ~1 call in 6** (a bare `"{"`, `output_tokens=0`). Four
  parameter combinations were tried; it's provider flakiness. Currently handled by a retry on a
  stronger tier plus `core/reasoning/volatility.py` as a deterministic backstop. Worth revisiting
  the model choice.
- **OP's LTP is not reliably on the open web.** The query is now correct
  (`OP NEPSE LTP` → NPR 308.90 once), but the search sometimes returns nothing, and the companion
  correctly says *"I had a look and couldn't find anything current on that"*. A NEPSE-specific
  data source would fix this properly.
- **`docs/BUILD_STATUS.md`** carries a warning block listing ✅ marks that this work proved
  untrustworthy (L3, L0/L5, U8, GS3 judge). Still accurate.

---

## 4. What changed, in one paragraph each

**S1 — the app answered current affairs from training data.** Routing hung off
`_is_live_info_query`, a topic-keyword regex returning `False` for "who is the current prime
minister of Nepal?", which sent the turn down the non-agentic streaming path where it could never
reach a tool. The reasoning step (`context_intent`) *already* computed `needs_live_info` and
`live_query`, logged them, and threw them away. They now ride on `AssembledPrompt` and drive
routing; the prompt is anchored to the user's local date; a classifier failure can never mean
"don't search"; `_stream_reply` hands the turn to the agentic path when its buffered draft is a
refusal or a hollow promise; and a required search that fails or finds nothing says so honestly
instead of falling back to a stale answer.

**S2 — `_capability_repair` sent the raw transcript to Serper**, so "OP" resolved to the Optimism
crypto token even with OP correctly seeded as a NEPSE holding. Queries are now composed from the
inferred intent plus the user's own entities/facts/memory.

**S4 — the character machinery had never run on the voice path.** Self-reflection, the curiosity
gate, `check_boundary()` and `_warm_disclosure()` all lived in `_finalize`, which
`generate_spoken → _stream_reply → _finish_spoken → _finish` never reached. And the detector that
*triggers* self-reflection caught **0 of 7** replies the judge called `chatbot_like`, so even once
the gates ran they would have found nothing to rewrite. Both are fixed (detector now 7/7 recall,
0 false positives) — but the end-to-end number is still 2/11, not 0/11.

**C3 — dynamic prosody had never fired.** `ser_service_url` is empty in every deployment, so
`prompt.emotion` was always `None` and the register was always `"neutral"`. Three docstrings
claimed a "falls back to text-sentiment" behaviour with no implementation behind it. The reasoning
step's own `emotional_read` now drives it; the docstrings were corrected.

---

## 5. Incidental bugs fixed along the way (all committed)

- A cold `web_search` is **6021 ms**; `run_inline` allowed **8 s** and timed out at 8002 ms.
  "right now"/"today" queries bypass the cache by design, so they were *always* cold —
  "what's the weather in Kathmandu right now?" returned nothing. Slow tools now declare their own
  inline budget (`ToolSpec.inline_timeout_s`).
- `search_memory` discharged a volatility-flagged turn: "what's the price of SYPNL?" answered
  "1,373 rupees" with **zero web searches**, reciting a price stored on an earlier turn as if
  current. Only `web_search` discharges a volatile turn now.
- **Langfuse silently rejected every judge score.** `create_score(trace_id=…, session_id=…)` is a
  400; either alone works. The sink swallowed it to `logger.debug`. Fixed and verified by reading
  a `companion_voice=4` score back out of the API.
- `_rewrite_assistant_speak` accepted any candidate with fewer flags — including the single word
  `"Hey,"`, since a one-word reply trivially has zero forbidden shapes. Degenerate rewrites are
  now rejected.
- Seeding OP/SYPNL with the description *"a share on the NEPSE (Nepal Stock Exchange)"* made BM25
  match the token **"Nepal"** in "who is the current prime minister of **Nepal**?" at 0.833, so the
  ambiguity guardrail hijacked the turn. My bug, caught by the probe, fixed.

---

## 6. Useful commands

```bash
uv run python -m scripts.quality_eval --label <name> --repeats 2   # the gate (chatbot_like, tone)
uv run python -m scripts.live_info_probe after                     # S1's 9-probe table
uv run python -m scripts.style_calibration                         # detector vs judge agreement
uv run python -m scripts.judge_contention                          # S5's contention claim
uv run python -m scripts.voice_live_probe --text "hi"              # one real turn, live entrypoint
uv run python -m scripts.seed_demo_user                            # re-seed the sample fixture
uv run ruff check && uv run mypy . && uv run lint-imports && uv run pytest -m "not real_call"
```

Deploy (human step, on the server): `sudo bash /opt/companion/deploy/update.sh`
— host `root@202.58.120.93`, key at
`/home/rabin/Documents/experiments/next-gen-nepse/trishul/cloud_access/id_ed25519_everestcloud-2026-06-15`.
