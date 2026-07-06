"""Port: Web search (Serper primary, Brave fallback) + query cache (spec §15).

Interface stub — method signatures are defined when the module is built,
after reading its spec section (CLAUDE.md §1).
"""

from typing import Protocol


class SearchProvider(Protocol):
    """Web search (Serper primary, Brave fallback) + query cache."""
