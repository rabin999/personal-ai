# SRC1 + tone — the app answered current affairs from training data

**Date:** 2026-07-09 · Every number below is a real run through `VoiceSession.converse`
(the live voice entrypoint), against real OpenRouter / Serper / Qdrant / Mongo / Neo4j.

Raw data: `docs/quality/live_info_before.json`, `docs/quality/live_info_after.json`,
`docs/quality/baseline_live.json`, `docs/quality/after_character*.json`.
Harnesses: `scripts/live_info_probe.py`, `scripts/quality_eval.py`,
`scripts/style_calibration.py`, `scripts/judge_contention.py`.

---

## 1. S1 — the evidence table

`streamable = not _is_live_info_query(utterance)` — a **topic-keyword regex**. When it returns
False the turn takes the non-agentic streaming path and **can never reach a tool**. A phrasing
heuristic was silently deciding whether the app was allowed to be correct.

### Before

| Question | `_is_live_info_query` | searches | answer |
|---|:--:|--:|---|
| who is the current prime minister of Nepal? | `False` | **0** | answered from training data |
| who is the president of the United States? | `False` | **0** | *"Joe Biden is still the President of the United States"* |
| what's the LTP of SYPNL? | `False` | **0** | *"I'm not familiar with 'LT p' … Could you clarify?"* |
| what's the price of SYPNL? | `True` | 2 | correct (live) |
| what's the weather in Kathmandu right now? | `True` | 2 | correct (live) |
| is Tim Cook still the CEO of Apple? | `False` | **0** | answered from training data |
| what happened in the news today? | `True` | 2 | correct (live) |
| *(control)* what's 15% of 240? | `False` | 0 | *"That's 36."* ✅ |
| *(control)* I'm feeling low today | `False` | 0 | warm reply, no search ✅ |

**5 of 7 current-affairs questions never searched.**

### After — 9/9

| Question | heuristic | searches | query used | answer |
|---|:--:|--:|---|---|
| who is the current prime minister of Nepal? | `False` | **1** | `current prime minister of Nepal` | *"Balendra Shah has been serving as the prime minister…"* |
| who is the president of the United States? | `False` | **1** | `Who is the current president of the United States?` | grounded in the search result |
| what's the LTP of SYPNL? | `False` | **1** | `LTP SYPNL stock price Nepal` | *"The Last Traded Price for SYPNL is 1,367.50"* |
| what's the price of SYPNL? | `True` | **1** | `SYPNL share price NEPSE` | *"currently 1,373.00 Nepalese Rupees"* |
| what's the weather in Kathmandu right now? | `True` | **1** | `weather in Kathmandu right now` | *"about 26 °C … 40% chance of rain"* |
| is Tim Cook still the CEO of Apple? | `False` | **1** | `Is Tim Cook still CEO of Apple` | *"Yes … but he's stepping down on September 1, 2026"* |
| what happened in the news today? | `True` | **1** | `top news headlines today` | live headlines |
| *(control)* what's 15% of 240? | `False` | **0** | — | *"That would be 36."* ✅ |
| *(control)* I'm feeling low today | `False` | **0** | — | warm reply ✅ |

Both controls still hold. Over-searching is its own failure.

---

## 2. The design chosen, and why

**The reasoning step already knew.** `LangGraphOrchestrator._resolve_note` (the `context_intent`
node) computes `needs_live_info` and `live_query` on every turn, logs them to the trace — and
**throws them away**. Routing hung off the regex instead. So the fix is not a new mechanism; it
is *using the one that already exists*.

1. `needs_live_info` / `live_query` now ride on `AssembledPrompt` and drive routing
   (`_requires_live_lookup`).
2. The context prompt is **anchored to the user's local date** ("Today is Thursday, 09 July
   2026. 'Current' means THIS date.") and told that role-holders, "still", prices, scores and
   today's news are **always** volatile.
3. `_stream_reply` returns `None` when its buffered draft is a refusal or hollow promise, so
   the streaming route **cannot permanently block a search**. This is only safe because C1
   buffers the draft before speaking — nothing has to be retracted aloud.
4. A required search that **fails** or **finds nothing** now says so honestly. It never falls
   back to a stale answer, and never ships an unkeepable promise.

### Why a deterministic backstop was still necessary

We measured the classifier rather than trusting it. Its **judgement** is good — 4-5 of 5 correct
on the probe set. Its **delivery** is not:

```
context_intent JSON outcomes over 12 calls: {'ok': 11, 'parse_fail': 1}
  FAIL out_tokens=0 raw='{'
```

~1 call in 6 returns a bare `"{"` with `output_tokens=0`. We tried four parameter combinations
(temperature 0.0/0.2, with and without `max_tokens`, with and without the reasoning budget);
the failure occurs in all of them. It is provider flakiness, not a parameter.

That `JSONDecodeError` was caught and swallowed straight into `needs_live_info=False` — i.e.
*"answer from training data"*. So:

- `context_intent` **retries once, on a stronger tier**.
- **A classifier failure never means "don't search."** `core/reasoning/volatility.py` is a
  deterministic backstop matching the **shape of a time-sensitive question** — role-holder,
  "still", temporal deixis on a *factual* question, moving-value vocabulary. It is explicitly
  **not another topic-keyword list**: it fires on "who is the current prime minister of Nepal?"
  and refuses to fire on "I'm feeling low today" (a statement, despite the deixis) or "how are
  you doing today?" (aimed at the companion). 22 unit tests, controls included.

Bias is toward searching: a needless search costs a second; a stale answer costs trust.

---

## 3. S2 — the search query is built from the resolved entity

`_capability_repair` sent **the raw transcript** to Serper. With `OP` correctly seeded as a
NEPSE holding, "what's the price of OP?" still returned the **Optimism crypto token**:

```
before:  query = "what's the price of OP?"
         -> "Right now, Optimism (OP) is trading at about $0.0989"
```

`_build_search_query` now composes the query from the inferred intent **plus the user's own
context** (resolved entities, facts, project, episodic memory) already assembled into the turn:

```
after:   query = "OP NEPSE LTP"   /   "NEPSE LTP OP"   /   "current LTP OP NEPSE"
         -> the NEPSE share. Never crypto.
```

Where the search genuinely finds nothing (OP's LTP is not on the open web), the companion now
says so — *"I had a look and couldn't find anything current on that"* — instead of promising
*"I'll do my best to find that for you"*, a promise the turn cannot keep.

---

## 4. S3 — the fixture

`u_demo_001` had **one** entity ("My portfolio") and no `OP` anywhere.
`scripts/seed_demo_user.py` seeds OP / SYPNL / NABIL as `holding` entities plus five episodic
memories (OP bought at 300 yesterday, SYPNL at 230, NABIL dividends).

**A bug I introduced, and caught.** The first version described them as *"a share on the NEPSE
(Nepal Stock Exchange)"*. Entity resolution runs BM25 over the whole utterance, so the token
**"Nepal"** matched OP and SYPNL at 0.833 for *"who is the current prime minister of Nepal?"* —
two close candidates — and the ambiguity guardrail hijacked the turn with *'Quick check — do
you mean "OP" or "SYPNL"?'*. Descriptions no longer contain common words. Found by the probe,
not by a test.

---

## 5. Three further defects the probes exposed

1. **A cold `web_search` is 6021 ms; `run_inline` allowed 8 s and timed out at 8002 ms.**
   Queries phrased "right now"/"today" deliberately bypass the cache, so they are *always*
   cold. "what's the weather in Kathmandu right now?" returned **nothing**. Slow tools now
   declare their own inline budget (`ToolSpec.inline_timeout_s`); `web_search` gets 20 s. It
   overlaps the spoken ack, so the user hears no extra silence.

2. **`search_memory` discharged a volatility-flagged turn.** "what's the price of SYPNL?"
   answered *"1,373 rupees"* with **zero web searches** — the model reached for `search_memory`,
   which satisfied the "no tools ran" check, and a price stored on an earlier turn was spoken
   as current. Only a `web_search` discharges a volatile turn now.

3. **Judge scores were silently rejected by Langfuse.** Enabling the evaluator surfaced
   `"API errors occurred: Bad request"` on every submit, swallowed to `logger.debug`. Verified
   against the live API: `create_score(trace_id=…)` alone works, `create_score(session_id=…)`
   alone works, **both together 400**. Scores attach to the trace now, and a failure logs at
   WARNING. Proof: `companion_voice=4` read back out of the Langfuse API.

---

## 6. S4 — tone

*(filled in from the second gate run; see §7 for the honest verdict)*

### The two stacked bugs

**The character machinery had never run.** `generate_spoken → _stream_reply → _finish_spoken →
_finish`. Self-reflection (§9.3), the curiosity gate, `check_boundary()` and `_warm_disclosure()`
all lived in `_finalize`, which the streaming voice path never reached. Verified: **no
`reflection` span existed on any voice turn.**

**And the detector caught nothing**, so even once the gates ran they would have found nothing to
rewrite — a passing trace with an unchanged voice.

### Detector vs judge (`scripts/style_calibration.py`)

| | before | after |
|---|--:|--:|
| recall (judge said `chatbot_like` → detector flagged) | **0 / 7** | **7 / 7** |
| false positives (detector flagged a reply the judge passed) | 1 | **0** |

Root cause per missed reply:

| Missed reply | Why |
|---|---|
| *"What can I help you with right now?"* | pattern required the literal word **"today"** |
| *"I'm really sorry for that / …if I've been slow"* | **no corporate-apology pattern existed** |
| *"I don't have enough information to get you…"* | no service-framing pattern |
| *"Do you mean a stock symbol, or something else?"* | no clarify-shape pattern |
| *"I don't feel emotions like a person does"* | object list had `like you`, not `like a person` |
| *"I don't have feelings the way you do"* (gs3) | verb list was `feel\|think\|experience` — not **`have`** |
| *"Yeah, what's up?"* (gs3) | no flat-filler-reply rule |
| *"Hey, OnlyForA here—"* | no self-announcement rule |

The one **false positive** — *"Please know I'm here to listen"* on a grief turn, judged 5/5 —
is fixed by requiring an availability qualifier. *"sorry **to hear**"* (empathy) is now
distinguished from *"sorry **for/about/if**"* (service apology). **No word-count rule**: good
replies run to 67 words and bad ones to 70, so length does not separate them.

### C3 — dynamic prosody had never fired

`ser_service_url` is empty in every deployment → `prompt.emotion` is always `None` →
`read_register()` always returns `"neutral"`. **U8 was marked ✅ and had never once executed.**
Three docstrings claimed a "falls back to text-sentiment" behaviour with **no implementation**.

`core/reasoning/prosody.py::emotion_from_text` now derives the read from the reasoning step's
own `emotional_read` (already computed, already discarded), with lower confidence than acoustic
SER, which still wins when present. The three false docstrings were corrected to describe what
exists.

---

## 7. Honest verdict — the quality gate FAILED

**`chatbot_like` = 2/11. The bar is 0/11. Nothing was deployed.**

| Metric | Before | After | Target | |
|---|--:|--:|--:|:--:|
| `chatbot_like` scenarios | 3/11 | **2/11** | 0/11 | ❌ |
| detector-vs-judge recall | 0/7 | **7/7** | high | ✅ |
| detector false positives | 1 | **0** | 0 | ✅ |
| `reflection` span on voice turns | absent | **present** | present | ✅ |
| dynamic-tone gate `min_tone_fit` | 2 | **2** | ≥3 | ❌ |
| S1 live-info probes | 4/9 | **9/9** | 9/9 | ✅ |

Real quality DID move — `nature_disclosure` 3→4.5 (and no longer `chatbot_like`),
`blunt_frustrated` 2→3, `trivial_greeting` 4.5→5, `memory_recall` 4→5,
`emotional_sad` keeps 5 and no longer carries a style flag. But "moved" is not "passed."

### What is still wrong, precisely

1. **`live_search`** — the answer is now *correct* (`NPR 308.90`, the NEPSE share) but the reply
   opens with *"I'll check that for you right now"* and **speaks the raw search query aloud**:
   *"…OP NEPSE LTP current price Nepal stock exchange The current LTP of OP is NPR 308.90."*
2. **`blunt_frustrated`** — *"I really want to help sort things out for you right now"*, which the
   detector did not know.
3. **`nature_disclosure` `banned=True` was a bug in my own harness**, not the engine:
   `quality_eval` called `find_forbidden()` without `allow_disclosure`, so it flagged the warm
   one-line disclosure that §1.2 rule 4 explicitly *asks for*. The engine's `style_flags` was `[]`.
4. **`min_tone_fit = 2`** on the neutral variant of the tone probe. The sad and excited variants
   both score 5 and the three replies are distinct, so the register *is* driving delivery — but a
   neutral turn is still reading as generically warm.

Fixes for (1), (2) and (3) are committed. **They are not verified** — the gate has not been re-run
against them. Do that first (`docs/HANDOVER.md` §2).

### Deliberately not measured

- **Latency.** `first_audio` in the gate runs is 7–22 s vs 4.3–6.2 s in the baseline. The causes
  are known (draft buffering — measured at median 177 ms / p95 669 ms; the `context_intent` retry
  on a stronger tier; the second `style_rewrite` attempt; more turns legitimately searching; and
  the judge running concurrently). They are **single samples**, and `context_intent` alone varied
  1872 ms → 6985 ms on identical input. **Reporting them as a latency result would be dishonest.**
  Re-measure with N ≥ 5, median + p95.
- **S5 contention.** `scripts/judge_contention.py` is written but has not been run.

### Doc claims deleted for having no implementation

- `voice/emotion.py`, `adapters/ser/emotion2vec_client.py` and `core/reasoning/prosody.py` each
  claimed the pipeline "falls back to text-sentiment" when acoustic SER is unavailable. **No such
  code existed.** The claim is now true (`emotion_from_text`), and the docstrings describe it.
- `docs/BUILD_STATUS.md` U8 "Dynamic prosody per emotional read ✅" was marked done while
  `read_register()` had never returned anything but `"neutral"` in production.
