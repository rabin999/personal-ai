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
from adapters.outbox import OutboxStore, WelcomeMailer
from adapters.preference.mem0_adapter import Mem0PreferenceMemory
from adapters.prompt.langfuse_prompt import BundledPromptProvider
from adapters.queue.redis import RedisTaskQueue
from adapters.search.brave import BraveSearch
from adapters.search.serper import SerperSearch
from adapters.ser.emotion2vec_client import Emotion2VecSER
from adapters.stt.faster_whisper import FasterWhisperSTT
from adapters.tts.grok import GrokTTS
from adapters.user_context.accounts import AccountStore
from adapters.user_context.session import SessionUserContext
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import Settings
from core.cost import CostLedger
from core.feedback import FeedbackStore
from core.memory.compaction import SessionCompactor
from core.memory.conversation_store import ConversationStore
from core.memory.entities import EntityResolver
from core.memory.episodic import EpisodicMemory
from core.memory.extraction import MemoryExtractor
from core.memory.procedural import ProceduralMemory
from core.memory.routing import MemoryRouter
from core.memory.semantic import SemanticMemory
from core.memory.vocab import VocabProvider
from core.memory.working import WorkingMemory
from core.observability import TraceStore
from core.observability.logger import StructuredLogger
from core.profile import ProfileService, TraitRegistry
from core.projects.service import ProjectService
from core.psych.consolidation import Consolidator
from core.psych.user_model import PsychUserModel
from core.reasoning.orchestrator import Orchestrator
from core.reasoning.prompt_assembly import PromptAssembler
from core.reasoning.recall import ConversationRecall
from core.reasoning.response_gen import ResponseGenerator
from core.reasoning.self_model import SelfModel
from core.tools.builtin.core_tools import register_core_tools
from core.tools.delivery import DeliveryComposer
from core.tools.dispatcher import ToolDispatcher
from core.tools.registry import ToolRegistry
from core.tools.results import ToolResultStore
from core.tools.web_search import WebSearch
from ports.prompt import PromptProvider
from ports.score_sink import ScoreSink

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
    user_context: SessionUserContext
    accounts: AccountStore
    outbox: OutboxStore
    mailer: WelcomeMailer
    working: WorkingMemory
    episodic: EpisodicMemory
    semantic: SemanticMemory
    procedural: ProceduralMemory
    llm: OpenRouterLLM
    assembler: PromptAssembler
    generator: ResponseGenerator
    orchestrator: Orchestrator
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
    memory_router: MemoryRouter
    preferences: Mem0PreferenceMemory | None
    logs: StructuredLogger
    feedback: FeedbackStore
    prompts: PromptProvider  # F13: runtime prompt management (Langfuse or bundled)
    scores: ScoreSink | None  # F13: eval/feedback scoring backend (Langfuse), if enabled
    compactor: SessionCompactor  # F14: rolling-summary compaction for long sessions

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
    trace_sinks: list[Any] = [*build_log_sinks(settings), TraceStoreLogSink(traces)]
    # A8: also route the per-turn trace into self-hosted Langfuse when enabled
    # (behind the LogSink port — swappable). Guarded so a bad key never blocks boot.
    if settings.langfuse_enabled and settings.langfuse_public_key:
        try:
            from adapters.tracing.langfuse_sink import LangfuseTraceSink

            trace_sinks.append(
                LangfuseTraceSink(
                    settings.langfuse_public_key,
                    settings.langfuse_secret_key,
                    settings.langfuse_host,
                )
            )
            logger.info("Langfuse tracing enabled → %s", settings.langfuse_host)
        except Exception:
            logger.exception("Langfuse sink init failed; continuing without it")
    logs = StructuredLogger(trace_sinks)

    # F13: prompt management + eval scoring behind their ports. When Langfuse is
    # enabled, prompts are fetched (+ seeded) from Langfuse and user feedback is
    # scored onto the trace; otherwise a bundled-default prompt provider is used so
    # the app never hard-depends on Langfuse. Both are swappable (A1.5).
    prompts: PromptProvider = BundledPromptProvider()
    scores: ScoreSink | None = None
    if settings.langfuse_enabled and settings.langfuse_public_key:
        try:
            from adapters.prompt.langfuse_prompt import LangfusePromptProvider
            from adapters.tracing.langfuse_sink import LangfuseScoreSink

            lf_prompts = LangfusePromptProvider(
                settings.langfuse_public_key, settings.langfuse_secret_key, settings.langfuse_host
            )
            seeded = lf_prompts.seed_defaults()  # populate Langfuse's Prompts section
            logger.info("Langfuse prompts: %s", seeded)
            prompts = lf_prompts
            scores = LangfuseScoreSink(
                settings.langfuse_public_key, settings.langfuse_secret_key, settings.langfuse_host
            )
        except Exception:
            logger.exception("Langfuse prompt/score init failed; using bundled prompts")

    profiles = ProfileService(docs)
    registry = TraitRegistry(docs, profiles)
    await registry.seed_defaults(DEFAULTS_DIR)  # traits + project types + provider config

    # Real auth (design §18): account store + transactional outbox + welcome
    # mailer. On Google sign-up the account store creates the user, seeds the §2
    # profile, and queues the welcome email via the outbox (brief §4).
    outbox = OutboxStore(docs)
    mailer = WelcomeMailer(settings)
    accounts = AccountStore(docs, profiles, outbox=outbox)

    tiers = await _load_tiers(docs)
    pricing = await _load_pricing(docs)
    llm = OpenRouterLLM(settings, ledger=ledger, tiers=tiers, logs=logs)
    await llm.verify_models()  # list/verify configured models against the live catalog

    vectors = QdrantVectorStore(db, settings.embedding_model)
    graph = GraphitiGraphStore(db, ledger=ledger, pricing=pricing)

    working = WorkingMemory()
    reranker = None
    if settings.reranker_enabled:
        from adapters.rerank.fastembed_reranker import FastEmbedReranker

        reranker = FastEmbedReranker(settings.reranker_model)
        logger.info("reranker enabled: %s", settings.reranker_model)
    episodic = EpisodicMemory(vectors, reranker=reranker)
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
    delivery = DeliveryComposer(queue, llm, max_interjections=settings.delivery_max_interjections)
    conversations = ConversationStore(docs)
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
    extractor = MemoryExtractor(llm, episodic, semantic, projects, preferences=preferences)

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
        recall=ConversationRecall(conversations),  # F3/F4 conversation-recall routing
    )
    generator = ResponseGenerator(
        llm,
        self_model,
        registry,
        logs=logs,
        max_turn_cost_usd=settings.max_turn_cost_usd,
        reasoning_tier=settings.reasoning_tier,  # A2: mature model for the main turn
        prompts=prompts,  # F13: managed self-reflection prompt (Langfuse or bundled)
    )
    # A1/A1.5: the reasoning engine sits behind the Orchestrator port. LangGraph is
    # one adapter (imported only in adapters/), the native loop is the other —
    # swapping is this one wiring line; core/ is untouched.
    orchestrator: Orchestrator
    if settings.orchestrator == "langgraph":
        from adapters.orchestrator.langgraph_orchestrator import LangGraphOrchestrator

        orchestrator = LangGraphOrchestrator(llm, generator, logs=logs, prompts=prompts)
    else:
        orchestrator = generator
    consolidator = Consolidator(semantic, procedural, psych, docs, llm, episodic=episodic)

    return Pipeline(
        settings=settings,
        db=db,
        ledger=ledger,
        profiles=profiles,
        registry=registry,
        user_context=SessionUserContext(profiles),
        accounts=accounts,
        outbox=outbox,
        mailer=mailer,
        working=working,
        episodic=episodic,
        semantic=semantic,
        procedural=procedural,
        llm=llm,
        assembler=assembler,
        generator=generator,
        orchestrator=orchestrator,
        self_model=self_model,
        psych=psych,
        stt=FasterWhisperSTT(
            model_size=settings.stt_model_size,
            final_model_size=settings.stt_final_model_size,
            ledger=ledger,
        ),
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
        conversations=conversations,
        extractor=extractor,
        memory_router=MemoryRouter(conversations, extractor, logs=logs),
        preferences=preferences,
        logs=logs,
        feedback=FeedbackStore(docs),
        prompts=prompts,
        scores=scores,
        compactor=SessionCompactor(llm, working, logs=logs),
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
