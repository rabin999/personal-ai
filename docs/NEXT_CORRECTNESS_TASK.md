# SRC1 — "what's the current LTP of OP?" · evidence-based diagnosis

**Status: diagnosed, NOT fixed.** F5 asked to diagnose before acting, because the earlier
`LATENCY_ANALYSIS.md` write-up may have mis-read the failure. It had. So did the counter-hypothesis.
Three separate defects are involved; fixing any one alone does not fix the scenario.

All evidence below is from real runs against the real engine, real Serper, real Qdrant/Mongo/Neo4j.

---

## The two hypotheses on the table, both wrong on their own

| Hypothesis | Verdict |
|---|---|
| "Turn 3 is a routing bug — it never called `web_search`." (LATENCY_ANALYSIS §10.3) | **Half right.** The routing gap is real, but the write-up also mis-attributed the reply. |
| "It may be the ambiguous-entity guardrail working correctly — OP was never seeded." (F5) | **Half right.** The fixture *is* missing OP, but the guardrail never fired, and seeding OP does not make the turn search. |

---

## Defect 1 — `_is_live_info_query` doesn't know "LTP" or "trading at" (REAL routing gap)

`core/reasoning/response_gen.py::_LIVE_INFO_QUERY` matches `price of`, `stock price`, `share price` —
but **not** `LTP` (last traded price), and **not** `trading at`.

```
_is_live_info_query("what's the current LTP of OP?")   -> False
_is_live_info_query("what's OP trading at?")           -> False
_is_live_info_query("what is the price of OP")         -> True
```

This decides `streamable` in `generate_spoken`:

```python
streamable = not (pending confirmation) and not _is_live_info_query(prompt.utterance)
if streamable:
    return await self._stream_reply(...)   # plain prose. NO tool loop, NO capability backstop.
```

So an `LTP` query **cannot** search — `generate()` is never entered, so neither the model's
`tool_request` nor the `_capability_repair` backstop is reachable.

**Controlled proof** — same ticker, one the demo user genuinely holds (`SYPNL`, seeded in episodic
memory as *"bought 10 shares of SYPNL at 230"*), only the phrasing changes:

| Query | `_is_live_info_query` | searches | reply |
|---|---|--:|---|
| `what's the current LTP of SYPNL?` | `False` | **0** | *(no live data)* |
| `what's the price of SYPNL?` | `True` | **1** | *"SYPNL is currently trading at about 1,373 Nepalese Rupees…"* (correct, live) |

The fixture is identical across both rows. **The phrasing alone decides whether the companion searches.**

---

## Defect 2 — the sample user has no OP holding (REAL fixture gap)

Confirmed by scrolling the real stores for `u_demo_001`:

- Qdrant `entities`: **1 point** — `"My portfolio"` (a project). No tickers at all.
- Qdrant `episodic`: 30 points, including `"bought 10 shares of SYPNL at 230"`. **No mention of OP.**
- Mongo `entities` / `trades` / `procedural`: **0 rows**.

So when Turn 4 searched, nothing in the user's data connected "OP" to their portfolio, and the model
guessed. Its own search query was literally `"weather in Kathmandu Nepal, OP crypto trading price"`.
That is the fixture gap — and it explains the *crypto* reading.

---

## Defect 3 — the capability-repair search ignores resolved entities (NEW; nobody predicted this)

Fixing the fixture is **not sufficient.** With OP seeded into an isolated fixture user
(`u_src1_fixture`, entity + episodic evidence, since deleted), the entity now resolves correctly —
and the answer is *still* crypto:

```
[price] "what's the price of OP?"   live_info=True   entities=['OP']   ← resolved to the NEPSE holding
   TOOL web_search args={'query': "what's the price of OP?"}      ← raw utterance, no entity context
   TOOL web_search mode=capability_repair
   reply="Right now, Optimism (OP) is trading at about $0.0989..."  ← the crypto token
```

`ResponseGenerator._capability_repair` sends `prompt.utterance` verbatim as the search query:

```python
result = await dispatcher.run_inline(
    ToolCall(tool_id="web_search", args={"query": prompt.utterance}), context
)
```

The resolved entity (`OP` → *"a stock ticker in the user's NEPSE share portfolio"*) is in the prompt
context but never reaches the search query. Serper disambiguates "OP" globally → Optimism.

**So even a perfect fixture yields a wrong answer.** The repair search must be entity-aware.

---

## Defect 4 — the "clarifying question" is model assistant-speak, not the guardrail

The F5 hypothesis was that the reply was the ambiguity guardrail working as designed. It was not:

- `PromptAssembler.assemble("what's the current LTP of OP?")` returns an **`AssembledPrompt`**, not a
  `DisambiguationRequest`.
- The turn's `action` is **`"respond"`**, not `"disambiguate"`.
- `_disambiguate()` produces a fixed shape — `Quick check — do you mean "X" or "Y"?` — which does not
  match the observed reply.

The observed reply (reproduced 3/3, deterministic — the `response` call is a prompt-cache hit):

> *"I'm sorry, Nandi, I don't have enough information to get you the current LTP for "OP." Do you mean
> a stock symbol, or something else?"*

And with OP seeded, it opens:

> *"**Companion here!** I see in your memory that you bought 50 shares of OP at 412 in your NEPSE
> portfolio—is that the one you're wondering about?"*

Both are the model clarifying an obviously-clear message, and `"Companion here!"` is service-desk
phrasing. `core/eval/judge.py`'s own rubric **hard-fails** exactly this ("clarifying an obviously-clear
message … instead of engaging"). The style gate did not flag it (`style_flags=[]`).

---

## Two further problems found while diagnosing

- **Empty reply.** With no prior context, `"what's the current LTP of SYPNL?"` produced
  `final_text=''` — the user hears *nothing*. Once a price is in memory, the same query recites it.
- **Stale price served as current.** After an earlier turn put *"SYPNL ≈ 1,373 NPR"* into memory, the
  LTP query (which never searches) recited that number as the current price, with no freshness check.
  A memorized price must never be spoken as live data.

---

## What to fix (in order), and what to verify

1. **Make live-info detection intent-based, not keyword-based.** The regex is unmaintainable — it
   already needs `LTP`, `trading at`, `quote`, `last traded`, `how much is … worth`. Prefer routing on
   the `context_intent` step's existing `needs_live_info` / `live_query` output (it already computes
   exactly this and is discarded on the voice path), or a cheap classifier. **Do not just add "LTP" to
   the regex** — that repeats the defect for the next phrasing.
2. **Never let `streamable` skip the tool loop for a turn that might need a tool.** Today the streaming
   fast-path is chosen *before* any reasoning about tools. Bias toward the agentic path when unsure.
3. **Make `_capability_repair` entity-aware**: build the search query from the utterance **plus** the
   resolved entities/portfolio context (e.g. `"OP NEPSE share price"`), not the raw utterance.
4. **Ground price answers in a search, never in memory.** A price recalled from episodic memory must be
   re-verified or explicitly time-qualified ("when we last looked, it was …").
5. **Seed the sample user's real holdings** so SRC1 has a truthful fixture. Do not invent holdings —
   ask the user which tickers are actually theirs.
6. **The style gate missed `"Companion here!"` and the clarify-shape.** Add both to
   `core/reasoning/style.py::find_forbidden`.

**Verification bar:** with a truthful fixture, `"what's the current LTP of OP?"` must (a) fire exactly
one `web_search`, (b) whose query is entity-qualified, (c) return the NEPSE price, (d) never say
"Optimism"/crypto, (e) never ask a clarifying question, and (f) score ≥4 with `chatbot_like=false` on
the companion-voice judge — measured through `VoiceSession`, not `orchestrator.generate_spoken`.

---

## Why the test estate said this passed

`GAP_ANALYSIS.md` recorded SRC1 as passing with LLM-judge 1.0. It could not have exercised this path:
`tests/golden/test_gs3_judge.py` scores **canned reply strings** from `gs3_judge.json`, never the
engine's output, and nothing in `tests/` drives `VoiceSession`. See F4 in `docs/TEST_REPORT.md` for the
full list of claims that were verified only via the harness path and are therefore **unverified**.
