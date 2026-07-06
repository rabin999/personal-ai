"""Thin e2e for §4+§5: a session's turns survive into the next session.

Full path: working memory buffers a session → close() hands the transcript
over → turn-based chunking → embedded write to Qdrant → a *new* session
retrieves it — "remembers me across sessions", the core memory promise.
"""

import uuid

import pytest

from adapters.db import Database
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import Settings
from core.memory.episodic import EpisodicMemory, chunk_transcript
from core.memory.working import Turn, WorkingMemory
from tests.integration.conftest import wait_until_healthy

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]


async def test_conversation_remembered_across_sessions() -> None:
    settings = Settings(_env_file=None)
    database = Database(settings)
    user_id = f"it_{uuid.uuid4().hex[:12]}"
    try:
        await wait_until_healthy(database)
        await database.ensure_qdrant_collections()
        episodic = EpisodicMemory(QdrantVectorStore(database, settings.embedding_model))
        wm = WorkingMemory()

        # Session 1: talk, then close.
        session_1 = f"s_{uuid.uuid4().hex[:8]}"
        wm.append(session_1, Turn(role="user", text="I adopted a golden retriever named Biscuit"))
        wm.append(session_1, Turn(role="assistant", text="Biscuit is a great name!"))
        transcript = wm.close(session_1)
        await episodic.write(user_id, session_1, chunk_transcript(transcript))

        # Session 2: fresh buffer, memory comes from episodic retrieval.
        session_2 = f"s_{uuid.uuid4().hex[:8]}"
        assert wm.recent(session_2) == []
        hits = await episodic.retrieve(user_id, "what's my dog's name?", k=3)
        assert hits and any("Biscuit" in h.text for h in hits)
    finally:
        await database.aclose()
