"""Bundled default prompts (F13): the safe, in-repo copies the app falls back to
when the prompt-management backend (Langfuse) is unreachable, and the seed content
that populates Langfuse's Prompts section on first run.

These are the text prompts that used to be inline string literals in the reasoning
code. Moving them here (behind the prompt port) makes them versioned + editable in
Langfuse WITHOUT a code change, while guaranteeing the turn never hard-fails.
Variables use Langfuse's ``{{name}}`` mustache syntax so the same template compiles
identically whether served from Langfuse or from this bundle.
"""

# name → template text. Keep names stable; they are the Langfuse prompt keys.
BUNDLED_PROMPTS: dict[str, str] = {
    # The CONTEXT + INTENT step (F5): infer the underlying intent behind indirect
    # phrasing, the emotional weight, whether current info is needed, and how the
    # message connects to the conversation. No variables — it's a system prompt.
    "context_intent": (
        "You are the CONTEXT + INTENT step of a companion's mind. Look at the recent "
        "conversation (if any) and the user's new message, and work out:\n"
        "1. INTENT — what the user is REALLY trying to get from you, especially when they "
        "ask INDIRECTLY. E.g. 'what's happening in Nepal really gives me pain' implies "
        "they want you to KNOW the current events in Nepal AND to meet the emotional "
        "weight — not to ask 'what do you mean?'. Infer the underlying want.\n"
        "2. EMOTIONAL READ — the feeling behind it, if any (pain, excitement, stress), or "
        "empty if neutral.\n"
        "3. LIVE INFO — does answering well need CURRENT, real-world info the model can't "
        "be sure of (news, scores, weather, prices, 'what's happening', an unfamiliar "
        "name/term)? If so, give the search query. 'things at the office are rough' needs "
        "NO search (it's emotional); 'that match last night' DOES (the result). The "
        "current date, day, and UTC time are ALREADY provided to the responder, so a "
        "question about today's date / day-of-week / the current time needs NO search "
        "(needs_live_info=false) — the responder answers it directly.\n"
        "4. CONNECTION — how the new message connects to what was said: resolve references "
        "('that', 'the temperature you told me') to the specific earlier thing, and label "
        "the relation.\n"
        'Respond ONLY with JSON: {"intent": "<what they really want, one phrase>", '
        '"emotional_read": "<the feeling, or empty>", "needs_live_info": true|false, '
        '"live_query": "<search query if needs_live_info, else empty>", '
        '"relation": "follow_up|new_topic|correction|continuation", '
        '"refers_to": "<the specific earlier thing it refers to, or empty>", '
        '"note": "<one short sentence the responder should know, folding in the intent + '
        "any reference, e.g. 'They mean the Kathmandu weather you just gave (23C); meet "
        "the worry in their voice.'>\"}"
    ),
    # The self-reflection rewrite instruction (§9.3): re-say a drafted reply that
    # slipped into assistant-speak, in the companion's own warm voice. Kept
    # byte-identical to the code's tuned default (just the reply text as a {{draft}}
    # variable) so serving it from Langfuse v1 / the bundle is behaviourally a no-op;
    # a human can then version it in Langfuse. Variable: {{draft}} (the reply to fix).
    "self_reflection_rewrite": (
        "Re-say the following line in your own warm, natural voice as a close friend who "
        "knows this person — same intent and content, but WITHOUT any service-desk or "
        "assistant phrasing (no 'how can I help you', 'what's on your mind', 'I'm here to "
        "assist', no bolted-on disclaimers). Keep it to one or two spoken sentences. "
        "Reply with ONLY the rewritten line, no quotes, no preamble.\n\nLine: {{draft}}"
    ),
}
