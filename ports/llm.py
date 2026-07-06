"""Port: Chat completions via OpenRouter — complexity-tier routing, fallback (spec §11).

Interface stub — method signatures are defined when the module is built,
after reading its spec section (CLAUDE.md §1).
"""

from typing import Protocol


class LLM(Protocol):
    """Chat completions via OpenRouter — complexity-tier routing, fallback."""
