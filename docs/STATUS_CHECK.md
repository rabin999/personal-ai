# Status check — D-9, D-19, voice path (read-only, no code changed)

```
D-9    : fixed    — generate() guard response_gen.py:531-552; test_e1_enforcement.py:338 GREEN; today's 402 outage degraded to the safe line, not silence
D-19   : open     — only the PROMPT rule (response_gen.py:235); NO mechanical grounding guard; defect test test_core_engine_e2e.py:193 still @pytest.mark.defect, RED at HEAD
voice  : partial  — SHARES the engine (retrieval + D-9 + gates all apply structurally) but NO voice real-call test drives verified retrieval or the D-9/D-19 scenarios
```

## D-9 — silent failure on the reply path → **FIXED**
- Guard: `generate()` wraps the turn in `_run_turn`; `except PROGRAMMING_ERRORS: raise` (F3 preserved),
  any other `Exception` → `_safe_degrade()` → a gated safe line, never silence (`response_gen.py:531-552`).
  Enhancement gates + `OpenRouterLLM.stream()` consumption also hardened to degrade on any provider failure.
- Defect test `test_any_reply_path_exception_yields_a_reply_never_a_raise` (`test_e1_enforcement.py:338`)
  is **GREEN** (ran this session). It still carries a stale `@pytest.mark.defect` marker — cosmetic only.
- `test_a_programming_error_still_fails_loudly` still **GREEN** — no F3 regression.
- Live proof today: the OpenRouter account hit `402 (out of credits)`, every model failed, and the reply
  came back as `"hey, i'm right here with you — what's going on?"` — the safe line, not `reply=""`.
- NOTE: `DEFECTS_FOUND.md` line 11 still lists D-9 in the OPEN column — the doc is stale; the code+test say fixed.

## D-19 — inventing facts about the user's life → **OPEN**
- The only thing standing against it is the PROMPT rule `_JUDGMENT_INSTRUCTIONS` ("…NEVER invent details
  about the user's life", `response_gen.py:235`) — advice, which the defect docstring notes the engine
  "does anyway". There is **no mechanical grounding check** that blocks asserting a personal fact when the
  user's memory/context is empty. `grep` for a personal-fact grounding guard finds only that prompt line.
- Defect test `test_the_engine_never_invents_a_fact_about_the_user` (`test_core_engine_e2e.py:193`) is
  `@pytest.mark.defect`, documented **RED at HEAD**. It could not be re-confirmed today because credits are
  exhausted (LLM fully down → the D-9 safe line fires, which is not a fabrication but also not "I don't know").
- This session did NOT address general D-19. (The companion-name self-naming guard and superseded-fact
  exclusion are narrower/adjacent; verify-before-answer covers volatile EXTERNAL facts, not personal ones.)
- Positive/grounded case works: `test_voice_turn_recalls_a_real_stored_fact` ("when do I take my meds?" →
  "8") shows a real answer is returned when the fact IS present — a fix must keep this green.

## Voice path — **PARTIAL (structurally caught up, independently UNVERIFIED for retrieval + D-9/D-19)**
The live voice path (`VoiceSession → generate_spoken`) shares the exact engine the text path uses, so the
fixes apply by construction:
- (a) Verified retrieval: **yes** — `api/routes/voice.py:147,155` builds `VoiceSession` with
  `generator=pipeline.orchestrator` and `dispatcher=pipeline.dispatcher`, and that dispatcher is wired with
  `retrieval_builder=_build_retrieval` (Crawl4AI) at `composition.py:273-292`. `generate_spoken`'s tool turns
  call the same `web_search` → `RetrievalPort.verify()`.
- (b) D-9 guard: **yes** — `generate_spoken` calls the guarded `generate()`; its streaming path `_stream_reply`
  is itself wrapped (`response_gen.py:976`); `VoiceSession` also has a no-silence guard after `_speak_turn`.
- (c) D-19 guard: **same as text — none** (only the prompt rule); open for both paths.
- (d) Engine gates: **yes** — `_stream_reply` runs `_apply_gates` (self-reflection, boundary, enforcement) +
  prosody strip BEFORE the first sentence is spoken (`response_gen.py:1167-1173`).
- BUT: the voice real-call suite (`test_live_voice_path.py`, `test_progressive_delivery.py`) proves the edge
  contract, memory recall, audio-not-silence, and quality — **none drives voice through verified retrieval,
  nor the D-9 forced-dependency-failure, nor the D-19 empty-memory question.** So voice is caught up in WIRING
  but not independently VERIFIED for those three.

## Smallest next step for each (NOT done this session)
- **D-9**: nothing functional. Optional hygiene: drop the stale `@pytest.mark.defect` marker and move D-9 to
  the fixed column in `DEFECTS_FOUND.md`.
- **D-19**: add a mechanical grounding guard — when a turn asks for a PERSONAL fact (bucket C) and nothing in
  the user's memory/context supplies it, force an honest "I don't know / you haven't told me" instead of a
  drafted name; flip `test_core_engine_e2e.py:193` green while keeping the "meds at 8" grounded test green.
- **Voice**: add ONE real-call test that drives `say_spoken`/`say_voice` through (1) a live-info question
  (verified retrieval span present), (2) a forced non-`LLMUnavailable` failure (reply non-empty), and (3) the
  empty-memory personal question (honest, no invented name) — so the voice path is proven, not just inherited.
