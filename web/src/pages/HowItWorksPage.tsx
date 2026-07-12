import { useTheme } from "../lib/theme";
import { ThemeToggle } from "../components/ThemeToggle";
import { AsaathiMark } from "../components/AuthPage";

// URL-only explainer (/how-it-works): a visual walk-through of the app's architecture
// and the per-turn loop. Intentionally NOT linked from anywhere in the app nav.

// The per-turn pipeline, in order. Each is one stage of a single conversational turn.
const STAGES: { emoji: string; title: string; body: string }[] = [
  { emoji: "🎙️", title: "You speak", body: "You just talk — no push-to-talk. The mic streams continuously." },
  { emoji: "🔊", title: "Voice activity gate", body: "Silence is detected and stays free — no paid work runs while you're not speaking." },
  { emoji: "📝", title: "Speech → text", body: "Your words are transcribed, and endpointing decides the moment you actually finished a thought." },
  { emoji: "🧩", title: "Assemble context", body: "Before reasoning, it READS your memory — recent turns, facts, people, preferences — and builds the prompt." },
  { emoji: "🧠", title: "Think (ReAct)", body: "It reasons about what you meant, decides if it needs memory or a web search, acts, and observes — then answers." },
  { emoji: "🪞", title: "Self-reflect", body: "It critiques its own draft — warm, human, honest, on-topic — and revises before a single word is spoken." },
  { emoji: "🗣️", title: "Speak the reply", body: "The answer is streamed back as voice in small chunks — a quick reaction first, then the full reply." },
  { emoji: "💾", title: "Remember (after)", body: "Off your conversation, it extracts what's worth keeping and consolidates it into long-term memory." },
];

// The stores/knowledge the reasoning core reads before a turn and writes after it.
const STORES: { name: string; tech: string; body: string }[] = [
  { name: "Working memory", tech: "Redis", body: "The live thread of the current conversation." },
  { name: "Episodic memory", tech: "Qdrant", body: "Things that happened, searchable by meaning." },
  { name: "Semantic graph", tech: "Neo4j · Graphiti", body: "Durable facts about you and how they connect." },
  { name: "Personalization", tech: "Mem0", body: "Your preferences and how you like to be talked to." },
  { name: "Documents", tech: "MongoDB", body: "Profiles, conversations, projects, and the cost ledger." },
];

const PRINCIPLES: { title: string; body: string }[] = [
  { title: "Thinks before it replies", body: "Every turn reasons first — it's not a reflexive one-shot generation." },
  { title: "Reflects on itself", body: "It judges its own draft against a warm, human standard and fixes it before speaking." },
  { title: "Remembers properly", body: "It decides what's worth storing and where, and never re-saves something it's only recalling." },
  { title: "Private & isolated", body: "Every read and write is scoped to you alone — and you can wipe everything anytime." },
  { title: "Fast, in chunks", body: "A quick reaction lands in a few seconds; the full answer streams right after." },
  { title: "Honest", body: "It looks things up when it should, and says plainly when it doesn't know." },
];

export default function HowItWorksPage() {
  const { pref, setPref } = useTheme();
  return (
    <div className="min-h-[100dvh] bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      {/* Top bar */}
      <header className="sticky top-0 z-10 border-b border-slate-200/70 bg-slate-50/80 backdrop-blur-md dark:border-slate-800/70 dark:bg-slate-950/70">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-5 py-4 sm:px-8">
          <div className="flex items-center gap-2.5">
            <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-sky-500 to-cyan-500 text-white shadow-md shadow-sky-600/25">
              <AsaathiMark className="h-5 w-5" />
            </div>
            <span className="text-sm font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
              Asaathi
            </span>
          </div>
          <ThemeToggle pref={pref} onChange={setPref} />
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-5 pb-24 pt-12 sm:px-8 sm:pt-16">
        {/* Hero */}
        <div className="relative overflow-hidden">
          <div className="pointer-events-none absolute -left-20 -top-24 h-80 w-80 rounded-full bg-sky-400/15 blur-3xl" />
          <p className="relative text-sm font-semibold uppercase tracking-[0.16em] text-sky-600 dark:text-sky-400">
            How it works
          </p>
          <h1 className="relative mt-3 max-w-3xl text-4xl font-bold leading-[1.1] tracking-tight sm:text-5xl">
            A companion that thinks, remembers, and{" "}
            <span className="bg-gradient-to-r from-sky-500 to-cyan-500 bg-clip-text text-transparent">
              sounds human
            </span>
            .
          </h1>
          <p className="relative mt-5 max-w-2xl text-lg leading-relaxed text-slate-600 dark:text-slate-300">
            Asaathi isn't a chatbot that fires back the first thing it generates. Every time
            you speak, it runs a real loop — perceive, recall, reason, reflect, then reply —
            and quietly learns from the conversation afterward. Here's the whole picture.
          </p>
        </div>

        {/* High-level architecture — three layers */}
        <section className="mt-14">
          <SectionTitle n="01" title="The big picture" />
          <div className="mt-6 grid gap-4 md:grid-cols-3">
            <LayerCard
              tint="from-sky-500 to-cyan-500"
              label="Voice runtime"
              items={["Continuous listening", "Voice-activity gate", "Speech-to-text + endpointing", "Barge-in / interrupt", "Streaming text-to-speech"]}
            />
            <LayerCard
              tint="from-violet-500 to-fuchsia-500"
              label="Reasoning core"
              items={["Context assembly", "ReAct reasoning loop", "Tool use (web search)", "Self-reflection & revision", "Response standard + safety"]}
            />
            <LayerCard
              tint="from-emerald-500 to-teal-500"
              label="Memory & knowledge"
              items={["Working / episodic / semantic", "Personalization layer", "Per-user isolation", "Background consolidation", "Full per-turn tracing"]}
            />
          </div>
          <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">
            The voice runtime handles the real-time conversation; the reasoning core decides
            what to say; memory makes it personal — and everything money-costing is logged and
            never blocks your reply.
          </p>
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

        {/* Principles */}
        <section className="mt-16">
          <SectionTitle n="04" title="What makes it different" />
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

        {/* CTA */}
        <section className="mt-16 overflow-hidden rounded-3xl border border-slate-200/80 bg-gradient-to-br from-sky-500 to-cyan-500 p-8 text-center text-white shadow-xl shadow-sky-600/20 sm:p-10 dark:border-slate-800">
          <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">Ready to just talk?</h2>
          <p className="mx-auto mt-2 max-w-md text-white/90">
            Sign in and start a conversation — your companion remembers you from the first hello.
          </p>
          <a
            href="/login"
            className="mt-6 inline-flex items-center justify-center gap-2 rounded-2xl bg-white px-6 py-3.5 text-base font-semibold text-sky-700 shadow-lg transition-transform hover:scale-[1.02] active:scale-[0.99]"
          >
            Start talking
          </a>
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

function LayerCard({ tint, label, items }: { tint: string; label: string; items: string[] }) {
  return (
    <div className="rounded-2xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-900/50">
      <div className={`mb-4 inline-flex rounded-lg bg-gradient-to-br ${tint} px-3 py-1 text-sm font-semibold text-white shadow-sm`}>
        {label}
      </div>
      <ul className="space-y-2">
        {items.map((it) => (
          <li key={it} className="flex items-start gap-2 text-sm text-slate-600 dark:text-slate-300">
            <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-sky-400" />
            <span>{it}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
