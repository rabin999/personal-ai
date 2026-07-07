# Remediation Log

Autonomous hardening pass. Root causes, fixes, and framework adopt/reject decisions.
Newest entries at the bottom of each section. Companion doc: `GAP_ANALYSIS.md`.

---

## Framework decisions (adopt / reject + why)

| Area | Decision | Rationale |
|---|---|---|
| Observability | **Persist traces to Mongo + debug endpoint** (reject full Langfuse *for now*) | Trace events already exist and stream to the UI; they were only missing durability. A `turn_traces` Mongo collection + `GET /debug/traces/{session}` meets the brief's "durable, queryable, inspectable" bar with **zero new infrastructure**, consistent with the local-first/cost-conscious stack. Standing up a self-hosted Langfuse server + SDK is a larger change with its own ops surface; recommended as a follow-up once the pipeline is stable. The trace schema is span-compatible so a Langfuse exporter can be added later without touching call sites. |
| Voice pipeline | **Keep the current asyncio runtime; fix the real bugs** (defer Pipecat/LiveKit migration) | The reported voice defects trace to *specific, fixable* bugs (no pre-roll buffer; delivery double-fire), not to the runtime being fundamentally wrong. The gate/endpoint/barge-in state machine is sound in code. A full framework migration is high-risk and unverifiable without duplex-audio hardware in this environment. Pipecat adoption remains the right call for production AEC/transport and is recommended, but is out of scope for an unattended pass that cannot A/B it against a mic. |
| Memory | **Keep Graphiti (§6) + Qdrant (§5); add a raw tool-result store** | Graphiti/Qdrant are already wired per spec. The gap was durability of *tool outputs* and a conversational path to *create* project instances — both added without swapping frameworks. Mem0 not adopted: Graphiti already covers the semantic/temporal role the spec assigns; adding Mem0 would duplicate it. |
| Search | **Keep Serper→Brave** | Already correct. No phantom tools present in the registry (no "deep research"). |

---

## Root causes found & fixed

_(appended as work proceeds)_
