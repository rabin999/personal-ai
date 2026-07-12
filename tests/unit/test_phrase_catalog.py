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
        self.uses: dict[tuple[str, str], int] = {}

    async def load(self) -> dict[str, list[str]] | None:
        self.loads += 1
        return self.saved

    async def save(self, pools: dict[str, list[str]]) -> None:
        self.saved = pools

    async def bump_uses(self, counts: dict[tuple[str, str], int]) -> None:
        for k, n in counts.items():
            self.uses[k] = self.uses.get(k, 0) + n

    async def used_over(self, threshold: int) -> dict[str, list[str]]:
        worn: dict[str, list[str]] = {}
        for (pool, line), n in self.uses.items():
            if n > threshold:
                worn.setdefault(pool, []).append(line)
        return worn

    async def reset_uses(self, keys: list[tuple[str, str]]) -> None:
        for k in keys:
            self.uses.pop(k, None)


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


# ── usage-driven worn-line refresh ─────────────────────────────────────────────


def test_record_use_and_drain() -> None:
    cat = PhraseCatalog()
    cat.record_use("ack_lookup", "Let me look that up.")
    cat.record_use("ack_lookup", "Let me look that up.")
    cat.record_use("progress_lookup", "Still on it — almost there.")
    drained = cat.drain_uses()
    assert drained[("ack_lookup", "Let me look that up.")] == 2
    assert drained[("progress_lookup", "Still on it — almost there.")] == 1
    assert cat.drain_uses() == {}  # cleared after draining


async def test_replace_worn_swaps_only_the_worn_line_and_keeps_size() -> None:
    from core.phrases.refresh import replace_worn_once

    cat = PhraseCatalog()
    original = list(DEFAULT_POOLS["ack_lookup"])
    store = _FakeStore({"ack_lookup": list(original)})
    worn_line = original[0]
    store.uses = {("ack_lookup", worn_line): 11}  # over the threshold of 10
    gen = PhraseGenerator(FakeLLM([json.dumps({"lines": ["A shiny brand-new lookup line."]})]))

    n = await replace_worn_once(gen, store, cat, threshold=10)
    assert n == 1
    new_pool = store.saved["ack_lookup"]
    assert len(new_pool) == len(original)  # size preserved (1:1 swap)
    assert worn_line not in new_pool  # the worn line is gone
    assert "A shiny brand-new lookup line." in new_pool  # the fresh one is in
    assert all(ln in new_pool for ln in original[1:])  # the other lines were untouched
    assert store.uses == {}  # the replaced line's count was reset


async def test_replace_worn_keeps_worn_line_when_no_replacement() -> None:
    """A provider hiccup (no fresh line) must leave the worn line in place and its count intact,
    so it's retried — never silently dropped."""
    from core.phrases.refresh import replace_worn_once

    cat = PhraseCatalog()
    original = list(DEFAULT_POOLS["ack_lookup"])
    store = _FakeStore({"ack_lookup": list(original)})
    worn_line = original[0]
    store.uses = {("ack_lookup", worn_line): 11}
    gen = PhraseGenerator(FakeLLM(["garbage not json"]))

    n = await replace_worn_once(gen, store, cat, threshold=10)
    assert n == 0
    assert store.uses == {("ack_lookup", worn_line): 11}  # untouched → retried next tick


async def test_replace_worn_noop_below_threshold() -> None:
    from core.phrases.refresh import replace_worn_once

    store = _FakeStore({"ack_lookup": list(DEFAULT_POOLS["ack_lookup"])})
    store.uses = {("ack_lookup", DEFAULT_POOLS["ack_lookup"][0]): 5}  # ≤ threshold
    gen = PhraseGenerator(FakeLLM([json.dumps({"lines": ["unused"]})]))
    assert await replace_worn_once(gen, store, PhraseCatalog(), threshold=10) == 0


async def test_regenerate_replacements_are_distinct_and_valid() -> None:
    keep = list(DEFAULT_POOLS["ack_lookup"])
    # One duplicate of an existing line, one assistant-speak, one clean new line.
    resp = json.dumps({"lines": [keep[0], "How can I help you?", "Fresh clean lookup line."]})
    gen = PhraseGenerator(FakeLLM([resp]))
    out = await gen.regenerate_replacements("ack_lookup", keep=keep, n=2)
    assert "Fresh clean lookup line." in out
    assert keep[0] not in out  # duplicate rejected
    assert "How can I help you?" not in out  # assistant-speak rejected


async def test_edge_refresh_flushes_uses_to_store() -> None:
    """One edge tick drains the catalog's in-memory use counts into the shared store."""
    import asyncio

    from core.phrases.refresh import refresh_forever

    cat = PhraseCatalog()
    cat.record_use("ack_lookup", "Let me look that up.")
    store = _FakeStore({"ack_lookup": list(DEFAULT_POOLS["ack_lookup"])})
    task = asyncio.create_task(refresh_forever(store, cat, interval_s=0.01))
    for _ in range(200):
        if store.uses.get(("ack_lookup", "Let me look that up.")):
            break
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert store.uses[("ack_lookup", "Let me look that up.")] == 1
    assert cat.drain_uses() == {}  # counts were drained, not left to double-count


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
