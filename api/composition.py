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
from typing import Any, cast

from adapters.db import Database
from adapters.graph.graphiti import GraphitiGraphStore
from adapters.llm.openrouter import OpenRouterLLM
from adapters.logging.factory import build_log_sinks
from adapters.logging.trace_sink import TraceStoreLogSink
from adapters.outbox import OutboxStore, WelcomeMailer
from adapters.phrase.redis_store import RedisPhraseStore
from adapters.preference.mem0_adapter import Mem0PreferenceMemory
from adapters.prompt.langfuse_prompt import BundledPromptProvider
from adapters.queue.redis import RedisTaskQueue
from adapters.retrieval import Crawl4AIClient, RetrievalConfig, build_crawl4ai_retrieval
from adapters.search.brave import BraveSearch
from adapters.search.serper import SerperSearch
from adapters.ser.emotion2vec_client import Emotion2VecSER
from adapters.sound.heuristic import HeuristicSoundClassifier
from adapters.stt.faster_whisper import FasterWhisperSTT
from adapters.stt.grok import GrokSTT
from adapters.tts.grok import GrokTTS
from adapters.user_context.accounts import AccountStore
from adapters.user_context.session import SessionUserContext
from adapters.vector.qdrant import QdrantVectorStore
from config.settings import Settings
from core.cost import CostLedger
from core.eval.evaluator import TurnEvaluator
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
from core.phrases.catalog import PhraseCatalog
from core.phrases.generator import PhraseGenerator
from core.profile import ProfileService, TraitRegistry
from core.projects.service import ProjectService
from core.psych.consolidation import Consolidator
from core.psych.persona import PersonaStore
from core.psych.user_model import PsychUserModel
from core.reasoning.orchestrator import Orchestrator, assert_orchestrator_contract
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
from ports.doc_store import DocStore
from ports.llm import Tier
from ports.prompt import PromptProvider
from ports.retrieval import RetrievalPort
from ports.score_sink import ScoreSink
from ports.stt import STT

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
    docs: DocStore  # raw user-scoped doc store (account deletion, etc.)
    vectors: QdrantVectorStore  # Qdrant (episodic + entity collections) — account deletion
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
    persona: PersonaStore
    stt: STT  # faster-whisper (local) or Grok STT, per settings.stt_engine
    tts: GrokTTS
    ser: Emotion2VecSER
    sound_classifier: HeuristicSoundClassifier  # U10-U12 sound-awareness stage
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
    phrases: PhraseCatalog  # §8.12: live interjection/greeting pools (regenerated in background)
    phrase_store: RedisPhraseStore  # shared store the worker writes + the edge reads
    phrase_generator: PhraseGenerator  # background regenerator (worker-side)
    langfuse: Any | None = None  # LangfuseTraceSink (deep-link builder), if enabled
    evaluator: TurnEvaluator | None = None  # §6/§7 live LLM-as-judge, if enabled

    async def aclose(self) -> None:
        await self.ledger.flush()
        await self.queue.aclose()
        await self.phrase_store.aclose()
        await self.db.aclose()
        self.logs.close()


def _build_stt(settings: Settings, ledger: CostLedger) -> STT:
    """Pick the STT engine (#18): xAI Grok STT (vendor-grade) or local faster-whisper
    ($0, default), by ``settings.stt_engine`` — one wiring line, ``core/`` untouched."""
    if settings.stt_engine == "grok":
        # Grok STT (fast, vendor-grade) with local faster-whisper as a fallback: xAI STT
        # intermittently ReadTimeouts from the box, and without a net a dropped utterance reads
        # as "it didn't hear me" + a long wait (real prod incident). The fallback runs a FAST
        # model (stt_fallback_model_size, ~1-2s) — NOT the slow "small" (~5-10s on CPU) — so a slow
        # Grok call recovers in a couple of seconds, not fifteen. preload() warms it at startup.
        fallback = FasterWhisperSTT(
            model_size=settings.stt_fallback_model_size,
            final_model_size=settings.stt_fallback_model_size,
            ledger=ledger,
        )
        logger.info("STT engine: Grok STT (xAI) with fast local whisper fallback")
        return GrokSTT(settings, ledger=ledger, fallback=fallback)
    return FasterWhisperSTT(
        model_size=settings.stt_model_size,
        final_model_size=settings.stt_final_model_size,
        ledger=ledger,
    )


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
    langfuse_sink: Any | None = None
    if settings.langfuse_enabled and settings.langfuse_public_key:
        try:
            from adapters.tracing.langfuse_sink import LangfuseTraceSink

            langfuse_sink = LangfuseTraceSink(
                settings.langfuse_public_key,
                settings.langfuse_secret_key,
                settings.langfuse_host,
            )
            trace_sinks.append(langfuse_sink)
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
    persona = PersonaStore(docs)  # brief U2: dynamic per-user persona ("how to talk")

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
    providers = _search_providers(settings)
    web_search = WebSearch(docs, llm, *providers, ledger=ledger)
    # §15 verified retrieval: read the pages + cross-check instead of trusting snippets.
    # One shared Crawl4AI client; a per-CALL builder scopes the formatter LLM + cost to the
    # turn's resolved user_id (invariant 2) — the web_search tool is multi-tenant, the build
    # is not. Reuses the Serper provider for stage S1.
    retrieval_cfg = RetrievalConfig()
    retrieval_fetcher = Crawl4AIClient(
        base_url=retrieval_cfg.base_url,
        api_token=retrieval_cfg.api_token,
        page_timeout_ms=retrieval_cfg.page_timeout_ms,
        fetch_deadline_ms=retrieval_cfg.fetch_deadline_ms,
        max_concurrency=retrieval_cfg.max_concurrency,
        word_count_threshold=retrieval_cfg.word_count_threshold,
    )

    def _build_retrieval(user_id: str, session_id: str | None) -> RetrievalPort:
        return build_crawl4ai_retrieval(
            search=providers[0],
            llm=llm,
            user_id=user_id,
            ledger=ledger,
            session_id=session_id,
            config=retrieval_cfg,
            fetcher=retrieval_fetcher,
            logs=logs,
        )

    register_core_tools(  # the MVP core tool set (§8.5) — so the loop can act
        tool_registry,
        episodic=episodic,
        semantic=semantic,
        web_search=web_search,
        profiles=profiles,
        projects=projects,
        results=tool_results,
        retrieval_builder=_build_retrieval,
    )

    # §2 Mem0 preference memory (fast personalization layer). Guarded init:
    # a failure degrades to None and the app still runs without it.
    preferences = Mem0PreferenceMemory(settings) if settings.preference_memory_enabled else None
    extractor = MemoryExtractor(
        llm, episodic, semantic, projects, persona=persona, preferences=preferences
    )

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
        persona=persona,  # brief U2: persona drives HOW the reply is delivered
        preferences=preferences,
        recall=ConversationRecall(conversations),  # F3/F4 conversation-recall routing
    )
    # §8.12 dynamic phrases: one shared in-memory catalog (defaults until the worker fills it),
    # a Redis store the worker writes + the edge reads, and the background regenerator. The live
    # turn only ever reads `phrases` — a pure in-memory lookup — so none of this is on the reply
    # path. Cast keeps the tier a Literal from the free-form settings string.
    phrases = PhraseCatalog()
    phrase_store = RedisPhraseStore(settings)
    phrase_generator = PhraseGenerator(
        llm,
        tier=cast(Tier, settings.phrase_regen_tier),
        pool_size=settings.phrase_pool_size,
        logs=logs,
    )
    generator = ResponseGenerator(
        llm,
        self_model,
        registry,
        logs=logs,
        max_turn_cost_usd=settings.max_turn_cost_usd,
        reasoning_tier=settings.reasoning_tier,  # A2: mature model for the main turn
        prompts=prompts,  # F13: managed self-reflection prompt (Langfuse or bundled)
        progress_filler_gap_s=settings.progress_filler_gap_s,  # §8.12: fill dead air on slow turns
        progress_filler_max=settings.progress_filler_max,
        progress_filler_apology_after=settings.progress_filler_apology_after,
        phrases=phrases,  # §8.12: interjection pools (regenerated in background)
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
    # F3: fail FAST if the wired engine can't accept the call the voice edge makes.
    # This exact mismatch previously reached production and was absorbed by a broad
    # `except Exception` as silence on every voice turn.
    assert_orchestrator_contract(orchestrator)
    consolidator = Consolidator(semantic, procedural, psych, docs, llm, episodic=episodic)

    return Pipeline(
        settings=settings,
        db=db,
        docs=docs,
        vectors=vectors,
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
        persona=persona,
        stt=_build_stt(settings, ledger),
        tts=GrokTTS(settings, ledger=ledger),
        ser=Emotion2VecSER(settings),
        sound_classifier=HeuristicSoundClassifier(),
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
        phrases=phrases,
        phrase_store=phrase_store,
        phrase_generator=phrase_generator,
        langfuse=langfuse_sink,
        # S5: a DEDICATED LLM client for the judge — its own AsyncOpenAI connection pool,
        # and `logs=None` so its background call never lands inside the live turn's trace.
        evaluator=TurnEvaluator(
            OpenRouterLLM(settings, ledger=ledger, tiers=tiers),
            scores,
            enabled=settings.langfuse_eval_enabled,
            sample_rate=settings.eval_sample_rate,
        ),
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
