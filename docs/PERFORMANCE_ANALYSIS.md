# Performance analysis — where the latency & cost go, and how to cut them

Grounded in real measurements taken this session (real drives + traces), not guesses. Ranked by
impact / effort. "Turn" = one user message → spoken reply.

## Measured baseline (what a turn actually does)

- A **simple greeting** ("Hello, brother!") makes **2 successful LLM calls** on the paid models
  (`context_intent` + `response`), and up to **4** on the free fallback (`response` JSON fails
  validation twice → `response_plain`). Source: real drive, `r.purposes`.
- During the credit outage, **every** LLM call walks the whole tier chain
  `haiku(402) → gpt-4.1-mini(402) → gemini-flash-lite(402) → gpt-oss-20b:free(ok)` — i.e. **3 wasted
  round-trips per call**. A greeting logged **9 fallback-failure spans** on top of 4 real calls.
- Free-fallback reasoning effort: **default ≈17 s/call, `medium` ≈12 s, `low` ≈3–7 s** (measured).
  Already set to `low` for the fallback this session.
- Frontend bundle: **531 KB** single chunk (trips Vite's 500 KB warning; react-force-graph added ~300 KB).
- Prompt caching (`cache_control`) is wired **only for Anthropic** models — active when haiku/sonnet
  is primary, absent on gpt/gemini/gpt-oss.
- Memory extraction, psych consolidation, and background delivery already run **off the reply path**
  (async) — good, leave as is.

## Ranked enhancements

### 1. Circuit-break the dead-provider chain  ·  HIGH impact · LOW effort
Every call re-tries the same 3 dead paid models before reaching the free one — 2–4 calls/turn × 3
wasted 402 round-trips ≈ **8–12 pointless requests per turn** right now. Add a short-lived circuit
breaker in `OpenRouterLLM`: when a model returns 402/hard-down, mark it "open" for ~30–60 s and skip
it at the head of the chain, jumping straight to the first healthy model. Saves the wasted latency on
every subsequent call this turn (and any outage), with no behavior change. This is the single biggest
current win.

### 2. Fewer LLM calls on simple turns  ·  HIGH impact · MED effort
`context_intent` (reference/emotion/needs-live-info classification) runs **every** turn, even a bare
"hello" with nothing to resolve.
- **Skip `context_intent`** for trivially-simple turns (short greeting/social, no question mark, no
  resolved entity, no live-info cue) via the existing deterministic `_complexity_hint` + a cheap
  regex → 1 call instead of 2 on the most common turns.
- **On the fallback tier, skip the dual judgment-JSON** and use the plain-reply path directly:
  gpt-oss fails the complex `draft_response`/`judgment`/`tool_request` schema → 2 retries + a plain
  fallback (3 calls). Detect a `:free`/reasoning-mandatory model and go straight to plain reply (1
  call). Cuts the fallback turn from 4 calls to ~2.

### 3. Maximize prompt caching  ·  MED impact · LOW effort
The stable prefix (identity + traits + persona + how-to-answer) is cache-controlled for Anthropic, so
haiku turns should be re-reading it from cache. Verify the **cache-hit rate** in traces
(`cached_tokens`) and make the `cache_prefix` as large and byte-stable as possible (any per-turn value
that leaks into the prefix breaks the cache). Cheap tokens + lower TTFT on every haiku turn.

### 4. Explicit recency ranking + incident-merge in retrieval  ·  MED impact · MED effort
Recency today = Serper's day/week bias + `recency.py` staleness-drop. For a "what's the latest on X"
turn, additionally **sort the surviving candidates by extracted page-date, newest-first**, and when
several corroborating sources describe the **same recent event within a few days**, merge them into
one summary rather than treating them as separate facets (user request on the "missing plane" case).

### 5. Trim the assembled prompt  ·  MED impact · MED effort
A large system prompt = more input tokens = higher TTFT and cost on every call. Audit the assembled
prompt size per turn (it now carries identity, traits, persona, psych, rules, facts, episodic,
project, recall). Drop sections that are empty/low-value for the turn's complexity tier; cap episodic
+ facts to the top-k that actually matter.

### 6. Code-split the frontend  ·  LOW impact · LOW effort
`react-force-graph-2d` (~300 KB) is only used on `/graph`. `dynamic import()` it so the initial
Companion page loads a ~230 KB bundle instead of 531 KB — faster first paint on mobile.

### 7. Cache volatile-but-slow-moving answers briefly  ·  LOW impact · LOW effort
Officeholder/"who is the PM" answers change rarely but cost a full search each time. A short TTL
(hours) per-answer cache (keyed by normalized query) keeps freshness while skipping repeat searches —
`search_cache` already exists; extend it to the composed answer for the officeholder class.

## Already done this session (for reference)
- Free-fallback reasoning effort → `low` (17 s → 3–7 s/call); token floor so the reply survives.
- Fallback settings separated from the primary models (`_FALLBACK_MODEL_SETTINGS`).
- Progressive search runs facets **concurrently** (N lookups ≈ one round of latency); queries
  de-duplicated + date-stripped so no redundant searches.
- No-silent-failure guard; honest outage line; verify-before-answer.

## Suggested order
1 → 2 → 3 are the highest value-per-effort and compound (fewer calls × faster each × cached prefix).
1 is a quick, self-contained win that helps most **right now** during the credit outage.
