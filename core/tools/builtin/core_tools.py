"""Core (non-project) tools — the MVP tool set (design doc §8.5).

Registered against the shared registry at startup so the agentic loop (§14.11)
can actually DO things: recall the user's memory, look up live info on the web
(background), read semantic facts, and adjust audio sensitivity. Project tools
(e.g. finance ``log_entry``) are registered by §16 per instance.

Handlers are ``(args, ToolContext) -> dict`` and take ``user_id`` from the
resolved context (never hard-coded). ``web_search`` is a *background* tool: the
dispatcher enqueues it (§8.6 — never run search inline) and the worker executes
this handler, so its result comes back at a pause (§14).
"""

import contextlib
from typing import Any

from core.memory.episodic import EpisodicMemory
from core.memory.semantic import SemanticMemory
from core.profile import ProfileService
from core.projects.service import ProjectService
from core.tools.registry import ToolContext, ToolRegistry, ToolSpec
from core.tools.web_search import WebSearch

# VAD nudge per "you're too sensitive" style request (§11.3); clamped in §2.
_VAD_STEP = 0.12

FINANCE_TYPE = "finance_portfolio"


def register_core_tools(
    registry: ToolRegistry,
    *,
    episodic: EpisodicMemory,
    semantic: SemanticMemory,
    web_search: WebSearch,
    profiles: ProfileService,
    projects: ProjectService,
) -> None:
    async def search_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        hits = await episodic.retrieve(ctx.user_id, str(args.get("query", "")), k=5)
        return {"snippets": [h.text for h in hits]}

    registry.register(
        ToolSpec(
            id="search_memory",
            description="Search this user's past conversations (episodic memory) for "
            'relevant snippets. args: {"query": str}',
            type="readonly",
            latency_class="fast",
        ),
        search_memory,
    )

    async def get_semantic_facts(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        facts = await semantic.profile_facts(ctx.user_id, limit=10)
        return {"facts": [f.fact for f in facts]}

    registry.register(
        ToolSpec(
            id="get_semantic_facts",
            description="Get stable known facts about this user (name, people, "
            "preferences). args: {}",
            type="readonly",
            latency_class="fast",
        ),
        get_semantic_facts,
    )

    async def web_search_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        outcome = await web_search.run(str(args.get("query", "")), ctx.user_id, ctx.session_id)
        return {
            "summary": outcome.summary,
            "sources": [s.url for s in outcome.sources][:5],
            "found": bool(outcome.sources),
        }

    registry.register(
        ToolSpec(
            id="web_search",
            description="Search the LIVE web for current / real-time info — news, "
            "sports scores, prices, events, 'what's happening'. args: {\"query\": str}",
            type="background",
            latency_class="slow",
        ),
        web_search_tool,
    )

    async def set_companion_name(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()[:40]
        if not name:
            return {"error": "no name given"}
        await profiles.update(ctx.user_id, {"companion_name": name, "onboarded": True})
        # Durable semantic fact so the name survives across sessions (§3.1).
        # Best-effort: the profile name is canonical if the graph write fails.
        with contextlib.suppress(Exception):
            await semantic.add_episode(ctx.user_id, f"The user named the companion '{name}'.")
        return {"companion_name": name}

    registry.register(
        ToolSpec(
            id="set_companion_name",
            description="Remember the name the user wants to call you (the companion). "
            'Call this the first time they give you a name. args: {"name": str}',
            type="action",
            latency_class="fast",
            requires_confirmation=False,
        ),
        set_companion_name,
    )

    async def record_trade(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        # §16: persist a stock trade to the user's finance portfolio, creating the
        # project instance on first use so "record my trade" works from a cold
        # account (root cause of trades not persisting — no create path existed).
        ticker = str(args.get("ticker", "")).upper().strip()
        side = str(args.get("side", "")).lower().strip()
        if not ticker or side not in ("buy", "sell"):
            return {"error": "need a ticker and side (buy or sell)"}
        try:
            qty = float(args.get("qty", 0))
            price = float(args.get("price", 0))
        except (TypeError, ValueError):
            return {"error": "qty and price must be numbers"}
        project = await projects.find_or_create(ctx.user_id, FINANCE_TYPE, "My portfolio")
        await projects.log_entry(
            project.id,
            ctx.user_id,
            {"ticker": ticker, "side": side, "qty": qty, "price": price},
        )
        return {
            "recorded": True,
            "project_id": project.id,
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "price": price,
        }

    registry.register(
        ToolSpec(
            id="record_trade",
            description="Record a stock trade the user tells you about (buy/sell). Creates "
            "their portfolio the first time. Use when they say things like 'I bought 10 "
            'AAPL at 150\' or \'record my trade\'. args: {"ticker": str, "side": "buy"|"sell", '
            '"qty": number, "price": number}',
            type="action",
            latency_class="fast",
            requires_confirmation=False,
        ),
        record_trade,
    )

    async def update_audio_prefs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        profile = await profiles.get(ctx.user_id)
        threshold = profile.audio_prefs.vad_threshold
        direction = str(args.get("direction", "")).lower()
        if "less" in direction or "up" in direction or args.get("more_sensitive") is False:
            threshold += _VAD_STEP  # less twitchy about background noise
        elif "more" in direction or "down" in direction:
            threshold -= _VAD_STEP
        updated = await profiles.update(ctx.user_id, {"audio_prefs": {"vad_threshold": threshold}})
        return {"vad_threshold": updated.audio_prefs.vad_threshold}

    registry.register(
        ToolSpec(
            id="update_audio_prefs",
            description="Adjust mic sensitivity when the user says you're picking up too "
            "much/little (clamped to a safe range, persisted). "
            'args: {"direction": "less_sensitive"|"more_sensitive"}',
            type="action",
            latency_class="fast",
            requires_confirmation=False,
        ),
        update_audio_prefs,
    )
