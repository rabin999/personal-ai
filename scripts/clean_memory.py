"""Clean accreted gibberish from a user's long-term memory (brief U1).

Enumerates each user's stored semantic facts + episodic events, judges quality with
a pinned model, deletes the junk, and dedups episodic. User-scoped; keeps on doubt.

Run on the box so it uses prod stores:
    cd /opt/companion && uv run python -m scripts.clean_memory [user_id ...]
With no args, cleans every user found in the profile store.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.composition import build_pipeline
from config.settings import get_settings
from core.memory.cleanup import MemoryCleaner


async def _all_user_ids(pipeline: object) -> list[str]:
    docs = await pipeline.docs.find("user_profile", {}, limit=1000)  # type: ignore[attr-defined]
    return [str(d.get("_id")) for d in docs if d.get("_id")]


async def main() -> None:
    pipeline = await build_pipeline(get_settings())
    try:
        cleaner = MemoryCleaner(pipeline.llm, pipeline.semantic, pipeline.episodic)
        user_ids = sys.argv[1:] or await _all_user_ids(pipeline)
        if not user_ids:
            print("no users found")
            return
        for user_id in user_ids:
            report = await cleaner.clean_user(user_id)
            print(f"\n=== {user_id} ===")
            print(
                f"semantic: {report.semantic_deleted}/{report.semantic_reviewed} dropped · "
                f"episodic: {report.episodic_deleted}/{report.episodic_reviewed} dropped · "
                f"deduped: {report.episodic_deduped}"
            )
            for line in report.dropped:
                print(f"  - {line}")
    finally:
        await pipeline.aclose()


if __name__ == "__main__":
    asyncio.run(main())
