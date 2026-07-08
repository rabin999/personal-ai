"""Per-user memory view (brief Part C — /memories).

Read-focused, auth'd, strictly user-scoped (§0.5). Groups a user's own memories by
supported type (no invented types): semantic facts (with validity windows),
episodic events (timestamped), procedural rules (with confidence). Working memory
is transient (in-process, per active session) and is not surfaced here.

Server-side pagination per type. The user may delete an episodic memory (the
"forget this" right, design §user-control); fact correction via the temporal graph
is a documented follow-up — semantic facts are view-only here.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from api.deps import CurrentUser

router = APIRouter(prefix="/api")


def _pipeline(request: Request) -> Any:
    pipeline = request.app.state.pipeline
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pipeline not wired"
        )
    return pipeline


@router.get("/memories/semantic")
async def semantic_memories(
    user: CurrentUser, request: Request, limit: int = Query(50, ge=1, le=200)
) -> dict[str, Any]:
    facts = await _pipeline(request).semantic.profile_facts(user.user_id, limit=limit)
    return {
        "type": "semantic",
        "items": [
            {
                "fact": f.fact,
                "valid_from": getattr(f, "valid_from", None),
                "valid_to": getattr(f, "valid_to", None),
            }
            for f in facts
        ],
    }


@router.get("/memories/episodic")
async def episodic_memories(
    user: CurrentUser,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=200),
) -> dict[str, Any]:
    hits = await _pipeline(request).episodic.list_recent(user.user_id, limit=offset + limit)
    page = hits[offset : offset + limit]
    return {
        "type": "episodic",
        "total": len(hits),
        "offset": offset,
        "items": [
            {"id": h.id, "text": h.text, "timestamp": h.timestamp, "session_id": h.session_id}
            for h in page
        ],
    }


@router.get("/memories/persona")
async def persona_memories(user: CurrentUser, request: Request) -> dict[str, Any]:
    """The dynamic persona — "How I've learned to talk with you" (brief U2). Readable
    style/interest/sensitivity learnings, each with confidence; ``active`` marks the
    ones confident enough to shape responses right now."""
    notes = await _pipeline(request).persona.readable(user.user_id)
    return {"type": "persona", "items": notes}


@router.get("/memories/procedural")
async def procedural_memories(user: CurrentUser, request: Request) -> dict[str, Any]:
    rules = await _pipeline(request).procedural.rules_for(user.user_id)
    return {
        "type": "procedural",
        "items": [
            {
                "id": r.id,
                "rule": r.rule_text,
                "trigger": r.trigger,
                "confidence": r.confidence,
                "evidence_count": r.evidence_count,
                "updated_at": r.updated_at,
            }
            for r in rules
        ],
    }


@router.get("/projects")
async def list_projects(user: CurrentUser, request: Request) -> dict[str, Any]:
    """The user's dynamic projects with current status (brief U3): each project's
    name, type, status/key metrics, and last activity. User-scoped."""
    summaries = await _pipeline(request).projects.summaries(user.user_id)
    return {"items": summaries}


@router.get("/knowledge-graph")
async def knowledge_graph(
    user: CurrentUser, request: Request, limit: int = Query(200, ge=1, le=500)
) -> dict[str, Any]:
    """The user's knowledge graph (brief U4): entities + relationships with temporal
    validity, from Graphiti/Neo4j. Read-only, user-scoped (isolation). Nodes are the
    connected entities; edges are the relationship facts (superseded ones marked)."""
    facts = await _pipeline(request).semantic.all_facts(user.user_id, limit=limit)
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for f in facts:
        for name in (f.source, f.target):
            if name and name not in nodes:
                nodes[name] = {"id": name, "label": name}
        edges.append(
            {
                "source": f.source,
                "target": f.target,
                "fact": f.fact,
                "relation": f.relation,
                "valid_from": f.valid_from,
                "valid_to": f.valid_to,  # set → superseded (historical)
                "current": f.valid_to is None,
            }
        )
    return {"nodes": list(nodes.values()), "edges": edges}


class _Correction(BaseModel):
    fact: str


@router.post("/memories/semantic")
async def correct_semantic_fact(
    body: _Correction, user: CurrentUser, request: Request
) -> dict[str, Any]:
    """Correct a wrong fact: record the corrected version; Graphiti's temporal
    reasoning supersedes the contradicted one (never deletes it, §6)."""
    fact = body.fact.strip()
    if not fact:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fact required")
    await _pipeline(request).semantic.record_fact(user.user_id, fact)
    return {"recorded": fact}


@router.delete("/memories/episodic/{memory_id}")
async def delete_episodic_memory(
    memory_id: str, user: CurrentUser, request: Request
) -> dict[str, Any]:
    """Forget one episodic memory (user-scoped delete)."""
    removed = await _pipeline(request).episodic.delete(user.user_id, memory_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory not found")
    return {"deleted": memory_id}
