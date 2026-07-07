"""bge-reranker cross-encoder via fastembed (implements ports.reranker.Reranker, A10).

Picks WHICH fused candidate memories enter the prompt. The model is loaded lazily
(first use) and cached; scoring is CPU-viable for the small candidate sets we
rerank. Never raises — a failure degrades to the input order so retrieval still works.
"""

import logging
from functools import cached_property
from typing import Any

logger = logging.getLogger(__name__)


class FastEmbedReranker:
    def __init__(self, model: str = "BAAI/bge-reranker-base") -> None:
        self._model_name = model

    @cached_property
    def _encoder(self) -> Any:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        return TextCrossEncoder(model_name=self._model_name)

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[int]:
        if not documents:
            return []
        try:
            scores = list(self._encoder.rerank(query, documents))
            order = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)
            return order[:top_n]
        except Exception:
            logger.warning("rerank failed; keeping fusion order", exc_info=True)
            return list(range(min(top_n, len(documents))))
