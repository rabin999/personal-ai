"""Port: Background task queue (Redis) — pull-at-pause delivery (spec §14).

Interface stub — method signatures are defined when the module is built,
after reading its spec section (CLAUDE.md §1).
"""

from typing import Protocol


class TaskQueue(Protocol):
    """Background task queue (Redis) — pull-at-pause delivery."""
