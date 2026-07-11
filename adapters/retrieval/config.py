"""Config for the verified-retrieval adapter (config over code, CLAUDE.md §3.6).

Behaviour knobs (timeouts, thresholds, concurrency cap, the Crawl4AI endpoint and
its auth token) come from the environment via ``CRAWL4AI_*`` vars — never hard-coded
in the pipeline. This is the adapter's OWN settings object; it deliberately does NOT
touch ``config/settings.py`` (shared engine config) so the retrieval build stays
isolated behind the port.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class RetrievalConfig(BaseSettings):
    """Env-driven config for the Crawl4AI verified-retrieval pipeline."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="CRAWL4AI_", extra="ignore"
    )

    # ── Crawl4AI Docker service (deploy/crawl4ai) ────────────────────────────
    # Loopback by default — the server is NEVER exposed publicly (brief deploy rules).
    base_url: str = "http://127.0.0.1:11235"
    # Secure-by-default auth token (v0.9.0). MUST be >= 32 chars in any real deploy;
    # empty means "no auth" which the server only permits on a loopback bind.
    api_token: str = ""
    # Per-URL page render/settle timeout (ms) handed to the crawler.
    page_timeout_ms: int = 12_000
    # HTTP client timeout for the whole fetch batch (ms) — a hard ceiling so a hung
    # server can never wedge the turn; we take whatever streamed back before this.
    fetch_deadline_ms: int = 20_000

    # ── Selection / fetch shape ──────────────────────────────────────────────
    # Global concurrency cap (server dispatcher max_session_permit) — polite crawling.
    max_concurrency: int = 4
    # Start with this many sources; widen to max_sources only if not corroborated.
    initial_sources: int = 2
    # Thin-content reject: a page under this many words is blocked/paywalled/empty.
    word_count_threshold: int = 40

    # ── Recency ──────────────────────────────────────────────────────────────
    # A time-sensitive answer older than this many days is a NEGATIVE signal.
    stale_after_days: int = 120

    # ── Formatter LLM (S7) ───────────────────────────────────────────────────
    # A small fast model — a FORMATTER, not a researcher. Low temperature.
    formatter_model: str = "google/gemini-2.5-flash"
    formatter_temperature: float = 0.1
