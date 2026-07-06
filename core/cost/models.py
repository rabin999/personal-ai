"""Data schema for the Cost Ledger (spec §3)."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

Component = Literal["llm", "stt", "tts", "tool", "search"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CostMetadata(BaseModel):
    session_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    cache_hit: bool = False


class CostEntry(BaseModel):
    """One append-only record of a money-costing computation.

    ``units`` shape varies by component: {input_tokens, output_tokens} for
    LLM, {characters} for TTS, {seconds} for STT/SER, {queries} for search.
    ``cost_usd`` of 0 is valid (cache hit — set ``metadata.cache_hit``).
    """

    user_id: str
    component: Component
    provider: str
    units: dict[str, int | float]
    cost_usd: float = Field(ge=0)
    timestamp: str = Field(default_factory=_now_iso)
    metadata: CostMetadata = Field(default_factory=CostMetadata)


class CostSummary(BaseModel):
    total_usd: float
    count: int
    breakdown: dict[str, float] | None = None
