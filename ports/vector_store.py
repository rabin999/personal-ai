"""Port: Vector store (Qdrant) — hybrid dense+BM25+RRF, user-filtered (spec §1, §5, §8).

Interface stub — method signatures are defined when the module is built,
after reading its spec section.
"""

from typing import Protocol


class VectorStore(Protocol):
    """Vector store (Qdrant) — hybrid dense+BM25+RRF, user-filtered."""
