"""Composition root: build the full pipeline object graph (design doc §17.2).

This is the one place adapters are constructed and wired to the core modules
through their ports — ``core/`` itself never imports ``adapters/``. Both the
serving edge (``api/app.py``) and the background worker
(``workers/consolidation_worker.py``) build their object graph here so wiring
stays in a single, reviewable place.

Behavior params (LLM tier chains, model pricing) are loaded from the seeded
``provider_config`` documents, not hard-coded (spec §2 rule: config over code).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapters.db import Database
from adapters.graph.graphiti import GraphitiGraphStore
from adapters.llm.openrouter import OpenRouterLLM
from adapters.logging.factory import build_log_sinks
from adapters.logging.trace_sink import TraceStoreLogSink
from adapters.preference.mem0_adapter import Mem0PreferenceMemory
from adapters.queue.redis import RedisTaskQueue
from adapters.search.brave import BraveSearch
from adapters.search.serper import SerperSearch
from adapters.ser.emotion2vec_client import Emotion2VecSER
from adapters.stt.faster_whisper import FasterWhisperSTT
from adapters.tts.grok import GrokTTS
from adapters.user_context.static import StaticUserContext
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import Settings
from core.cost import CostLedger
from core.feedback import FeedbackStore
from core.memory.conversation_store import ConversationStore
from core.memory.entities import EntityResolver
from core.memory.episodic import EpisodicMemory
from core.memory.extraction import MemoryExtractor
from core.memory.procedural import ProceduralMemory
from core.memory.semantic import SemanticMemory
from core.memory.vocab import VocabProvider
from core.memory.working import WorkingMemory
from core.observability import TraceStore
from core.observability.logger import StructuredLogger
from core.profile import ProfileService, TraitRegistry
from core.projects.service import ProjectService
from core.psych.consolidation import Consolidator
from core.psych.user_model import PsychUserModel
from core.reasoning.prompt_assembly import PromptAssembler
from core.reasoning.response_gen import ResponseGenerator
from core.reasoning.self_model import SelfModel
from core.tools.builtin.core_tools import register_core_tools
from core.tools.delivery import DeliveryComposer
from core.tools.dispatcher import ToolDispatcher
from core.tools.registry import ToolRegistry
from core.tools.results import ToolResultStore
from core.tools.web_search import WebSearch

logger = logging.getLogger(__name__)

DEFAULTS_DIR = Path(__file__).parents[1] / "config" / "defaults"

# Background task types (spec §14 registry) → handlers registered in the worker.
TASK_WEB_SEARCH = "web_search"
TASK_CONSOLIDATION = "consolidation"


@dataclass
class Pipeline:
    """The wired object graph shared by the serving edge and the worker."""

    settings: Settings
    db: Database
    ledger: CostLedger
    profiles: ProfileService
    registry: TraitRegistry
    user_context: StaticUserContext
    working: WorkingMemory
    episodic: EpisodicMemory
    semantic: SemanticMemory
    procedural: ProceduralMemory
    llm: OpenRouterLLM
    assembler: PromptAssembler
    generator: ResponseGenerator
    self_model: SelfModel
    psych: PsychUserModel
    stt: FasterWhisperSTT
    tts: GrokTTS
    ser: Emotion2VecSER
    queue: RedisTaskQueue
    tool_registry: ToolRegistry
    dispatcher: ToolDispatcher
    projects: ProjectService
    web_search: WebSearch
    delivery: DeliveryComposer
    consolidator: Consolidator
    vocab: VocabProvider
    traces: TraceStore
    conversations: ConversationStore
    extractor: MemoryExtractor
    preferences: Mem0PreferenceMemory | None
    logs: StructuredLogger
    feedback: FeedbackStore

    async def aclose(self) -> None:
        await self.ledger.flush()
        await self.queue.aclose()
        await self.db.aclose()
        self.logs.close()


async def build_pipeline(settings: Settings) -> Pipeline:
    """Construct and wire everything; fail loud if a datastore is unreachable (§1)."""
    db = Database(settings)
    await db.startup()

    from adapters.doc.mongo import MongoDocStore  # local: keep import graph flat

    docs = MongoDocStore(db)
    ledger = CostLedger(docs)

    # Observability wired early so per-LLM-call spans reach the trace (§1/§5):
    # structured logs fan to the configured sinks + a trace-store sink that maps
    # correlation-bound records into the durable per-turn trace.
    traces = TraceStore(docs)
    logs = StructuredLogger([*build_log_sinks(settings), TraceStoreLogSink(traces)])

    profiles = ProfileService(docs)
    registry = TraitRegistry(docs, profiles)
    await registry.seed_defaults(DEFAULTS_DIR)  # traits + project types + provider config

    tiers = await _load_tiers(docs)
    pricing = await _load_pricing(docs)
    llm = OpenRouterLLM(settings, ledger=ledger, tiers=tiers, logs=logs)
    await llm.verify_models()  # list/verify configured models against the live catalog

    vectors = QdrantVectorStore(db, settings.embedding_model)
    graph = GraphitiGraphStore(db, ledger=ledger, pricing=pricing)

    working = WorkingMemory()
    episodic = EpisodicMemory(vectors)
    semantic = SemanticMemory(graph)
    procedural = ProceduralMemory(docs)
    entities = EntityResolver(vectors)
    self_model = SelfModel(docs, vectors, llm)
    psych = PsychUserModel(docs)

    queue = RedisTaskQueue(settings)
    tool_registry = ToolRegistry()
    projects = ProjectService(docs, entities, tool_registry, llm=llm)
    await projects.sync_tool_registrations()  # re-register actions for live instances

    tool_results = ToolResultStore(docs)
    dispatcher = ToolDispatcher(
        tool_registry, queue, ledger=ledger, results=tool_results, logs=logs
    )
    delivery = DeliveryComposer(queue, llm)
    web_search = WebSearch(docs, llm, *_search_providers(settings), ledger=ledger)
    register_core_tools(  # the MVP core tool set (§8.5) — so the loop can act
        tool_registry,
        episodic=episodic,
        semantic=semantic,
        web_search=web_search,
        profiles=profiles,
        projects=projects,
        results=tool_results,
    )

    # §2 Mem0 preference memory (fast personalization layer). Guarded init:
    # a failure degrades to None and the app still runs without it.
    preferences = Mem0PreferenceMemory(settings) if settings.preference_memory_enabled else None

    assembler = PromptAssembler(
        profiles,
        registry,
        working,
        episodic,
        semantic,
        procedural,
        entities,
        self_model,
        projects=projects,
        psych=psych,
        preferences=preferences,
    )
    generator = ResponseGenerator(llm, self_model, registry, logs=logs)
    consolidator = Consolidator(semantic, procedural, psych, docs, llm)

    return Pipeline(
        settings=settings,
        db=db,
        ledger=ledger,
        profiles=profiles,
        registry=registry,
        user_context=StaticUserContext.from_defaults(DEFAULTS_DIR, profiles),
        working=working,
        episodic=episodic,
        semantic=semantic,
        procedural=procedural,
        llm=llm,
        assembler=assembler,
        generator=generator,
        self_model=self_model,
        psych=psych,
        stt=FasterWhisperSTT(model_size=settings.stt_model_size, ledger=ledger),
        tts=GrokTTS(settings, ledger=ledger),
        ser=Emotion2VecSER(settings),
        queue=queue,
        tool_registry=tool_registry,
        dispatcher=dispatcher,
        projects=projects,
        web_search=web_search,
        delivery=delivery,
        consolidator=consolidator,
        vocab=VocabProvider(semantic, profiles),
        traces=traces,
        conversations=ConversationStore(docs),
        extractor=MemoryExtractor(llm, episodic, semantic, projects, preferences=preferences),
        preferences=preferences,
        logs=logs,
        feedback=FeedbackStore(docs),
    )


async def _load_tiers(docs: Any) -> dict[str, list[str]] | None:
    doc = await docs.get("provider_config", "llm_router")
    return doc.get("tiers") if doc else None


async def _load_pricing(docs: Any) -> dict[str, dict[str, float]] | None:
    doc = await docs.get("provider_config", "llm_pricing")
    return doc.get("models") if doc else None


def _search_providers(settings: Settings) -> tuple[SerperSearch, BraveSearch | None]:
    primary = SerperSearch(settings.serper_api_key)
    fallback = BraveSearch(settings.brave_api_key) if settings.brave_api_key else None
    return primary, fallback
