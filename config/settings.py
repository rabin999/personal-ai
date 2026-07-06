"""Env-driven configuration (design doc §17.3).

Secrets and connection strings come from the environment / ``.env`` — never
hard-coded (see ``.env.example`` for the required keys). Behavior params do
NOT belong here; they live in the profile/registry (spec §2).
"""

from functools import lru_cache

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
    # Dense vector size for Qdrant collections; must match the embedding
    # model chosen in §5 (Episodic Memory). Changing it means recreating
    # the collections.
    embedding_dim: int = 1536
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "companion-dev"
    redis_url: str = "redis://localhost:6379/0"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
