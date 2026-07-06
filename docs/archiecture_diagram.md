```
================================================================================
                     PERSONAL AI COMPANION — SYSTEM ARCHITECTURE
                          (multi-user, modular monolith + edge services)
================================================================================

┌──────────────────────────────────────────────────────────────────────────────┐
│                                  USERS (many)                                  │
│              each interacts via a client (mobile / desktop / web)              │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                     │  audio + text, per-user (user_id on everything)
                                     ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              API / SERVING EDGE (FastAPI, ASGI)                │
│   • auth + user_id resolution        • session start/stop                     │
│   • SSE / WebSocket streaming (tokens out, audio out)                          │
│   • routes: chat, config, projects, "what do you know about me" / delete       │
└──────────┬───────────────────────────────────────────────┬───────────────────┘
           │ real-time voice session                        │ text / control requests
           ▼                                                ▼
┌──────────────────────────────┐              ┌──────────────────────────────────┐
│   VOICE SESSION RUNTIME       │              │        CORE APP (modular          │
│   (separate, stateful,        │              │        monolith, provider-        │
│    latency-critical)          │              │        agnostic domain)           │
│                               │              │                                   │
│  mic ──▶ AEC ──▶ noise supp.  │  clean audio │  ┌────────────────────────────┐  │
│      ──▶ AGC ──▶ Silero VAD ──┼─────────────▶│  │  PROMPT ASSEMBLY (§10)      │  │
│   (all toggleable; VAD gate   │   transcript │  │  gathers all layers in      │  │
│    = idle is FREE)            │◀─────────────┤  │  priority order + budgets   │  │
│         │                     │   response   │  └───────────┬────────────────┘  │
│         ▼                     │   (TTS-tagged│              │                    │
│   Semantic Endpointing (§21)  │    text)     │   ┌──────────▼──────────────┐    │
│   (silence + completeness)    │              │   │ 1 ENTITY RESOLUTION (§8) │    │
│         │                     │              │   │ 2 MEMORY RETRIEVAL       │    │
│         ▼                     │              │   │ 3 PROJECT DATA (§16)     │    │
│   Barge-in handler (§24) ─────┼──stop TTS────┤   │ 4 TRAITS/CONFIG (§2)     │    │
│                               │              │   │ 5 SELF-MODEL (§9)        │    │
└───────┬───────────────┬───────┘              │   └──────────┬──────────────┘    │
        │ audio window  │ audio out                          │                    │
        ▼               ▲                       │   ┌──────────▼──────────────┐    │
┌───────────────┐  ┌────┴────────┐              │   │  LLM ROUTER (§11)        │    │
│  STT ADAPTER  │  │ TTS ADAPTER │              │   │  complexity tiering,     │    │
│  (§20)        │  │ (§23 Grok,  │              │   │  fallback ── via ───────┐│    │
│  streaming,   │  │  inline     │              │   └──────────┬──────────────┘│    │
│  vocab-boost  │  │  tags)      │              │              │               ││    │
└───────────────┘  └─────────────┘              │   ┌──────────▼──────────────┐│    │
        ▲                                        │   │ RESPONSE GEN + GATES(§12)││    │
        │ audio                                  │   │ • curiosity gate         ││    │
┌───────┴───────┐                                │   │ • overclaim rewrite (§9) ││    │
│  SER SERVICE  │ valence/arousal/label          │   │ • pull-based disclosure  ││    │
│  (§22,        │───────────────────────────────▶│   │ • Pydantic-validated     ││    │
│   emotion2vec,│  (latency-tolerant,            │   │   dual output            ││    │
│   GPU box)    │   may lag one turn)            │   └───────┬──────────────────┘│    │
└───────────────┘                                │           │ tool call?        ││    │
                                                 │           ▼                   ││    │
                                                 │   ┌────────────────────────┐  ││    │
                                                 │   │ TOOL DISPATCHER (§13)   │  ││    │
                                                 │   │ MCP-shaped registry;    │  ││    │
                                                 │   │ readonly=inline /        │  ││    │
                                                 │   │ background=queue /       │  ││    │
                                                 │   │ action=confirm+          │  ││    │
                                                 │   │ non-interruptible        │  ││    │
                                                 │   └───┬─────────────┬────────┘  ││    │
                                                 │       │ inline      │ slow→queue ││    │
                                                 │       ▼             ▼            ││    │
                                                 │  (memory/project   [BACKGROUND   ││    │
                                                 │   readonly tools)   QUEUE §14]   ││    │
                                                 └───────────────────────┬──────────┘│    │
                                                                         │           │    │
       ┌─────────────────────────────────────────────────────────────────┘           │    │
       │                                                                              │    │
       ▼                                                                              │    │
┌──────────────────────────────────────┐        ┌──────────────────────────────────┐│    │
│   BACKGROUND WORKERS (separate proc)  │        │        LLM PROVIDER (cloud)       ││    │
│                                       │        │   OpenRouter gateway ◀───────────┘│    │
│  • Task Worker (§14):                 │        │   • LLM (tiered: cheap/mid/strong)│    │
│    runs web search as DETACHED call ──┼───────▶│   • STT endpoint                  │    │
│    (never blocks conversation)        │        │   • TTS endpoint (Grok voice)     │    │
│    ── Web Search (§15):               │        └──────────────────────────────────┘     │
│       Serper primary → Brave fallback │        ┌──────────────────────────────────┐     │
│       + cache + summarize pass ───────┼───────▶│   SEARCH PROVIDERS                │     │
│                                       │        │   Serper (primary) / Brave (fb)   │     │
│  • Consolidation Worker (§18):        │        └──────────────────────────────────┘     │
│    after session close, ASYNC:        │                                                  │
│    - extract facts → Semantic         │                                                  │
│    - update procedural rules          │                                                  │
│    - update mood baseline / traits    │                                                  │
│    - correlation analysis             │                                                  │
│    - confidence updates (both ways)   │                                                  │
└───────────────┬───────────────────────┘                                                  │
                │                                                                          │
================┼==========================================================================┘
                │  ALL DATA STORES  (each scoped by user_id — multi-tenant ready)
                ▼
┌───────────────────────┬───────────────────────┬──────────────────────┬─────────────────┐
│      MongoDB          │       Qdrant          │   Neo4j + Graphiti   │      Redis      │
│  (doc / relational)   │  (vector search)      │  (temporal graph)    │  (queue/cache)  │
│                       │                       │                      │                 │
│ • user_profile        │ • episodic (§5)       │ • semantic memory    │ • background    │
│ • trait_defs          │   dense+BM25+RRF      │   (§6): entities,    │   task queue    │
│ • project_types       │ • entities (§8)       │   relationships,     │   (§14)         │
│ • projects/instances  │   pointers for fuzzy  │   facts w/ validity  │ • search cache  │
│ • ledger_entries      │   resolution          │   windows            │   (§15)         │
│ • cost_ledger (§3)    │                       │                      │ • session state │
│ • procedural (§7)     │  [filtered-HNSW by    │  [supersede, don't   │                 │
│ • psych_model (§17)   │   user_id]            │   delete: history    │                 │
│ • self_model_log (§9) │                       │   preserved]         │                 │
└───────────────────────┴───────────────────────┴──────────────────────┴─────────────────┘

────────────────────────────────────────────────────────────────────────────────
CROSS-CUTTING (apply everywhere):
  • Cost Ledger logged on EVERY paid op (LLM/STT/TTS/tool/search); cache hits = $0
  • All queries filtered by user_id (multi-tenant isolation)
  • Config-over-code: behavior params in profile/registry, tunable без redeploy
  • Idle is nearly free: VAD gate blocks all paid calls during silence
  • User speaks first; pull-based disclosure; no overclaiming; never diagnose
  • Ports/adapters: core depends on interfaces, providers swappable via config
BACKLOG (not built): presence detection · custom wake words · encryption at rest ·
  external MCP integrations (registry is MCP-shaped so they slot in later)
────────────────────────────────────────────────────────────────────────────────
```