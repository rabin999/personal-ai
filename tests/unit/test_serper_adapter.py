"""Unit tests for the Serper adapter's response parsing + freshness retry (§15).

No network: httpx.AsyncClient.post is monkeypatched to return canned Serper JSON,
which also lets us assert exactly what freshness filter (tbs) reached the wire.
"""

from typing import Any

import httpx
import pytest

from adapters.search.serper import SerperSearch


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch(monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]) -> list[dict]:
    """Queue Serper JSON responses; return the list that records each request payload."""
    sent: list[dict] = []
    queue = list(responses)

    async def fake_post(self: Any, url: str, *, headers: Any = None, json: Any = None) -> _Resp:
        sent.append(json)
        return _Resp(queue.pop(0))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return sent


async def test_answer_box_leads_and_organic_follows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        [
            {
                "answerBox": {"answer": "125 million", "title": "Population", "link": "http://x"},
                "organic": [{"title": "Japan", "link": "http://y", "snippet": "about 125M"}],
            }
        ],
    )
    results = await SerperSearch("k").search("population of Japan")
    assert results[0].snippet == "125 million"  # the direct answer leads
    assert results[0].url == "http://x"
    assert len(results) == 2 and results[1].title == "Japan"


async def test_empty_filtered_result_retries_without_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # qdr:m returns nothing for an evergreen query; the adapter must retry unfiltered.
    sent = _patch(
        monkeypatch,
        [
            {"organic": []},  # first (filtered) fetch → empty
            {"organic": [{"title": "Japan", "link": "http://y", "snippet": "125M"}]},  # retry
        ],
    )
    results = await SerperSearch("k").search("population of Japan", recency="month")
    assert len(results) == 1 and results[0].title == "Japan"  # recovered via retry
    assert sent[0].get("tbs") == "qdr:m"  # first call carried the freshness filter
    assert "tbs" not in sent[1]  # retry dropped it


async def test_no_retry_when_filtered_result_is_non_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _patch(
        monkeypatch,
        [{"organic": [{"title": "Now", "link": "http://n", "snippet": "fresh"}]}],
    )
    results = await SerperSearch("k").search("breaking news", recency="day")
    assert len(results) == 1 and len(sent) == 1  # one call, no wasteful retry
    assert sent[0].get("tbs") == "qdr:d"
