"""Engine-suite fixtures.

Re-exports the real-call `real_turns` fixture (a live pipeline built once per module against
the real model and real stores) so the E5 caller-independence tests can drive BOTH engine
entrypoints over the same assembled prompt.

Follows the same re-export pattern `tests/golden/conftest.py` uses for the `db` fixture.
Without this the E5 tests ERROR with "fixture 'real_turns' not found" — which is the correct
failure mode, and how this omission was caught. A missing prerequisite must never look like a
passing test (F3/F4).
"""

from tests.real_call.conftest import real_turns

__all__ = ["real_turns"]
