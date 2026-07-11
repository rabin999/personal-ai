# Retrieval policy — what the model may answer from training vs what must be verified

**Purpose.** Decide, from first principles, which information the companion may answer from the
model's **parametric (training) knowledge** and which it **must search & verify before replying** —
so it never speaks a stale/guessed fact (e.g. "Prachanda is PM" when he isn't). This is the design
the search-gating is built on; individual bug fixes (stale officeholder, prices, news) are instances
of it.

## The principle (why this is needed)

Parametric knowledge has two hard limits (corroborated by the hallucination/RAG literature):
1. **It freezes at the training cutoff.** Any fact about a state/event *after* the model's cutoff is
   simply unknown — the model will still answer fluently (hallucinate) with no built-in verification.
2. **It is least reliable for precise, volatile facts** — names, numbers, dates, prices, offices —
   exactly the facts that change. And models **cannot reliably tell** whether their memory is current,
   so they over-trust it and pick the parametric answer over retrieval.

**Therefore the SYSTEM must gate retrieval — never the model's own self-assessment of "do I know
this?"** The model doesn't know what it doesn't know, and doesn't know its own cutoff.

## The information taxonomy (four buckets)

**A. Answer from training (parametric) — no search.** Stable across time and settled before the
cutoff:
- Reasoning, math, logic, coding; language; definitions; how-to/procedures.
- General/established knowledge: science, geography basics, history *before the cutoff*, culture.
- The companion's own nature/persona/values.
- *Reliable because the answer does not change and was well-represented in training.*

**B. MUST verify (search) before replying — volatile / time-sensitive / post-cutoff.** The model
must NOT answer these from memory, even if it "knows" one:
- **Officeholders / who-holds-role**: PM, president, CEO, mayor, champion, title-holder.
- **Market/values**: prices, LTP, stock/crypto, exchange rates, valuations, "trading at".
- **Weather / forecasts.**
- **News / current events / "what's happening" / breaking stories** (e.g. a recent plane crash).
- **Sports**: scores, results, standings, "who won".
- **Schedules / availability**: is X open now, opening hours, showtimes, event timing.
- **Recency-marked**: anything with *current / latest / now / today / recent / still / these days /
  newest / this week|month|year*.
- **Anything whose truth could have changed after the model's cutoff** — the default-verify class.

**C. MUST use the USER's memory — personal, never invent.** The user's own life:
- Their name and what they call the companion; people in their life; preferences; their projects;
  their portfolio/holdings; things said in past conversations.
- Retrieve from episodic/semantic memory; if it isn't there, say so — never fabricate (see D-19).

**D. Ambiguous / mixed — resolve, then route.** e.g. *"what do you think about the current PM of
Nepal?"* = an **opinion wrapper around a volatile fact**. The current fact (who the PM is) is bucket
B → verify it FIRST, then give the take. Do not let the opinion framing ("what do **you** think")
suppress the verify. Resolve the entity (bucket C/§8) before deciding.

## The decision rule (gating algorithm)

Classify by the **answer's class**, not the surface phrasing:
1. Is the answer about the **user's own life**? → bucket C (user memory; never invent).
2. Could the answer **change over time OR post-date the cutoff** (officeholder/price/weather/news/
   score/schedule/recency-marked/"could be different now")? → bucket B: **MUST search, and answer
   ONLY from the results.**
3. Is it a mixed/opinion-wrapped volatile fact? → verify the fact (B), then reason.
4. Otherwise (stable knowledge / reasoning) → bucket A: answer from parametric knowledge.

**The invariant that fixes the stale bug:** on a bucket-B query the model must **never emit a
parametric answer as the final reply**. The pre-search draft is discarded; the answer comes from
`_REPAIR_INSTRUCTIONS` over the search results, or an honest "I couldn't verify that" — verify
**before** answering, not answer-then-correct.

Conservative by design: **a false search costs a second; a stale fact costs trust.** When unsure
between B and A for an external-world fact that *could* be current, choose B.

## How this maps onto our engine (and the gaps to close)

- `core/reasoning/volatility.py` `is_volatile_question` + `_is_live_info_query` + the LLM
  `needs_live_info` classifier already approximate bucket B. Gaps: (i) the classifier is
  nondeterministic (D-4); (ii) even when a turn is flagged volatile, the agentic path can still ship
  the model's **pre-search draft** (the stale-PM bug) — the invariant above is not enforced.
- **Build:** (1) enforce verify-before-answer on bucket-B turns (never ship the pre-search draft;
  force the search + answer from results). (2) Treat the classifier's job as **answer-class
  detection**, widen the deterministic backstop to the full bucket-B list, and keep the LLM
  classifier only as an OR-signal. (3) Bucket C stays memory-only + honest-not-found.

## How to prove it (measurable, per the eval rules)

- A **labeled taxonomy set**: ~150 queries tagged A/B/C/D (officeholders, prices, weather, news,
  scores, schedules, user-life, math, definitions, opinions-on-volatile-facts, traps like
  "is it worth learning Rust?" = A). Measure classifier precision/recall per bucket.
- **Invariant test (mutation-proof):** no bucket-B query ever ships a final answer without a
  `web_search`/verified-retrieval span in the trace. Break it (let the draft ship) → a test goes red.
- **Real-drive, N≥5, both callers:** "current PM of Nepal", a live price, "recent plane crash",
  "what do you think about the current PM" — all must search first; none answer stale.

## Sources
- Parametric memory freezes at cutoff + hallucination without verification; time-sensitive validation
  is the key retrieval trigger; LLMs are unreliable for precise facts from memory and struggle to
  select parametric vs retrieved. (Hallucination survey, arXiv 2510.06265; PAIRS, arXiv 2508.04057;
  Query-Optimization for parametric refinement, arXiv 2411.07820; ReDeEP, arXiv 2410.11414.)
