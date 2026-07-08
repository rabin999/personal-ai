"""Prompt providers (F13): fetch runtime prompts from Langfuse prompt management,
with a bundled fallback. Implements ``ports.prompt.PromptProvider``.

- ``LangfusePromptProvider`` fetches the deployed ('production' label) prompt via
  the Langfuse SDK, which caches client-side (TTL) so there's no per-turn latency,
  and falls back to the bundled default if Langfuse can't serve it — so the turn
  never hard-fails and a prompt edit/version-promotion in Langfuse is picked up on
  the next turn without a code change.
- ``BundledPromptProvider`` serves only the in-repo defaults (used when Langfuse is
  disabled). Both keep the prompt-management backend swappable behind the port.

Langfuse is imported ONLY in this adapter (A1.5 — core depends on the port).
"""

import logging
from collections.abc import Mapping
from typing import Any

from adapters.prompt.defaults import BUNDLED_PROMPTS
from ports.prompt import RenderedPrompt

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 60  # SDK serves cached prompts within this window (no added latency)


def _compile_bundled(name: str, variables: Mapping[str, str] | None) -> RenderedPrompt:
    text = BUNDLED_PROMPTS.get(name, "")
    for k, v in (variables or {}).items():
        text = text.replace("{{" + k + "}}", v)
    return RenderedPrompt(text=text, name=name, version="bundled", source="fallback")


class BundledPromptProvider:
    """Prompt provider with no backend — serves the in-repo defaults only."""

    def get(self, name: str, *, variables: Mapping[str, str] | None = None) -> RenderedPrompt:
        return _compile_bundled(name, variables)


class LangfusePromptProvider:
    def __init__(self, public_key: str, secret_key: str, host: str) -> None:
        from langfuse import Langfuse  # imported only in the adapter

        self._lf = Langfuse(public_key=public_key, secret_key=secret_key, host=host)

    def get(self, name: str, *, variables: Mapping[str, str] | None = None) -> RenderedPrompt:
        fallback = BUNDLED_PROMPTS.get(name, "")
        try:
            # cache_ttl_seconds keeps this off the network on the hot path; fallback
            # is used by the SDK itself if the very first fetch fails.
            prompt = self._lf.get_prompt(
                name,
                label="production",
                type="text",
                cache_ttl_seconds=_CACHE_TTL_S,
                fallback=fallback,
                max_retries=1,
            )
        except Exception:
            logger.debug("langfuse get_prompt(%s) failed; using bundled default", name)
            return _compile_bundled(name, variables)
        try:
            text = prompt.compile(**dict(variables or {}))
        except Exception:
            text = fallback
        # is_fallback is set by the SDK when it served our bundled default.
        used_fallback = bool(getattr(prompt, "is_fallback", False))
        version: Any = "bundled" if used_fallback else getattr(prompt, "version", 0)
        return RenderedPrompt(
            text=str(text),
            name=name,
            version=version,
            source="fallback" if used_fallback else "langfuse",
        )

    def seed_defaults(self) -> dict[str, str]:
        """Populate Langfuse's Prompts section (F13): create any bundled prompt that
        doesn't exist yet, labelled 'production'. Idempotent — an existing prompt is
        left untouched so human edits/versions in Langfuse are never overwritten.
        Returns {name: 'created'|'exists'|'error'}."""
        results: dict[str, str] = {}
        for name, text in BUNDLED_PROMPTS.items():
            try:
                existing = None
                try:
                    existing = self._lf.get_prompt(name, label="production", max_retries=0)
                    if getattr(existing, "is_fallback", False):
                        existing = None
                except Exception:
                    existing = None
                if existing is not None:
                    results[name] = "exists"
                    continue
                self._lf.create_prompt(
                    name=name,
                    prompt=text,
                    labels=["production"],
                    type="text",
                    commit_message="seed from bundled default (F13)",
                )
                results[name] = "created"
            except Exception:
                logger.warning("could not seed prompt %s into Langfuse", name, exc_info=True)
                results[name] = "error"
        return results
