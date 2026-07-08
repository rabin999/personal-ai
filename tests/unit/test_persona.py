"""Unit tests for the dynamic user persona (brief U0-U2) — deterministic merge logic."""

from core.psych.persona import INJECT_THRESHOLD, PersonaStore, StyleSignal
from tests.fakes import FakeDocStore

USER = "u_demo_001"
OTHER = "u_demo_002"


async def test_stated_preference_injects_immediately() -> None:
    """A directly-stated preference shapes the very next reply (high confidence)."""
    store = PersonaStore(FakeDocStore())
    await store.apply(
        USER, [StyleSignal(text="You like me to keep it short", dimension="brevity", stated=True)]
    )
    rendered = await store.render_for_prompt(USER)
    assert "keep it short" in rendered.lower()


async def test_inferred_style_needs_reinforcement_before_it_drives() -> None:
    """A merely-inferred style starts below threshold and only injects once repeated
    (distinguishes a durable shift from a one-off)."""
    store = PersonaStore(FakeDocStore())
    sig = StyleSignal(text="You seem to prefer short answers", dimension="brevity", stated=False)
    await store.apply(USER, [sig])
    assert await store.render_for_prompt(USER) == ""  # 0.3 < threshold
    await store.apply(USER, [sig])  # 0.45
    await store.apply(USER, [sig])  # 0.60 → injected
    assert "short" in (await store.render_for_prompt(USER)).lower()


async def test_contradiction_supersedes_via_validity_window() -> None:
    """A later contradicting signal on the same dimension supersedes the old one —
    no stale + new both active."""
    store = PersonaStore(FakeDocStore())
    await store.apply(
        USER, [StyleSignal(text="You like short, blunt answers", dimension="brevity", stated=True)]
    )
    await store.apply(
        USER,
        [
            StyleSignal(
                text="You now want detailed, thorough answers", dimension="brevity", stated=True
            )
        ],
    )
    persona = await store.get(USER)
    active = persona.active()
    assert len(active) == 1
    assert "detailed" in active[0].text.lower()
    # The superseded note is retained with a validity window (history, not deleted).
    superseded = [n for n in persona.notes if n.valid_to is not None]
    assert len(superseded) == 1


async def test_reinforcement_raises_confidence_not_duplicates() -> None:
    store = PersonaStore(FakeDocStore())
    sig = StyleSignal(text="You enjoy a bit of humor", dimension="humor", stated=False)
    await store.apply(USER, [sig])
    await store.apply(USER, [sig])
    persona = await store.get(USER)
    assert len(persona.active()) == 1
    assert persona.active()[0].evidence_count == 2


async def test_interests_and_sensitivities_render() -> None:
    store = PersonaStore(FakeDocStore())
    await store.apply(
        USER,
        [
            StyleSignal(text="You love football", kind="interest", stated=True),
            StyleSignal(
                text="Money topics tend to stress you, so keep those calm",
                kind="sensitivity",
                stated=True,
            ),
        ],
    )
    rendered = (await store.render_for_prompt(USER)).lower()
    assert "football" in rendered
    assert "money" in rendered


async def test_persona_is_user_scoped() -> None:
    """One user's persona never leaks into another's (§0.5 isolation)."""
    store = PersonaStore(FakeDocStore())
    await store.apply(
        USER, [StyleSignal(text="You like it blunt", dimension="directness", stated=True)]
    )
    assert await store.render_for_prompt(OTHER) == ""
    assert await store.readable(OTHER) == []


async def test_readable_marks_active_flag() -> None:
    store = PersonaStore(FakeDocStore())
    await store.apply(USER, [StyleSignal(text="You like humor", dimension="humor", stated=False)])
    items = await store.readable(USER)
    assert len(items) == 1
    assert items[0]["active"] is (items[0]["confidence"] >= INJECT_THRESHOLD)
