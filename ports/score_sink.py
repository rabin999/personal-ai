"""Port: evaluation scores (F13 human-in-the-loop). The app's user feedback
(thumbs up/down) and any judged scores are submitted through this interface and
attached to the corresponding trace in the eval backend (Langfuse), so a rating is
inspectable next to the pipeline that produced it and can calibrate the LLM-judge.

Swappable + best-effort: a scoring outage never breaks the request.
"""

from typing import Protocol


class ScoreSink(Protocol):
    def score(
        self, *, session_id: str, turn: int, name: str, value: float, comment: str = ""
    ) -> None:
        """Attach a numeric score to the (session, turn) trace. Never raises."""
        ...
