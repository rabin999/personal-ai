"""Env-driven configuration (design doc §17.3).

Secrets and connection strings come from the environment / ``.env`` — never
hard-coded (see ``.env.example`` for the required keys). Behavior params do
NOT belong here; they live in the profile/registry (spec §2).
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # OpenRouter — one key covers LLM, STT, and TTS (design doc §10.1)
    open_router_api_key: str = ""
    open_router_base_url: str = "https://openrouter.ai/api/v1"

    # Web search (spec §15) — filled in when the module is built
    serper_api_key: str = ""
    brave_api_key: str = ""

    # Datastores — defaults match docker-compose.yml for local dev
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "companion"
    qdrant_url: str = "http://localhost:6333"
    # Embedding model for episodic/entity vectors (§5): local fastembed
    # (OpenRouter exposes no embeddings endpoint). embedding_dim must match
    # the model; changing it means recreating the Qdrant collections.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # LLM used by Graphiti for entity/fact extraction (§6); overridable per
    # deployment. Routed through OpenRouter like all LLM traffic.
    # gemini-2.5-flash extracts reliably where gpt-4.1-mini kept emitting
    # edges to entities outside its node list (all dropped by Graphiti).
    graphiti_llm_model: str = "google/gemini-2.5-flash"
    graphiti_small_model: str = "google/gemini-2.5-flash"
    # Per-request timeout for LLM calls; fallback chain handles failures.
    llm_timeout_s: float = 60.0

    # Preference memory (Mem0, brief §2): fast personalization layer. Its own
    # extraction runs on this cheap model; embeddings are local fastembed and the
    # store is our Qdrant. Disable to run without it.
    preference_memory_enabled: bool = True
    preference_model: str = "google/gemini-2.5-flash-lite"

    # Voice runtime (CLAUDE.md §5): "native" (the asyncio loop) or "pipecat" (the
    # framework-owned pipeline/VAD/barge-in). Native is default until the Pipecat
    # path is verified end-to-end in the browser; the /ws/voice-pipecat endpoint
    # exists for that verification regardless of this flag.
    voice_runtime: str = "native"

    # Dev convenience: run the background worker (§14 consolidation/search/tool
    # tasks) as an in-process asyncio task inside the API instead of a separate
    # process, so `uvicorn api.app:app` runs everything. Keep FALSE in production
    # — there the worker is a separate process for latency + failure isolation.
    run_worker_in_process: bool = False

    # Application logging transport (brief Part B): pluggable, config-driven sinks.
    # Comma-separated list of active sinks: "file", "stdout" (both may be on).
    log_sinks: str = "file"
    log_file_path: str = "logs/companion.jsonl"

    # Speech synthesis (§23): Grok Voice TTS via the xAI TTS API
    # (https://api.x.ai/v1/tts) — the spec's chosen voice. Inline delivery
    # tags supported; ~$4.20 / 1M chars. Key env var is ``X-AI-API``.
    xai_api_key: str = Field(default="", validation_alias="X-AI-API")
    xai_base_url: str = "https://api.x.ai/v1"
    tts_voice: str = "eve"  # ara | eve | leo | rex | sal
    tts_language: str = "en"
    tts_timeout_s: float = 30.0

    # STT (§20): faster-whisper local model size. "tiny"/"base" are fast on
    # CPU (real-time-ish); "small"/"medium" are more accurate but slower.
    stt_model_size: str = "base"

    # SER (§22): self-hosted emotion2vec microservice on a small GPU box
    # (design doc §17.3) — separate service, its own hardware. Empty means
    # SER is disabled (acoustic emotion deferred; text-sentiment only).
    ser_service_url: str = ""
    ser_timeout_s: float = 3.0

    # ── Authentication (design doc §18/§26): real Google SSO replaces the static
    # bearer-token stub. Secrets from env; never hard-coded. ──────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    # PUBLIC url the browser + Google see (behind the reverse proxy). The OAuth
    # redirect URI is built from THIS, not the internal request host, so Google
    # never sees http://localhost and returns redirect_uri_mismatch (brief §0).
    public_base_url: str = "http://localhost:8000"
    # Signs the session cookie (Starlette SessionMiddleware). MUST be strong in
    # prod; a random dev default keeps local runs working without config.
    session_secret: str = "dev-insecure-session-secret-change-me"
    # Session cookie lifetime (seconds) and secure flag. secure=True in prod
    # (HTTPS only); auto-derived from PUBLIC_BASE_URL scheme unless overridden.
    session_max_age_s: int = 14 * 24 * 3600
    session_cookie_secure: bool | None = None  # None → derive from public_base_url

    # Conversation behaviors (§8/§14). Max background results delivered at a single
    # pause before we summarize-and-offer instead of dumping them (anti-machine-gun).
    delivery_max_interjections: int = 2

    # Deferred memory routing (Item 9): when True (default), the live turn only
    # writes the raw log; the episodic/semantic/procedural routing is done by the
    # background worker via the raw-log cursor (kills double-writes, off the latency
    # path). False = legacy inline extraction on the live path.
    defer_memory_routing: bool = True
    # How often the worker polls the raw log for unrouted turns (seconds).
    memory_routing_poll_s: float = 2.0

    # Cost-ceiling enforcement (§10): hard per-turn spend cap. If a turn's LLM
    # spend crosses this, the tool/reasoning loop STOPS and answers with what it
    # has (a cost_ceiling span is traced) — a runaway loop can't burn the budget.
    max_turn_cost_usd: float = 0.50

    # Welcome-email SMTP (fastapi-mail via Gmail). Gmail requires an APP PASSWORD
    # (2FA enabled) — the normal account password will NOT work. Empty MAIL_*
    # disables sending; the outbox still records (worker marks it skipped).
    mail_username: str = ""
    mail_password: str = ""
    mail_from: str = ""
    mail_from_name: str = "Your Companion"
    mail_server: str = "smtp.gmail.com"
    mail_port: int = 587
    mail_starttls: bool = True
    mail_ssl_tls: bool = False

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "companion-dev"
    redis_url: str = "redis://localhost:6379/0"
    # Task-queue key namespace (§14). Tests pass a unique namespace so an
    # isolated queue is never drained by a live worker on the default one.
    queue_namespace: str = "companion"

    @property
    def cookie_secure(self) -> bool:
        """Whether the session cookie is HTTPS-only. Explicit override wins; else
        derived from PUBLIC_BASE_URL (https in prod → secure)."""
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.public_base_url.lower().startswith("https")

    @property
    def google_redirect_uri(self) -> str:
        """The OAuth redirect URI Google must be registered with — built from the
        PUBLIC base url, NOT the internal request host (brief §0)."""
        return f"{self.public_base_url.rstrip('/')}/auth/google/callback"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
