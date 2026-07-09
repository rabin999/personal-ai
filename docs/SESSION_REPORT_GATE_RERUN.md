# Session report — S4 gate re-run (2026-07-09, later)

**Verdict: the quality gate FAILED. Nothing was deployed.**
**A separate, more severe production crash was found and fixed.**

Artifact: `docs/quality/after_character3.json` (22 live voice turns + 3 tone probes, $0.0894).
Entrypoint: `VoiceSession.converse` via `scripts/live_turn.drive_turn` — the real path, not canned strings.

---

## 1. What I was asked to do

Resume from `docs/HANDOVER.md`, whose single stated next action was:

```
uv run python -m scripts.quality_eval --label after_character3 --repeats 2
```

Three fixes from the prior session were committed but never verified by a gate run.
Pass condition: `chatbot_like` **0/11** and tone `min_fit` **≥ 3**.

---

## 2. Result: FAILED

| Gate | Target | Actual | |
|---|---|---|---|
| `chatbot_like` | 0/11 | **2/11** | ❌ |
| tone `min_fit` | ≥ 3 | **2** | ❌ |
| tone variants distinct | true | **false** | ❌ |
| empty replies | 0 | **1** | ❌ |

Per-scenario (mean of 2 runs):

| scenario | score | tone | chatbot | banned | empty | calls | first_audio |
|---|---|---|---|---|---|---|---|
| trivial_greeting | 4.5 | 5 | · | · | · | 2 | 12823 ms |
| thanks_ack | 4 | 3.5 | · | · | · | 2 | 8189 ms |
| memory_recall | 5 | 4 | · | · | · | 2.5 | 16208 ms |
| **live_search** | 3 | 4 | **✗** | **✗** | · | 9.5 | 10728 ms |
| multi_intent | 4 | 2 | · | · | · | 7 | 10250 ms |
| nature_disclosure | 5 | 5 | · | · | · | 3 | 16503 ms |
| **indirect_intent** | 2.5 | 2.5 | · | · | **✗** | 2.5 | 6947 ms |
| emotional_sad | 5 | 3 | · | · | · | 2.5 | 19451 ms |
| emotional_excited | 5 | 5 | · | · | · | 2 | 9108 ms |
| blunt_frustrated | 4.5 | 3.5 | · | · | · | 3 | 13498 ms |
| **followup_reference** | 3 | 3 | **✗** | · | · | 2.5 | 11660 ms |

### The three inherited fixes DO work

- **Detector learned the service-offer shapes** — it now flags `assistant offer` correctly.
- **`_strip_query_echo`** — no raw search query was spoken in any of the 22 turns.
- **`allow_disclosure` harness bug** — `nature_disclosure` now scores 5/5, clean. That
  "failure" was never real, as the prior session suspected.

`blunt_frustrated` is genuinely fixed: `chatbot_like` → `False`, score 4.5.

**The count did not move (2/11 → 2/11) because the failures relocated, not because the fixes
missed.** Reporting that as progress would be dishonest; reporting it as "no change" would be
equally wrong. Both statements need the breakdown above.

---

## 3. The three surviving defects, in priority order

### 3.1 `indirect_intent[1]` — the user heard SILENCE (new, most severe)

```
llm_calls=0   first_audio=None   reply=""   exceptions=['ReadTimeout']
```

A `ReadTimeout` on the reply call produced an **empty spoken turn with no fallback**. A silent
companion is worse than a chatbot-sounding one, and nothing in the pipeline degrades a timeout
into speech. This did not appear in the previous run.

### 3.2 `live_search` — the engine speaks a reply it has already flagged as bad

Final spoken text:

> *"Oh, you're looking for the current Last Traded Price for OP again. I'll check that for you
> right now."*

That is the search **acknowledgement**, carrying **no answer**, emitted after:

```
searches=3   llm_calls=11   discarded_drafts=5   style_flags=['assistant offer']   action=respond
purposes=[context_intent, ack, response, search_summarize, response, search_summarize,
          response, response, search_summarize, response, response]
```

The engine's own `style_flags` correctly identified the reply as assistant-speak — and shipped it
anyway. **The detector detects; nothing enforces.** Two separable bugs:

- **(a)** a draft carrying `style_flags` must never become the final spoken reply;
- **(b)** the ack must never survive as the final reply when the repair loop exhausts.

Intermittent: `live_search[0]` was clean (`"...I don't see current LTP information for a stock
with that exact ticker..."`, `chatbot_like=False`).

### 3.3 Tone gate — `distinct=False`, `min_fit=2`

The `excited` and `neutral` variants returned a **byte-identical** reply; `sad` differed only in
its opening word.

```
sad     [down    ] fit=2 :: "Oh, again with the work uncertainty, huh? It sounds like that's really…"
excited [excited ] fit=2 :: "Ah, still feeling that uncertainty about work, huh? It sounds like tha…"
neutral [neutral ] fit=5 :: "Ah, still feeling that uncertainty about work, huh? It sounds like tha…"
```

Dynamic prosody still does not change delivery. Consistent with the standing **U8** finding that
`ser_service_url` is empty in every deployment, so `prompt.emotion` is always `None`.

### 3.4 Not counted: `followup_reference`

Flagged `chatbot_like` on run 1 — but the judge's stated reason is *over-explaining*, not
service-desk phrasing, and run 0 was clean. Likely judge noise on a borderline reply. **Verify
before treating this as a code defect.**

---

## 4. The bug the gate could never have caught

While running the suite I found **seven red tests at `HEAD`** — against the prior handover's claim
that "**513 tests pass**". That claim was false.

**Root cause.** `629a500` ("run the independent context reads concurrently", an L1 latency
optimization) moved two calls into the same `asyncio.gather`:

- `first_run_sync(user_id)` — **creates** the user's profile if absent
- `enabled_traits(user_id)` — **reads** that profile, raising `ProfileNotFound` if absent

They are not independent. As siblings both read Mongo concurrently, the trait read loses, and
`assemble()` raises. **Every brand-new user's first turn crashed.**

Before `629a500` these were sequential (`prompt_assembly.py:249` then `:250`) and the ordering
carried the dependency implicitly. Making them concurrent silently deleted it.

**Why nothing caught it in review:** the quality gate only ever speaks as the seeded demo user
`u_demo_001`, who already has a profile. The gate is blind to onboarding by construction.

**Fix** (`6829504`): hoisted `first_run_sync` above the gather as the prerequisite it is. Every
layer that really is independent still runs concurrently. Cost: one sequential Mongo read (~ms).

Tests restored to green by the fix: 5 × `test_core_engine_e2e`, `test_full_text_conversation_turn`,
`test_full_assembly_over_real_stores`.

---

## 5. Checks

| Check | Result |
|---|---|
| `ruff check` | clean |
| `mypy .` | 204 errors — **unchanged** standing baseline |
| `lint-imports` | 2 contracts kept, 0 broken |
| `pytest -m "not real_call"` | **594 passed, 2 failed, 9 skipped, 5 xpassed** |

The 2 failures are the known SMTP-credential env failures (`test_outbox_worker_sends_when_mail_is_configured`,
`test_mailer_reports_disabled_without_credentials`) — they need real mail credentials, documented
as blocked since the F1–F16 pass.

**Method note:** my first suite run was executed concurrently with the gate and produced one
spurious failure (`test_self_model::test_real_llm_rewrites_overclaiming_draft`) from LLM
contention. I re-ran serially before drawing any conclusion. The seven `ProfileNotFound` failures
reproduced in both runs; only that one was contention.

---

## 6. Commits

- `6829504` — `fix(reasoning): §10 — a new user's first turn died with ProfileNotFound`
- `7bd4a00` — `docs: S4 gate re-run FAILED (2/11, tone min_fit=2) — honest status`

---

## 7. Next action

**Defect 3.1 first:** a `ReadTimeout` must degrade to *something spoken*, never silence. There is
no fallback on the reply path today.

Then **3.2(a)** — make `style_flags` enforcing rather than advisory — which likely subsumes
**3.2(b)**.

**3.3** is a deeper question than a patch: dynamic prosody has never fired in production (U8), so
the tone gate is measuring a mechanism that is switched off. Either wire the reasoning step's
`emotional_read` through to delivery, or stop gating on it and say so.

**Deploy stays blocked.** `sudo bash /opt/companion/deploy/update.sh` on `root@202.58.120.93`
only after `chatbot_like` is 0/11 and `min_fit` ≥ 3.
