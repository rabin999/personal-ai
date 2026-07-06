"""LLM usage recording at the HTTP layer (cost-ledger invariant, spec §0.5).

Graphiti (§6) drives its own OpenAI-compatible calls, bypassing the LLM
Router (§11) — this httpx response hook captures token usage for every
chat-completions response on the shared client so those calls still land in
the Cost Ledger. The §11 router logs its own calls directly.
"""

import json
import logging

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class LLMUsage(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int


class LLMUsageRecorder:
    """Attach ``on_response`` as an httpx event hook; drain() after each operation."""

    def __init__(self) -> None:
        self._events: list[LLMUsage] = []

    async def on_response(self, response: httpx.Response) -> None:
        if "/chat/completions" not in str(response.request.url):
            return
        try:
            data = json.loads(await response.aread())
            usage = data.get("usage") or {}
            self._events.append(
                LLMUsage(
                    model=str(data.get("model", "")),
                    input_tokens=int(usage.get("prompt_tokens", 0)),
                    output_tokens=int(usage.get("completion_tokens", 0)),
                )
            )
        except Exception:  # a malformed body must never break the API call itself
            logger.debug("could not parse usage from LLM response", exc_info=True)

    def drain(self) -> list[LLMUsage]:
        events, self._events = self._events, []
        return events
