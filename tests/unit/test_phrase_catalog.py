"""Unit tests for the dynamic phrase catalog (§8.12 follow-up): the in-memory catalog the live
turn reads, the background regenerator's quality gate, and the refresh loops. The overriding
property under test — the live pick is a pure in-memory lookup, and regeneration only ever makes
the pools fresher or leaves them at the safe defaults, never blanks them or blocks the turn."""

import json

from core.phrases.catalog import PhraseCatalog
from core.phrases.defaults import DEFAULT_POOLS, POOL_SPECS
from core.phrases.generator import PhraseGenerator, _acceptable_spoken
from core.phrases.refresh import refresh_forever, regenerate_once
from tests.fakes import FakeLLM


class _FakeStore:
    def __init__(self, pools: dict[str, list[str]] | None = None) -> None:
        self.saved: dict[str, list[str]] | None = pools
        self.loads = 0

    async def load(self) -> dict[str, list[str]] | None:
        self.loads += 1
        return self.saved

    async def save(self, pools: dict[str, list[str]]) -> None:
        self.saved = pools


# ── catalog ──────────────────────────────────────────────────────────────────


def test_catalog_returns_defaults_before_any_regen() -> None:
    cat = PhraseCatalog()
    assert cat.get("ack_lookup") == DEFAULT_POOLS["ack_lookup"]
    assert cat.get("greeting_angles") == DEFAULT_POOLS["greeting_angles"]


def test_catalog_get_is_pure_and_synchronous() -> None:
    """The hot-path read must be a plain in-memory lookup — no coroutine, no I/O."""
    cat = PhraseCatalog()
    import inspect

    assert not inspect.iscoroutinefunction(cat.get)


def test_apply_swaps_known_pools_and_keeps_defaults_for_others() -> None:
    cat = PhraseCatalog()
    cat.apply({"ack_lookup": ("Fresh one.", "Another fresh line.")})
    assert cat.get("ack_lookup") == ("Fresh one.", "Another fresh line.")
    # A pool that wasn't in the update keeps its default.
    assert cat.get("ack_thinking") == DEFAULT_POOLS["ack_thinking"]


def test_apply_ignores_unknown_and_empty_pools() -> None:
    cat = PhraseCatalog()
    cat.apply({"not_a_pool": ("x",), "ack_lookup": ()})  # unknown + empty
    assert cat.get("ack_lookup") == DEFAULT_POOLS["ack_lookup"]  # empty → default preserved
    assert "not_a_pool" not in cat.snapshot()


def test_apply_drops_blank_lines() -> None:
    cat = PhraseCatalog()
    cat.apply({"ack_lookup": ("Real line.", "   ", "")})
    assert cat.get("ack_lookup") == ("Real line.",)


# ── generator quality gate ─────────────────────────────────────────────────────


def _all_pools_json(overrides: dict[str, list[str]] | None = None) -> str:
    pools = {spec.name: list(DEFAULT_POOLS[spec.name]) for spec in POOL_SPECS}
    if overrides:
        pools.update(overrides)
    return json.dumps({"pools": pools})


async def test_generator_accepts_clean_pools() -> None:
    gen = PhraseGenerator(FakeLLM([_all_pools_json()]), pool_size=8)
    out = await gen.regenerate()
    assert set(out) == {spec.name for spec in POOL_SPECS}  # every pool met its bar
    assert out["ack_lookup"]  # non-empty


async def test_generator_rejects_assistant_speak_and_slang() -> None:
    """Lines that trip the live scrubber (assistant-speak / slang) are dropped; if that leaves a
    pool too thin, the pool is omitted so the catalog keeps its safe default."""
    bad = ["How can I help you?", "What's on your mind?", "One sec dude.", "Hang on bro."]
    gen = PhraseGenerator(FakeLLM([_all_pools_json({"ack_lookup": bad})]), pool_size=8)
    out = await gen.regenerate()
    # ack_lookup was all garbage → below min_lines → omitted (caller keeps the default).
    assert "ack_lookup" not in out
    # the other pools were clean and still came through
    assert "ack_thinking" in out


async def test_generator_enforces_word_cap() -> None:
    spec = next(s for s in POOL_SPECS if s.name == "ack_lookup")
    long_line = " ".join(["word"] * (spec.max_words + 5)) + "."
    lines = [long_line, *DEFAULT_POOLS["ack_lookup"]]
    gen = PhraseGenerator(FakeLLM([_all_pools_json({"ack_lookup": lines})]), pool_size=8)
    out = await gen.regenerate()
    assert long_line not in out["ack_lookup"]  # over the cap → dropped


async def test_generator_survives_unparseable_json() -> None:
    gen = PhraseGenerator(FakeLLM(["not json at all {{{"]))
    assert await gen.regenerate() == {}  # → caller keeps current pools


async def test_generator_accepts_bare_object_without_pools_key() -> None:
    bare = json.dumps({spec.name: list(DEFAULT_POOLS[spec.name]) for spec in POOL_SPECS})
    gen = PhraseGenerator(FakeLLM([bare]), pool_size=8)
    out = await gen.regenerate()
    assert "ack_lookup" in out


def test_acceptable_spoken_matches_the_live_scrubber() -> None:
    assert _acceptable_spoken("On it — let me check.", 8)
    assert not _acceptable_spoken("How can I help you?", 8)  # assistant-speak
    assert not _acceptable_spoken("One sec dude.", 8)  # slang
    assert not _acceptable_spoken("", 8)  # empty


# ── refresh loops ───────────────────────────────────────────────────────────────


async def test_regenerate_once_applies_and_persists() -> None:
    cat = PhraseCatalog()
    store = _FakeStore()
    gen = PhraseGenerator(FakeLLM([_all_pools_json({"ack_lookup": ["Brand new line."]})]))
    # ack_lookup has 1 line (< min) so it's omitted; use a full clean set instead:
    gen = PhraseGenerator(FakeLLM([_all_pools_json()]))
    n = await regenerate_once(gen, store, cat)
    assert n > 0
    assert store.saved is not None  # persisted for the edge to read
    assert cat.get("ack_lookup")  # applied locally too


async def test_regenerate_once_noop_when_generator_returns_nothing() -> None:
    cat = PhraseCatalog()
    store = _FakeStore()
    gen = PhraseGenerator(FakeLLM(["garbage"]))
    assert await regenerate_once(gen, store, cat) == 0
    assert store.saved is None  # nothing persisted
    assert cat.get("ack_lookup") == DEFAULT_POOLS["ack_lookup"]  # defaults intact


async def test_refresh_forever_loads_store_into_catalog_once() -> None:
    """One tick of the edge refresher pulls the stored pools into the in-memory catalog."""
    import asyncio

    cat = PhraseCatalog()
    store = _FakeStore({"ack_lookup": ["Stored line one.", "Stored line two."]})
    task = asyncio.create_task(refresh_forever(store, cat, interval_s=0.01))
    for _ in range(200):
        if cat.get("ack_lookup") == ("Stored line one.", "Stored line two."):
            break
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert cat.get("ack_lookup") == ("Stored line one.", "Stored line two.")
