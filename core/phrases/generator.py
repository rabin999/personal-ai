"""`PhraseGenerator` — regenerates the spoken-phrase pools in the background so they don't
feel static (§8.12 follow-up). Runs OFF the conversation path (worker process); its output is
stored and the live edge only ever reads the in-memory result.

Quality is enforced, not assumed (CLAUDE.md §6/§7): one cheap LLM call rewrites every pool in
the companion's voice, then EACH generated line must survive the very same forbidden-assistant-
speak + slang scrubbers the live reply runs (`core.reasoning.style`) — so regeneration can never
smuggle in "How can I help you?" or "dude". A pool that ends up with too few acceptable lines is
dropped from the result, and the catalog keeps that pool's hand-written default. Every call logs
its cost to the ledger via the LLM adapter (purpose="phrase_regen").
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, ValidationError

from core.observability.logger import StructuredLogger
from core.phrases.defaults import DEFAULT_POOLS, POOL_SPECS, PoolSpec
from core.reasoning.style import find_forbidden, scrub_forbidden, strip_slang
from ports.llm import LLM, LLMUnavailable, Tier

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You write SHORT spoken one-liners for a warm, human AI companion (not an assistant). "
    "The voice is casual and close, like a good friend: contractions, plain everyday words, "
    "no corporate or customer-service tone. BANNED, always: assistant-speak like 'How can I "
    "help you?', 'What's on your mind?', 'Is there anything else', 'I'm here to help'; and "
    "frat-boy slang like 'dude', 'bro', 'bruh'. These lines are said BEFORE the real answer is "
    "known, so they must state NO facts, numbers, names, or results — only the vibe of the beat. "
    "Return STRICT JSON: an object mapping each requested pool name to an array of distinct "
    "lines. No commentary, no markdown."
)


class _Payload(BaseModel):
    pools: dict[str, list[str]]


# The live `strip_slang` only excises slang in LEADING/opener position, so a trailing "dude"
# would survive it. The regenerator must be STRICTER than the live scrubber (a generated line is
# untrusted), so we reject these frat-boy tokens anywhere in the line.
_SLANG_ANYWHERE = re.compile(r"\b(dude|bro|bruh|broski|fam|homie|homies|yo)\b", re.IGNORECASE)


def _acceptable_spoken(line: str, max_words: int) -> bool:
    """A generated spoken line is acceptable only if it survives the SAME scrubbers the live
    reply runs, unchanged — no assistant-speak, no slang — and stays short. Stricter than the
    live path on slang position, because a regenerated line has no author we trust."""
    s = line.strip()
    if not s or len(s.split()) > max_words:
        return False
    if find_forbidden(s):  # assistant-speak phrases
        return False
    if _SLANG_ANYWHERE.search(s) or strip_slang(s) != s:  # frat-boy slang, anywhere
        return False
    # ...and nothing else the live scrubber would excise (assistant-speak clauses, etc.)
    return scrub_forbidden(s).strip() == s


def _dedupe_keep_order(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        key = ln.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(ln.strip())
    return out


class PhraseGenerator:
    def __init__(
        self,
        llm: LLM,
        *,
        tier: Tier = "simple",
        pool_size: int = 8,
        specs: tuple[PoolSpec, ...] = POOL_SPECS,
        logs: StructuredLogger | None = None,
    ) -> None:
        self._llm = llm
        self._tier = tier  # a cheap tier: these are throwaway one-liners, not reasoning
        self._pool_size = pool_size
        self._specs = specs
        self._logs = logs

    def _user_prompt(self) -> str:
        lines = [f"Write {self._pool_size} lines for each of these pools:\n"]
        for spec in self._specs:
            examples = ", ".join(f'"{e}"' for e in DEFAULT_POOLS.get(spec.name, ())[:3])
            kind = "spoken line" if spec.spoken else "stage direction (NOT spoken verbatim)"
            lines.append(
                f'- "{spec.name}" ({kind}, ≤{spec.max_words} words each): {spec.intent}.\n'
                f"  in the spirit of: {examples}"
            )
        lines.append(
            '\nReturn JSON shaped exactly as {"pools": {"<pool_name>": ["line", ...], ...}} '
            "covering every pool above."
        )
        return "\n".join(lines)

    async def regenerate(self, user_id: str = "_system") -> dict[str, list[str]]:
        """Produce fresh, validated pools. Returns only pools that met their `min_lines` bar
        after validation; the caller keeps defaults for any pool omitted here. Never raises for
        a provider/parse failure — returns {} so the current pools simply stand."""
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": self._user_prompt()},
        ]
        try:
            result = await self._llm.complete(
                user_id,
                messages,
                self._tier,
                response_format={"type": "json_object"},
                temperature=1.0,  # variety is the whole point here
                purpose="phrase_regen",
            )
        except LLMUnavailable:
            logger.warning("phrase regen: LLM unavailable; keeping current pools")
            return {}
        raw = result.text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{") : raw.rfind("}") + 1]
        try:
            payload = _Payload.model_validate_json(raw)
        except ValidationError:
            # Some models wrap the object bare (no "pools" key) — accept that shape too.
            try:
                data = json.loads(raw)
                payload = _Payload(pools=data.get("pools", data))
            except (json.JSONDecodeError, ValidationError, TypeError, AttributeError):
                logger.warning("phrase regen: unparseable JSON; keeping current pools")
                return {}

        specs_by_name = {s.name: s for s in self._specs}
        accepted: dict[str, list[str]] = {}
        for name, spec in specs_by_name.items():
            candidates = _dedupe_keep_order(payload.pools.get(name, []))
            if spec.spoken:
                good = [c for c in candidates if _acceptable_spoken(c, spec.max_words)]
            else:  # greeting ANGLES are directives, not spoken verbatim — length-bound only
                good = [c for c in candidates if 0 < len(c.split()) <= spec.max_words]
            good = good[: self._pool_size]
            if len(good) >= spec.min_lines:
                accepted[name] = good
            else:
                logger.info(
                    "phrase regen: pool %r kept default (%d/%d acceptable)",
                    name,
                    len(good),
                    spec.min_lines,
                )
        if self._logs is not None:
            self._logs.log(
                "info",
                "phrase_regen",
                message=f"regenerated {len(accepted)}/{len(self._specs)} pools",
                pools={k: len(v) for k, v in accepted.items()},
            )
        return accepted
