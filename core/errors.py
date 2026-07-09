"""Bug vs. dependency-failure classification (F3).

A broad ``except Exception`` on the turn path turned a wiring defect into months of
silent wrongness: `VoiceSession` called the wired engine with a keyword it didn't
accept, the resulting ``TypeError`` was absorbed as "voice turn failed", and the
companion answered every single turn with silence (docs/CODE_FLOW.md §0).

The two failure classes need opposite handling:

- **Bugs in our own code** — a contract between two of OUR modules is wrong. Retrying
  cannot help and degrading hides it. These must fail LOUDLY: full traceback to the
  structured logger, a failed step in the trace, and re-raised.
- **Dependency failures** — an LLM/STT/TTS/search/store call failed, timed out, or
  returned garbage. The world is allowed to be flaky. These degrade gracefully: the
  companion says honestly that the step failed and the conversation continues.

`LLMUnavailable`, `SearchProviderError` and friends are already dependency failures by
construction and are caught by name where they matter; this module is about the
*unnamed* residual `except Exception` that hides everything else.
"""

from __future__ import annotations

# Raised when OUR code is wrong, never because a dependency misbehaved:
#   TypeError        — wrong signature/arity (the F1 defect), wrong type passed
#   AttributeError   — missing method/field on an internal object
#   NameError        — includes UnboundLocalError
#   ImportError      — a module/adapter that should exist doesn't
#   AssertionError   — an internal invariant we asserted was violated
#   IndexError       — off-by-one on our own sequences
#   KeyError         — a missing key on an INTERNAL contract (prompt sections, spans,
#                      tool specs). Note the tradeoff: a malformed document read back
#                      from a store can also raise KeyError, and it will now fail loudly
#                      instead of degrading. That is deliberate — a store returning a
#                      shape we don't expect is a contract violation we want to see.
PROGRAMMING_ERRORS: tuple[type[BaseException], ...] = (
    TypeError,
    AttributeError,
    NameError,
    ImportError,
    AssertionError,
    IndexError,
    KeyError,
)


def is_bug(exc: BaseException) -> bool:
    """True when ``exc`` indicates a defect in our own code rather than a flaky
    dependency. Callers must re-raise (never absorb) when this returns True."""
    return isinstance(exc, PROGRAMMING_ERRORS)
