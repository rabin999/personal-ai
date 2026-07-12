import { useTheme } from "../lib/theme";
import { ThemeToggle } from "../components/ThemeToggle";
import { AsaathiMark } from "../components/AuthPage";
import { Mermaid } from "../components/Mermaid";

// URL-only explainer (/how-it-works): a visual walk-through of the app's architecture,
// the per-turn loop, and how it's evaluated. Intentionally NOT linked from the app nav.

// One comprehensive architecture diagram (Mermaid). Latest flowchart syntax.
const ARCHITECTURE = `flowchart LR
  U([🎙️ You speak]):::io

  subgraph EDGE["API edge · FastAPI"]
    direction TB
    WS["WebSocket /ws/voice"]
    AUTH["Google SSO → user_id"]
  end

  subgraph VOICE["Voice runtime · Pipecat"]
    direction TB
    VAD["VAD gate — idle is free"]
    STT["STT + endpointing"]
    TTS["Streaming TTS + barge-in"]
  end

  subgraph CORE["Reasoning core · every turn"]
    direction TB
    ASM["Assemble context"]
    REACT["Think — ReAct loop"]
    REF["Self-reflect & revise"]
    ENF["Response standard + safety"]
  end

  subgraph MEM["Memory & knowledge"]
    direction LR
    WORK[("Working · Redis")]
    EPI[("Episodic · Qdrant")]
    SEM[("Semantic graph · Neo4j + Graphiti")]
    PERS[("Personalization · Mem0")]
    DOC[("Documents · Mongo")]
  end

  subgraph PROV["Tools & providers"]
    direction LR
    LLM["LLM · OpenRouter"]
    SEARCH["Web search · Serper + Brave"]
  end

  subgraph BG["Background workers (async)"]
    direction LR
    EXT["Extraction"]
    CON["Consolidation"]
    LEDGER["Cost ledger + tracing"]
  end

  U --> WS --> VAD --> STT --> ASM
  AUTH -. user_id .-> CORE
  ASM -- reads --> MEM
  ASM --> REACT --> REF --> ENF --> TTS --> OUT([🔊 You hear the reply]):::io
  REACT -- tool --> SEARCH
  REACT --> LLM
  REF --> LLM
  ENF -. after reply .-> BG
  BG -- writes --> MEM
  EXT --> CON

  classDef io fill:#0ea5e9,stroke:#0284c7,color:#ffffff,font-weight:bold;
`;

// The per-turn pipeline, in order.
const STAGES: { emoji: string; title: string; body: string }[] = [
  { emoji: "🎙️", title: "You speak", body: "You just talk — no push-to-talk. The mic streams continuously." },
  { emoji: "🔊", title: "Voice activity gate", body: "Silence is detected and stays free — no paid work runs while you're not speaking." },
  { emoji: "📝", title: "Speech → text", body: "Your words are transcribed, and endpointing decides the moment you actually finished a thought." },
  { emoji: "🧩", title: "Assemble context", body: "Before reasoning, it READS your memory — recent turns, facts, people, preferences — and builds the prompt." },
  { emoji: "🧠", title: "Think (ReAct)", body: "It reasons about what you meant, decides if it needs memory or a web search, acts, and observes — then answers." },
  { emoji: "🪞", title: "Self-reflect", body: "It critiques its own draft — warm, human, honest, on-topic — and revises before a single word is spoken." },
  { emoji: "🗣️", title: "Speak the reply", body: "The answer is streamed back as voice in small chunks — a quick reaction first, then the full reply. If a lookup runs long, it keeps you in the loop instead of going quiet." },
  { emoji: "💾", title: "Remember (after)", body: "Off your conversation, it extracts what's worth keeping and consolidates it into long-term memory." },
];

const STORES: { name: string; tech: string; body: string }[] = [
  { name: "Working memory", tech: "Redis", body: "The live thread of the current conversation." },
  { name: "Episodic memory", tech: "Qdrant", body: "Things that happened, searchable by meaning." },
  { name: "Semantic graph", tech: "Neo4j · Graphiti", body: "Durable facts about you and how they connect." },
  { name: "Personalization", tech: "Mem0", body: "Your preferences and how you like to be talked to." },
  { name: "Documents", tech: "MongoDB", body: "Profiles, conversations, projects, and the cost ledger." },
];

// How the app is evaluated — the concrete metrics behind each check.
const METRICS: { title: string; how: string; body: string; metrics: string[] }[] = [
  {
    title: "Response quality",
    how: "LLM-as-judge",
    body: "A separate, pinned model scores every reply against the response standard — a companion, not an assistant.",
    metrics: ["Warmth (1–5)", "Human-ness (1–5)", "Brevity (1–5)", "Companion-fit (1–5)", "Overall (pass ≥ 4/5)"],
  },
  {
    title: "Hard rules",
    how: "Deterministic · pass/fail",
    body: "Absolute rules a human can spot in one read, checked automatically on every reply.",
    metrics: ["No assistant-speak", "Disclosure never proactive", "No duplicated content", "Self-reflection ran", "No unkeepable promise"],
  },
  {
    title: "Retrieval quality",
    how: "RAGAS",
    body: "Whether the answer is grounded in the memory and search results it was given.",
    metrics: ["Faithfulness", "Answer relevancy", "Context precision", "Context recall"],
  },
  {
    title: "Multi-tenant isolation",
    how: "Two-user tests",
    body: "One person's data must never surface in another's context — verified, not assumed.",
    metrics: ["Cross-user leakage = 0", "user_id-scoped reads", "user_id-scoped writes", "No double-write on recall"],
  },
  {
    title: "Latency",
    how: "Per-turn timing",
    body: "Voice can't wait — the first spoken chunk must land fast, measured every turn.",
    metrics: ["Time-to-first-audio ≤ ~5s", "First-chunk p50 / p95", "Per-LLM-call latency", "End-to-end turn time"],
  },
  {
    title: "Cost & tracing",
    how: "Cost ledger + spans",
    body: "Every paid call is logged; each turn carries a complete, inspectable trace.",
    metrics: ["Cost per turn (USD)", "Input / output tokens", "Cache-hit rate", "Per-turn span coverage"],
  },
];

const PRINCIPLES: { title: string; body: string }[] = [
  { title: "Thinks before it replies", body: "Every turn reasons first — it's not a reflexive one-shot generation." },
  { title: "Reflects on itself", body: "It judges its own draft against a warm, human standard and fixes it before speaking." },
  { title: "Remembers properly", body: "It decides what's worth storing and where, and never re-saves something it's only recalling." },
  { title: "Private & isolated", body: "Every read and write is scoped to you alone — and you can wipe everything anytime." },
  { title: "Fast, in chunks", body: "A quick reaction lands in a few seconds; the full answer streams right after — and it never leaves you in silence while it works." },
  { title: "Honest", body: "It looks things up when it should, and says plainly when it doesn't know." },
];

export default function HowItWorksPage() {
  const { pref, resolved, setPref } = useTheme();
  return (
    <div className="min-h-[100dvh] overflow-x-hidden bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      {/* Top bar */}
      <header className="sticky top-0 z-10 border-b border-slate-200/70 bg-slate-50/80 backdrop-blur-md dark:border-slate-800/70 dark:bg-slate-950/70">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-5 py-4 sm:px-8">
          <a href="/login" className="flex items-center gap-2.5" aria-label="Back to Asaathi">
            <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-sky-500 to-cyan-500 text-white shadow-md shadow-sky-600/25">
              <AsaathiMark className="h-5 w-5" />
            </div>
            <span className="text-sm font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
              Asaathi
            </span>
          </a>
          <ThemeToggle pref={pref} onChange={setPref} />
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-5 pb-24 pt-10 sm:px-8 sm:pt-14">
        {/* Concise hero (the marketing intro lives on the sign-in page). */}
        <p className="text-sm font-semibold uppercase tracking-[0.16em] text-sky-600 dark:text-sky-400">
          How it works
        </p>
        <h1 className="mt-3 max-w-3xl text-3xl font-bold leading-[1.12] tracking-tight sm:text-4xl">
          The architecture behind the conversation
        </h1>
        <p className="mt-4 max-w-2xl text-lg leading-relaxed text-slate-600 dark:text-slate-300">
          Every time you speak, Asaathi runs a real loop — perceive, recall, reason, reflect,
          then reply — and learns from the conversation afterward. Here's the whole system.
        </p>

        {/* Architecture diagram — full-bleed so it uses the whole page width */}
        <section className="relative left-1/2 mt-12 w-screen -translate-x-1/2 px-5 sm:px-8">
          <div className="mx-auto max-w-[1600px]">
            <SectionTitle n="01" title="System architecture" />
            <div className="mt-6 rounded-3xl border border-slate-200/80 bg-white/70 p-4 backdrop-blur-sm sm:p-6 dark:border-slate-800 dark:bg-slate-900/50">
              <Mermaid chart={ARCHITECTURE} dark={resolved === "dark"} />
            </div>
            <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">
              The voice runtime handles the real-time conversation; the reasoning core decides
              what to say; memory makes it personal. Everything money-costing is logged and never
              blocks your reply — slow work goes to background workers.
            </p>
          </div>
        </section>

        {/* Per-turn timeline */}
        <section className="mt-16">
          <SectionTitle n="02" title="A single turn, end to end" />
          <ol className="relative mt-8 space-y-6 before:absolute before:left-[19px] before:top-2 before:h-[calc(100%-1rem)] before:w-px before:bg-gradient-to-b before:from-sky-400/60 before:to-cyan-400/30 sm:before:left-[23px]">
            {STAGES.map((s, i) => (
              <li key={s.title} className="relative flex gap-4 sm:gap-5">
                <div className="z-[1] grid h-10 w-10 shrink-0 place-items-center rounded-full border border-slate-200 bg-white text-lg shadow-sm sm:h-12 sm:w-12 dark:border-slate-700 dark:bg-slate-900">
                  <span aria-hidden>{s.emoji}</span>
                </div>
                <div className="min-w-0 flex-1 rounded-2xl border border-slate-200/80 bg-white/70 p-4 backdrop-blur-sm sm:p-5 dark:border-slate-800 dark:bg-slate-900/50">
                  <div className="flex items-baseline gap-2">
                    <span className="text-xs font-semibold text-sky-500 dark:text-sky-400">
                      Step {i + 1}
                    </span>
                    <h3 className="text-base font-semibold">{s.title}</h3>
                  </div>
                  <p className="mt-1 text-[15px] leading-relaxed text-slate-500 dark:text-slate-400">
                    {s.body}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        </section>

        {/* Memory stores */}
        <section className="mt-16">
          <SectionTitle n="03" title="Memory it draws on" />
          <p className="mt-3 max-w-2xl text-slate-600 dark:text-slate-300">
            Before it reasons, the core READS from these; after it replies, it WRITES back the
            parts worth keeping. Each layer holds a different kind of memory.
          </p>
          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {STORES.map((s) => (
              <div
                key={s.name}
                className="rounded-2xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-900/50"
              >
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-base font-semibold">{s.name}</h3>
                  <span className="rounded-full bg-sky-500/10 px-2.5 py-1 text-[11px] font-semibold text-sky-600 dark:bg-sky-400/10 dark:text-sky-400">
                    {s.tech}
                  </span>
                </div>
                <p className="mt-2 text-[15px] leading-relaxed text-slate-500 dark:text-slate-400">
                  {s.body}
                </p>
              </div>
            ))}
          </div>
        </section>

        {/* Evaluation & metrics */}
        <section className="mt-16">
          <SectionTitle n="04" title="How we evaluate it" />
          <p className="mt-3 max-w-2xl text-slate-600 dark:text-slate-300">
            A companion is only as good as its replies, so quality is measured — not assumed.
            Every behavioral requirement is checked automatically against these metrics.
          </p>
          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {METRICS.map((m) => (
              <div
                key={m.title}
                className="rounded-2xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-900/50"
              >
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-base font-semibold">{m.title}</h3>
                  <span className="shrink-0 rounded-full bg-cyan-500/10 px-2.5 py-1 text-[11px] font-semibold text-cyan-600 dark:bg-cyan-400/10 dark:text-cyan-400">
                    {m.how}
                  </span>
                </div>
                <p className="mt-2 text-[15px] leading-relaxed text-slate-500 dark:text-slate-400">
                  {m.body}
                </p>
                <ul className="mt-3 flex flex-wrap gap-1.5">
                  {m.metrics.map((name) => (
                    <li
                      key={name}
                      className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-[12px] font-medium text-slate-600 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-300"
                    >
                      {name}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </section>

        {/* Principles */}
        <section className="mt-16">
          <SectionTitle n="05" title="What makes it different" />
          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {PRINCIPLES.map((p) => (
              <div
                key={p.title}
                className="rounded-2xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-900/50"
              >
                <h3 className="text-base font-semibold">{p.title}</h3>
                <p className="mt-1.5 text-[15px] leading-relaxed text-slate-500 dark:text-slate-400">
                  {p.body}
                </p>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}

function SectionTitle({ n, title }: { n: string; title: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="text-sm font-bold tabular-nums text-sky-500/80 dark:text-sky-400/80">{n}</span>
      <h2 className="text-xl font-semibold tracking-tight sm:text-2xl">{title}</h2>
      <span className="h-px flex-1 bg-slate-200 dark:bg-slate-800" />
    </div>
  );
}
