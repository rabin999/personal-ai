"""Port: relevance reranker (A10).

After vector + BM25 fusion returns candidate memories, a cross-encoder reranker
re-scores each candidate against the query and picks the few that ACTUALLY belong
in the prompt — directly improving context quality (A3). `core/` depends only on
this interface; the concrete model (bge-reranker via fastembed) is an adapter,
swappable at startup.
"""

from typing import Protocol


class Reranker(Protocol):
    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[int]:
        """Return the indices of ``documents`` ranked most→least relevant to
        ``query``, truncated to ``top_n``. Must not raise (degrade to input order)."""
        ...
