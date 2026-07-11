"""Answer a live-info question by RUNNING the project's engine + verified-retrieval
pipeline — not from any model's prior knowledge. Prints the companion's reply and the
grounded VerifiedResult (answer, sources, recency)."""

from __future__ import annotations

import asyncio
import sys
import uuid

QUESTION = " ".join(sys.argv[1:]) or "who is the prime minister of Nepal?"


async def main() -> int:
    from adapters.retrieval import build_crawl4ai_retrieval
    from adapters.search.serper import SerperSearch
    from api.composition import build_pipeline
    from config.settings import get_settings
    from tests.support.real_pipeline import RealTurns

    settings = get_settings()
    p = await build_pipeline(settings)
    try:
        print(f"QUESTION: {QUESTION!r}\n")

        # 1. The companion answering through the full engine (uses verified retrieval).
        turns = RealTurns(p, "u_demo_001")
        r = await turns.say(QUESTION, f"ask_{uuid.uuid4().hex[:6]}")
        print("=== COMPANION REPLY (full engine) ===")
        print(f"  {r.reply}")
        print(f"  searched: {r.searches}\n")

        # 2. The verified-retrieval pipeline directly — grounded answer + provenance.
        retrieval = build_crawl4ai_retrieval(
            search=SerperSearch(settings.serper_api_key),
            llm=p.llm,
            user_id="ask",
            ledger=p.ledger,
        )
        res = await retrieval.verify(QUESTION)
        print("=== VERIFIED RETRIEVAL (live sources) ===")
        print(f"  status        : {res.status}")
        print(f"  answer        : {res.answer!r}")
        print(f"  corroboration : {res.corroboration_count}")
        print(
            f"  recency       : time_sensitive={res.recency.is_time_sensitive} "
            f"stale={res.recency.is_stale} most_recent={res.recency.most_recent_source_date}"
        )
        for s in res.sources:
            print(f"  source        : {s.domain}  (published {s.published_date})")
        print(f"  spoken        : {res.formatted_voice}")
    finally:
        await p.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
