"""Port: prompt management (F13). Prompts live OUTSIDE the code — created,
versioned, and deployed-by-label in a prompt-management backend (Langfuse) — and
the app FETCHES them at runtime through this interface. Keeping it behind a port
means the backend is swappable (A1.5) and, crucially, that a prompt edit/version
promotion in the backend changes app behavior on the NEXT turn WITHOUT a code
change or redeploy.

Never hard-fails a turn: if the backend is unreachable, the adapter serves a safe
bundled default (recorded as ``source="fallback"``) so the conversation continues.
"""

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel


class RenderedPrompt(BaseModel):
    """A prompt fetched + compiled for use this turn, with provenance for the trace."""

    text: str
    name: str
    version: int | str = 0
    # "langfuse" when served from the prompt-management backend, "fallback" when the
    # bundled default was used (backend unreachable / prompt not yet created).
    source: str = "fallback"


class PromptProvider(Protocol):
    def get(self, name: str, *, variables: Mapping[str, str] | None = None) -> RenderedPrompt:
        """Fetch prompt ``name`` (the deployed 'production' label), compiled with
        ``variables``. Returns the bundled default (source='fallback') if the backend
        can't serve it — never raises."""
        ...
