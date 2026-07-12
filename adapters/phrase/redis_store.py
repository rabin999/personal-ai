"""Adapter: Redis-backed phrase-catalog store (implements ports.phrase_store.PhraseStore).

One global JSON document under ``{namespace}:phrases:catalog`` — the regenerated spoken pools.
Global (not user-scoped) on purpose: fillers carry no user data, so a shared set keeps cost flat
and sidesteps isolation entirely. A stored TTL lets the edge notice a stalled regenerator and
fall back to defaults rather than serving an ancient set forever.
"""

from __future__ import annotations

import json
import logging

import redis.asyncio as aioredis

from config.settings import Settings

logger = logging.getLogger(__name__)

# Stored copy outlives a few missed regen cycles but not a long worker outage — after this the
# edge stops refreshing (keeps whatever it last had / the defaults) rather than trusting a stale
# document indefinitely.
_CATALOG_TTL_S = 24 * 3600


class RedisPhraseStore:
    def __init__(self, settings: Settings, namespace: str | None = None) -> None:
        self._redis: aioredis.Redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        ns = namespace or settings.queue_namespace
        self._key = f"{ns}:phrases:catalog"

    async def load(self) -> dict[str, list[str]] | None:
        try:
            raw = await self._redis.get(self._key)
        except Exception:  # store down → caller keeps its current pools / defaults
            logger.warning("phrase store load failed; keeping current pools", exc_info=True)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("phrase store held malformed JSON; ignoring")
            return None
        if not isinstance(data, dict):
            return None
        # Coerce to the declared shape; drop anything that isn't a list of strings.
        pools: dict[str, list[str]] = {}
        for name, lines in data.items():
            if isinstance(lines, list):
                pools[str(name)] = [str(x) for x in lines if isinstance(x, str)]
        return pools or None

    async def save(self, pools: dict[str, list[str]]) -> None:
        payload = json.dumps(pools, ensure_ascii=False)
        try:
            await self._redis.set(self._key, payload, ex=_CATALOG_TTL_S)
        except Exception:  # a failed save just means the edge keeps its current pools
            logger.warning("phrase store save failed; catalog not updated", exc_info=True)

    async def aclose(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:
            logger.debug("phrase store redis close failed", exc_info=True)
