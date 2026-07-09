# HANDOVER — resume here

**Last session:** 2026-07-09. **Branch:** `main`, pushed. **Deployed:** NO (server still on `7e779c7`).

> **The quality gate FAILED (2/11, target 0/11), so nothing was deployed.** Two mechanical fixes
> for those two failures are committed but **NOT yet verified by a gate run**. That re-run is the
> single next action.

Read first: `docs/SRC1_AND_TONE_REPORT.md`, then this file.

---

## 1. Where things stand

| Item | State | Evidence |
|---|---|---|
| **S1** — current-affairs questions never searched | ✅ **9/9 probes** | `docs/quality/live_info_{before,after}.json` |
| **S2** — search query built from resolved entity | ✅ `OP` → `OP NEPSE LTP` → **NPR 308.90** (the NEPSE share, not the crypto token) | §3 of the report |
| **S3** — sample-user fixture | ✅ seeded | `scripts/seed_demo_user.py` |
| **S4** — tone / `chatbot_like` | ❌ **FAILED: 2/11** (was 3/11; target **0/11**) | `docs/quality/after_character2.json` |
| **S5** — judge on the live path | ✅ enabled + own connection pool; scores land in Langfuse | §5 of the report |
| **Deploy** | ⬜ **blocked on S4** | — |

`ruff` clean · `mypy` 204 (the standing baseline) · `lint-imports` 2/2 · **513 tests pass**.

---

## 2. NEXT ACTION (do this first)

```bash
uv run python -m scripts.quality_eval --label after_character3 --repeats 2
```

Then read the two numbers that decide everything:

- `chatbot_like` count across the 11 scenarios — **must be 0/11**
- `tone gate: min_fit` — **must be ≥ 3**

Three fixes landed after the last gate run and are **unverified**:

1. **Detector learned the service-offer shapes** it missed — `"I'll check that for you right now"`,
   `"I really want to help sort things out for you"`. Those were the exact two `chatbot_like`
   replies (`live_search`, `blunt_frustrated`).
2. **`_strip_query_echo`** — the raw search query was being spoken aloud:
   *"I'll check that for you right now. **OP NEPSE LTP current price Nepal stock exchange** The
   current LTP of OP is NPR 308.90."*
3. **Harness bug of mine:** `quality_eval` called `find_forbidden()` without `allow_disclosure`,
   so it flagged the *desired* pull-based disclosure on `nature_disclosure` as banned. The engine's
   own `style_flags` was correctly `[]`. Fixed — that "failure" was never real.

If the gate passes: run `scripts/judge_contention.py` (S5's last unmeasured claim), fill in
§7 of `docs/SRC1_AND_TONE_REPORT.md`, update `docs/TEST_REPORT.md`, then
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
