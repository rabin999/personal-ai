"""Unit tests for per-user fast-model selection (§4)."""

from adapters.llm.openrouter import OpenRouterLLM
from config.settings import Settings


def _llm() -> OpenRouterLLM:
    return OpenRouterLLM(
        Settings(),
        tiers={
            "simple": ["prov/flash-lite", "prov/nano"],
            "moderate": ["prov/flash", "prov/mini"],
            "complex": ["prov/pro"],
        },
    )


def test_fast_model_choices_are_simple_plus_moderate_deduped() -> None:
    choices = _llm().fast_model_choices()
    assert choices == ["prov/flash-lite", "prov/nano", "prov/flash", "prov/mini"]
    assert "prov/pro" not in choices  # complex tier is not user-selectable


def test_assembly_sets_override_only_on_non_complex_turns() -> None:
    import asyncio

    from core.memory.entities import EntityResolver
    from core.memory.episodic import EpisodicMemory
    from core.memory.procedural import ProceduralMemory
    from core.memory.semantic import SemanticMemory
    from core.memory.working import WorkingMemory
    from core.profile import ProfileService, TraitRegistry
    from core.reasoning.prompt_assembly import PromptAssembler
    from core.reasoning.self_model import SelfModel
    from tests.fakes import FakeDocStore, FakeGraphStore, FakeVectorStore

    async def run() -> None:
        docs = FakeDocStore()
        vectors = FakeVectorStore()
        profiles = ProfileService(docs)
        await profiles.first_run_sync("u_demo_001")
        await profiles.update("u_demo_001", {"model_prefs": {"fast_model": "prov/nano"}})
        assembler = PromptAssembler(
            profiles,
            TraitRegistry(docs, profiles),
            WorkingMemory(),
            EpisodicMemory(vectors),
            SemanticMemory(FakeGraphStore()),
            ProceduralMemory(docs),
            EntityResolver(vectors),
            SelfModel(docs, vectors, llm=None),
        )
        from core.reasoning.prompt_assembly import AssembledPrompt

        simple = await assembler.assemble("u_demo_001", "s1", "hey")
        assert isinstance(simple, AssembledPrompt)
        assert simple.model_override == "prov/nano"  # non-complex → honored

        heavy_utterance = (
            "why should I compare these two strategies, walk me through "
            "the tradeoffs and plan"
        )
        heavy = await assembler.assemble("u_demo_001", "s1", heavy_utterance)
        assert isinstance(heavy, AssembledPrompt)
        assert heavy.complexity_hint == "complex"
        assert heavy.model_override is None  # hard turns still route to the strong tier

    asyncio.run(run())
