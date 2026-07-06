"""Graphiti embedder backed by fastembed (spec §6).

OpenRouter exposes no embeddings endpoint, so Graphiti's fact/node embeddings
use the same local fastembed model as Episodic Memory (§5) — free per call
and dimensionally consistent across memory layers.
"""

import asyncio
from collections.abc import Iterable
from functools import cached_property

from fastembed import TextEmbedding
from graphiti_core.embedder.client import EmbedderClient


class FastembedEmbedder(EmbedderClient):
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name

    @cached_property
    def _model(self) -> TextEmbedding:
        return TextEmbedding(self._model_name)

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        if isinstance(input_data, str):
            texts = [input_data]
        elif isinstance(input_data, list) and all(isinstance(item, str) for item in input_data):
            texts = list(input_data)
        else:
            raise TypeError("FastembedEmbedder supports text input only")
        vectors: list[list[float]] = await asyncio.to_thread(
            lambda: [v.tolist() for v in self._model.embed(texts)]
        )
        return vectors[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        batch: list[list[float]] = await asyncio.to_thread(
            lambda: [v.tolist() for v in self._model.embed(input_data_list)]
        )
        return batch
