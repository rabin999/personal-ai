"""Mem0 preference-memory adapter (implements ports.preference_memory, brief §2).

Mem0 is the fast personalization/preference layer: its ``add()`` runs its own
extraction and stores distilled preferences ("User loves hiking", "User's dog is
named Trishul") with hybrid retrieval. We wire it to OUR stack — no OpenAI key
needed:

- LLM: the OpenRouter gateway (OpenAI-compatible), same fast model as the app.
- Embedder: local ``fastembed`` (the same bge-small model episodic uses).
- Vector store: our Qdrant, in a dedicated ``mem0_preferences`` collection.

Init and every call are guarded: if Mem0 is unavailable or errors, this degrades
to a no-op so a turn never breaks. Mem0's blocking client is run in a thread so it
never stalls the event loop. This complements Graphiti (temporal facts) and Qdrant
episodic — see docs/REMEDIATION_LOG.md for the two-engine rationale.
"""

import asyncio
import logging
import os

# Disable Mem0/PostHog telemetry BEFORE importing mem0 anywhere: PostHog spawns a
# background thread that phones home and can delay process exit (a lingering
# non-daemon flush on Ctrl+C). Set at import time so it's in place first.
os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("POSTHOG_DISABLED", "1")

from typing import Any
from urllib.parse import urlparse

from config.settings import Settings

logger = logging.getLogger(__name__)

MEM0_COLLECTION = "mem0_preferences"


class Mem0PreferenceMemory:
    def __init__(self, settings: Settings) -> None:
        self._memory: Any | None = None
        try:
            from mem0 import Memory  # type: ignore[import-untyped]

            parsed = urlparse(settings.qdrant_url)
            config = {
                "llm": {
                    "provider": "openai",
                    "config": {
                        "model": settings.preference_model,
                        "api_key": settings.open_router_api_key,
                        "openai_base_url": settings.open_router_base_url,
                    },
                },
                "embedder": {
                    "provider": "fastembed",
                    "config": {"model": settings.embedding_model},
                },
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "host": parsed.hostname or "localhost",
                        "port": parsed.port or 6333,
                        "collection_name": MEM0_COLLECTION,
                        "embedding_model_dims": settings.embedding_dim,
                    },
                },
            }
            self._memory = Memory.from_config(config)
            logger.info("Mem0 preference memory initialized")
        except Exception:  # never let a memory layer stop the app booting
            logger.exception("Mem0 init failed; preference memory disabled")
            self._memory = None

    async def add(self, user_id: str, messages: list[dict[str, str]]) -> None:
        if self._memory is None:
            return
        try:
            await asyncio.to_thread(self._memory.add, messages, user_id=user_id)
        except Exception:
            logger.exception("Mem0 add failed (best-effort)")

    async def search(self, user_id: str, query: str, limit: int = 5) -> list[str]:
        if self._memory is None or not query.strip():
            return []
        try:
            result = await asyncio.to_thread(
                self._memory.search, query, filters={"user_id": user_id}, limit=limit
            )
        except Exception:
            logger.exception("Mem0 search failed (best-effort)")
            return []
        hits = result.get("results", []) if isinstance(result, dict) else (result or [])
        return [h["memory"] for h in hits if isinstance(h, dict) and h.get("memory")]

    async def delete_all(self, user_id: str) -> None:
        """Wipe ALL of this user's Mem0 personalization memory (account deletion)."""
        if self._memory is None:
            return
        try:
            await asyncio.to_thread(self._memory.delete_all, user_id=user_id)
        except Exception:
            logger.exception("Mem0 delete_all failed (best-effort)")
