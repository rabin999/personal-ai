# Verified Retrieval Pipeline — Build Report

A self-contained "read the page, don't trust the snippet" verification pipeline behind
`ports/retrieval.py`. Given a resolved query it searches, fetches and RENDERS the real
pages, checks whether the answer is corroborated across independent domains and is current,
and returns a typed `VerifiedResult` the voice engine can speak. Built in an isolated
worktree on branch **`feat/verified-retrieval-crawl4ai`**; not merged (main-line's job).

I did **NOT** change `ports/retrieval.py`. No contract change was needed.

---

## 1. The pipeline, stage by stage

```
query ─► SEARCH ─► SELECT ─► FETCH(parallel) ─► EXTRACT ─► VALIDATE(recency) ─► CROSS-CHECK ─► FORMAT ─► VerifiedResult
```

| Stage | Module | What it does |
|------|--------|--------------|
| **S1 SEARCH** | `select.py` (via injected `ports.search.SearchProvider`) | Reuses the existing **Serper** adapter (primary). Time-sensitive queries bias to `recency="week"`. Not reimplemented — injected. |
| **S2 SELECT** | `select.py` | Drops junk/link-farms/query-echo, de-dupes by domain (one page per domain — corroboration counts *independent* domains), drops non-HTML (`.pdf`…). Orders candidates; top-2 first, widen to a 3rd only if not corroborated. |
| **S3 FETCH** | `fetch.py` | `POST /crawl/stream` to the Crawl4AI Docker server (NDJSON). Crawl4AI decides JS-render vs static. Per-URL failure → clean per-source `SourceError`, pipeline proceeds. Whole server down → `server_down` on every page → honest degrade. |
| **S4 EXTRACT** | `extract.py` | Uses BM25-relevance-filtered `fit_markdown`; rejects pages under `word_count_threshold` (thin/paywall/blocked); keeps `metadata` + the **final** URL. Then extracts the one short answer per source: deterministic for numbers/prices, a small fast LLM otherwise (may answer `NONE` — topic-match ≠ answer-match). |
| **S5 VALIDATE/RECENCY** | `recency.py` | Extracts a page date (metadata → `<meta>` article:published_time/og:updated_time/JSON-LD datePublished → URL path). Distinguishes **page-date vs fact-date**. For time-sensitive queries a stale page is a NEGATIVE signal: fresh corroboration drops the stale ones; if *all* are stale it keeps them but flags `is_stale` (corroborated-but-stale). |
| **S6 CROSS-CHECK** | `crosscheck.py` | Pure corroboration, **not** credibility scoring. Clusters answers (numbers canonicalised, text fuzzy-matched), de-dupes syndicated/near-identical text across domains → ONE source. Emits `corroborated` (≥2 independent domains) / `single_source` / `conflicting` (surface all) / `not_found`. |
| **S7 FORMAT** | `format.py` | A small fast LLM (`gemini-2.5-flash`, temp 0.1) turns the verified finding into one spoken sentence (+ structured JSON when `want_json`, Pydantic-validated). A FORMATTER, not a researcher — only states what sources support, carries provenance. Cost logged to the Cost Ledger. Zero/conflict/error rendered deterministically (no model can invent a fact). |
| **tracing** | `trace.py` | Every stage emits a span (brief description + duration + key data) to the project's `StructuredLogger` and/or durable `TraceStore`, fire-and-forget, never blocking. |

### Crawl4AI version + API verified
Verified against **docs.crawl4ai.com v0.9.x** and confirmed live against the running server:
- `POST /crawl` (sync) and `POST /crawl/stream` (NDJSON, final `{"status":"completed"}` marker) — https://docs.crawl4ai.com/core/docker-deployment/ , https://github.com/unclecode/crawl4ai/blob/main/deploy/docker/README.md
- POST body dump shape `{"type":"BrowserConfig"/"CrawlerRunConfig","params":{…}}` — confirmed live.
- `CrawlResult` fields `success` / `url` / `markdown.{raw_markdown,fit_markdown}` / `html` / `cleaned_html` / `metadata{title,description,keywords,author}` / `status_code` / `error_message` — https://docs.crawl4ai.com/core/crawler-result/ (confirmed live: markdown is an object with `raw_markdown`+`fit_markdown`; metadata carries **no date**, so S5 parses the HTML).
- `arun_many(urls, config, dispatcher)` streaming + `MemoryAdaptiveDispatcher(memory_threshold_percent, max_session_permit)` — https://docs.crawl4ai.com/api/arun_many/
- `BM25ContentFilter(user_query=…)` / `PruningContentFilter` via `DefaultMarkdownGenerator(content_filter=…)` → `markdown.fit_markdown` — https://docs.crawl4ai.com/core/fit-markdown/
- v0.9.0 secure-by-default token/loopback/SSRF — https://docs.crawl4ai.com/core/self-hosting/

**Version reality (honest):** the `unclecode/crawl4ai:0.9.1` image ships **without** the Playwright chromium-headless-shell binary and its worker crashes on boot (`Executable doesn't exist … chrome-headless-shell`). The **identical** API (`/crawl`, `/crawl/stream`, `arun_many`, `BM25ContentFilter`) is present in **0.8.6**, which boots healthy and renders. The deploy pins **0.8.6**; revisit when a fixed 0.9.x image lands.

---

## 2. The `VerifiedResult` contract as implemented

The adapter (`adapters/retrieval/crawl4ai_adapter.Crawl4AIRetrieval`) implements
`RetrievalPort.verify(query, *, time_sensitive=None, want_json=False, max_sources=3,
budget_ms=None) -> VerifiedResult` **exactly** and is checked against the `RetrievalPort`
Protocol. It returns the frozen `VerifiedResult` with `status` / `answer` / `confidence` /
`sources` (provenance always travels) / `corroboration_count` / `recency` / `conflict` /
`formatted_voice` / `formatted_json` / `timings` / `errors`. `ports/retrieval.py` is
untouched. Zero and conflicting are first-class returns; only a pipeline **bug** raises
`VerifiedRetrievalError` — source/dependency failures degrade.

---

## 3. §5 edge-case matrix → behaviour → proving test

| Edge case | Behaviour | Test |
|-----------|-----------|------|
| 404 / dead domain | per-source error, proceed on others | `test_dead_source_does_not_fail_the_query` |
| timeout / refused / unexpected crawl error | clean `SourceError`, proceed | live: `test_headline…` (asianews.network blocked, proceeded) |
| bot-wall (403/CAPTCHA/Cloudflare) | skip + note + use others | live headline (anti-bot page skipped) ; `test_dead_source…` |
| empty / JS-never-settles / paywall teaser | thin-reject, don't trust truncated fact | `test_thin_content_is_rejected` |
| redirect | use the **final** url | `test_redirect_uses_final_url` |
| PDF / non-HTML | dropped at SELECT | `test_select_dedupes_domains_and_drops_junk_and_pdf` |
| ALL sources fail | `status="error"`, honest line, never fabricate | `test_all_sources_fail_returns_error_never_fabricates` ; live `NEPSE` |
| topic-match without answer-match | extractor returns `NONE` → not counted | `ScriptedExtractor` None paths in `test_single_source` |
| two agree but both stale | corroborated **but** `is_stale` flagged, not dropped | `test_corroborated_but_stale_is_flagged_not_dropped` |
| conflict | `conflicting`, surface all claims | `test_conflicting_surfaces_both` |
| near-dup across domains (syndication) | counts as ONE source | `test_cross_check_dedupes_syndicated_text` |
| number / price / date | deterministic extraction | `test_numeric_query_and_number_extraction` |
| no date on page | unknown, flagged (not assumed fresh) | `test_extract_page_date…` / `test_is_stale_only_when_time_sensitive` |
| recent page, old event | page-date ≠ fact-date (page date only) | `recency.py` docstring + `test_extract_page_date…` |
| time-sensitive → recency HARD filter | stale down-weighted/dropped | `test_recency_drops_stale_when_fresh_corroboration_exists` |
| per-query total timeout / fast-path budget | stop widening, return verified, mark partial | `test_budget_stops_widening_and_marks_partial` ; live `test_fast_path_budget_is_measured` |
| Crawl4AI down | degrade to snippet-only or honest "couldn't verify", never crash | `test_crawler_down_degrades_to_snippet_only` |
| search provider down | honest error line, never crash | `test_search_provider_down_degrades_honestly` |
| non-HTML/junk domains | dropped before fetch | `test_select_…` |
| pipeline BUG (our code) | fail loudly as `VerifiedRetrievalError` | resilience guard in `verify()` (PROGRAMMING_ERRORS) |

**Mutation-proofs (a check that can't fail is not a check):**
- `test_mutation_corroboration_threshold_is_the_check` — flips `_CORROBORATION_MIN` 2→1; a single source then wrongly reads `corroborated`. The ≥2 check is load-bearing.
- `test_mutation_recency_filter_is_the_check` — patches `is_stale`→False; the STALE majority answer then wrongly wins on a time-sensitive query. The recency filter is load-bearing.

---

## 4. Cardinality proofs (verbatim `VerifiedResult`, deterministic harness)

**CORROBORATED**
```json
{ "status": "corroborated", "answer": "Sushila Karki", "confidence": 0.75,
  "sources": [
    {"url":"https://apnews.com/article","domain":"apnews.com","published_date":"2026-06-01","snippet":"Sushila Karki"},
    {"url":"https://reuters.com/article","domain":"reuters.com","published_date":"2026-06-02","snippet":"Sushila Karki"}],
  "corroboration_count": 2,
  "recency": {"most_recent_source_date":"2026-06-02","is_time_sensitive":false,"is_stale":false},
  "conflict": null,
  "formatted_voice": "Sushila Karki — confirmed across 2 sources like apnews.com.", "errors": [] }
```
**SINGLE_SOURCE**
```json
{ "status": "single_source", "answer": "Sushila Karki", "confidence": 0.5,
  "sources": [{"url":"https://apnews.com/article","domain":"apnews.com","published_date":"2026-06-01","snippet":"Sushila Karki"}],
  "corroboration_count": 0,
  "formatted_voice": "According to apnews.com, Sushila Karki. I only found one source for it.", "errors": [] }
```
**CONFLICTING**
```json
{ "status": "conflicting", "answer": null, "confidence": 0.3,
  "sources": [{"domain":"a.com",...},{"domain":"b.com",...}],
  "conflict": [{"source":"a.com","claim":"Alice"},{"source":"b.com","claim":"Bob"}],
  "formatted_voice": "Sources disagree on this — a.com says Alice, while b.com says Bob. I won't pick one for you." }
```
**NOT_FOUND**
```json
{ "status": "not_found", "answer": null, "confidence": 0.0, "sources": [],
  "formatted_voice": "I looked, but I couldn't find a reliable source for that, so I won't guess." }
```

---

## 5. THE headline case — end-to-end, LIVE (real Serper + real render + real LLM)

Query: *"who is the current prime minister of Nepal"* (time_sensitive). This is the case the
app kept failing (stale officeholder). VERBATIM:

```
status=corroborated  confidence=0.75  corroboration=2
answer='Balendra Shah'
voice='Sources confirm that Balendra Shah is the current prime minister of Nepal.'
recency: time_sensitive=True stale=False most_recent=2026-07-10
  source: thediplomat.com          date=2026-07-10  url=https://thediplomat.com/2026/07/the-power-struggle-at-the-heart-of-nepals-ruling-party/
  source: freemalaysiatoday.com    date=2026-07-05  url=https://www.freemalaysiatoday.com/category/world/2026/07/05/nepals-pm-marks-100-days-with-sweeping-changes
  error: asianews.network -> Blocked by anti-bot protection (skipped, proceeded)
timings: search=2.6s fetch=14.9s extract=3.1s total=21.8s
```

Corroborated across **2 independent domains**, both **recent** (within days), one bot-walled
source cleanly skipped, provenance + dates attached. The NEPSE-ticker case (NABIL LTP) hit a
bot-walled market site (merolagani) and returned an honest `status="error"` — **it did not
fabricate a number or return a crypto token.** Server-side render (Wikipedia → "Canberra")
and JS render (quotes.toscrape.com/js → 237 words incl. JS-injected "Einstein") both proven.

---

## 6. Latency — N=5, median + p95 (real runs)

**Background path** (full verify, `"capital of France"`, N=5):
```
samples_ms = [12489, 12818, 13324, 13474, 16205]
median_ms = 13324    p95_ms = 13474
```
Per-stage on a typical run: **search ≈ 1.5–3.7s, fetch ≈ 5–15s (dominant), extract ≈ 1.5–3.3s.**
Rendering 2–3 pages is the cost, and it is seconds — so verify runs on the **background/waiter
path by default** ("let me check that properly…"), exactly as the contract requires. It cannot
sit in the blocking reply path.

**Fast path (`budget_ms`)**: honest limitation — `budget_ms` gates **widening** (stops after
the first wave) and marks the result `partial`; it does **not** abort an in-flight browser
render (a single render can't be sub-second). `test_fast_path_budget_is_measured` shows
`budget_ms=1` returns a partial `single_source` ("Paris") with the `partial: budget_ms
exceeded` marker rather than blocking indefinitely. A meaningful inline fast path wants
`budget_ms` ≈ one-fetch latency (~5–8s) for a single high-confidence source; below that,
degrade to snippet-only.

---

## 7. Deploy shape and how it's wired behind the port

**Chosen: the Crawl4AI Docker service on `127.0.0.1:11235`** (not the in-process library).
Why: keeps Playwright/Chromium out of the app venv (no browser deps added to
`pyproject.toml` — the app talks HTTP), matches the box's docker-compose conventions, and the
streaming/job-queue endpoints fit the background path. Config: `deploy/crawl4ai/docker-compose.yml`
— pinned `0.8.6`, `--shm-size=1g`, 4g memory limit, **bound to loopback only**, `CRAWL4AI_API_TOKEN`
(≥32 chars) set and sent as `Authorization: Bearer`.

Wiring: `adapters/retrieval/fetch.Crawl4AIClient` (implements the internal `PageFetcher`
protocol) is the only thing that knows about `:11235`. The orchestrator sees only
`PageFetcher`, so a fake fetcher drives the harness deterministically and a different render
backend is a one-line swap. `build_crawl4ai_retrieval(...)` is the composition root's single
wiring call (inject the shared `SearchProvider` + `LLM` + `CostLedger` + optional
`StructuredLogger`/`TraceStore`). `core/` never imports the adapter (import-linter: **KEPT**).

---

## 8. Honest limits — what it blocks on / can't verify / is undone

- **Fetch latency dominates (5–15s).** Must run on the background/waiter path; no true
  sub-second inline path for a fresh render. Fast path is "first-wave + partial", not preemption.
- **Bot-walled sites** (Cloudflare / anti-bot / aggressive paywalls, and several NEPSE market
  sites) are skipped as thin/blocked. When *every* candidate is walled the result is an honest
  `error`, never a fabrication — but the fact goes unverified.
- **Auth not enforced by the 0.8.6 image** from the env token alone (upstream issue #1442);
  the **loopback bind is the real guard**. Production must enable `security.jwt_enabled` in
  `deploy/docker/config.yml`. Documented in the compose file.
- **0.9.1 image is broken** (missing browser); running on 0.8.6 with the identical API.
- **`conflicting` in the wild is rare** to reproduce deterministically with live search, so it
  is proven on fixtures (verbatim above) plus the pure-function `cross_check` tests; the live
  suite covers corroborated / single_source / not_found / error.
- **No `partial` field in the frozen contract** — a budget overrun is signalled via an
  `errors` entry (`partial: budget_ms exceeded`) and reduced confidence, not a status flag.
  Called out here rather than changing the contract (no contract change requested).
- Not merged, not deployed to production `202.58.120.93` — that is the main-line's gated last step.
