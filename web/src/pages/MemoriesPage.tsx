import { useEffect, useState } from "react";
import {
  correctSemanticFact,
  deleteEpisodicMemory,
  getEpisodicMemories,
  getPersonaMemories,
  getSemanticMemories,
  type EpisodicItem,
  type PersonaItem,
  type SemanticItem,
} from "../lib/api";
import { Loader } from "../components/States";

// The user's own memory space, in the three distinct layers (brief U0):
// facts about you (semantic, with validity), things that happened (episodic,
// timestamped/deletable), and how I've learned to talk with you (the dynamic
// persona — style/interests/sensitivities). Working memory is transient, not shown.
type Tab = "facts" | "events" | "patterns";

export default function MemoriesPage() {
  const [semantic, setSemantic] = useState<SemanticItem[]>([]);
  const [episodic, setEpisodic] = useState<EpisodicItem[]>([]);
  const [persona, setPersona] = useState<PersonaItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>("facts");

  const reloadEpisodic = () => getEpisodicMemories().then((r) => setEpisodic(r.items)).catch(() => {});

  useEffect(() => {
    setLoading(true);
    Promise.all([getSemanticMemories(), getEpisodicMemories(), getPersonaMemories()])
      .then(([s, e, p]) => { setSemantic(s.items); setEpisodic(e.items); setPersona(p.items); })
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  }, []);

  async function forget(id: string) {
    await deleteEpisodicMemory(id).catch(() => {});
    await reloadEpisodic();
  }

  const tabs: { id: Tab; label: string; count: number; sub: string }[] = [
    { id: "facts", label: "Facts about you", count: semantic.length, sub: "durable facts about you" },
    { id: "events", label: "Things that happened", count: episodic.length, sub: "timestamped things that happened" },
    { id: "patterns", label: "How I talk with you", count: persona.length, sub: "how I've learned to talk with you" },
  ];
  const active = tabs.find((t) => t.id === tab)!;

  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Memories</h1>
      {error && <p className="mb-3 text-sm text-red-600">{error}</p>}

      {/* Tabs by memory type */}
      <div className="mb-1 flex gap-1 overflow-x-auto border-b border-neutral-200 dark:border-neutral-800">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`-mb-px flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
              tab === t.id
                ? "border-sky-500 text-sky-600 dark:text-sky-400"
                : "border-transparent text-neutral-500 hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
            }`}
          >
            {t.label}
            <span className={`rounded-full px-1.5 text-xs ${tab === t.id ? "bg-sky-100 text-sky-700 dark:bg-sky-900/50 dark:text-sky-300" : "bg-neutral-100 text-neutral-500 dark:bg-neutral-800"}`}>{t.count}</span>
          </button>
        ))}
      </div>
      <p className="mb-4 mt-2 text-xs text-neutral-500">{active.sub}</p>

      {loading && <Loader label="Loading memories…" />}

      {!loading && tab === "facts" && (
        <div className="divide-y divide-neutral-200 rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
          {semantic.length === 0 && <Empty />}
          {semantic.map((f, i) => (
            <Row key={i}>
              <span>{f.fact}</span>
              {f.valid_to && <Tag>superseded {f.valid_to}</Tag>}
            </Row>
          ))}
          <CorrectFact onDone={() => getSemanticMemories().then((r) => setSemantic(r.items)).catch(() => {})} />
        </div>
      )}

      {!loading && tab === "events" && (
        <div className="divide-y divide-neutral-200 rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
          {episodic.length === 0 && <Empty />}
          {episodic.map((e) => (
            <Row key={e.id}>
              <span>
                {e.text}
                {e.timestamp && <span className="ml-2 text-xs text-neutral-400">{e.timestamp}</span>}
              </span>
              <button onClick={() => void forget(e.id)} className="shrink-0 text-xs text-red-500 hover:underline" title="Forget this">
                Forget
              </button>
            </Row>
          ))}
        </div>
      )}

      {!loading && tab === "patterns" && (
        <div className="divide-y divide-neutral-200 rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
          {persona.length === 0 && <Empty />}
          {persona.map((p) => (
            <Row key={p.id}>
              <span className={p.active ? "" : "text-neutral-400"}>
                {p.text}
                {!p.active && <span className="ml-2 text-xs text-neutral-400">(learning)</span>}
              </span>
              <Tag>{p.kind}</Tag>
            </Row>
          ))}
        </div>
      )}
    </section>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return <div className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm">{children}</div>;
}

function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span className="shrink-0 rounded-full bg-neutral-100 px-2 py-0.5 text-xs text-neutral-500 dark:bg-neutral-800">
      {children}
    </span>
  );
}

function Empty() {
  return <div className="px-4 py-2.5 text-sm text-neutral-400">Nothing here yet.</div>;
}

// "That's wrong — it's actually…": record the correction. Graphiti supersedes the
// contradicted fact (never deletes it).
function CorrectFact({ onDone }: { onDone: () => void }) {
  const [fact, setFact] = useState("");
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        if (!fact.trim()) return;
        await correctSemanticFact(fact.trim()).catch(() => {});
        setFact("");
        onDone();
      }}
      className="flex items-center gap-2 px-4 py-2.5"
    >
      <input
        value={fact}
        onChange={(e) => setFact(e.target.value)}
        placeholder="correct or add a fact — e.g. 'I moved to Boston'"
        className="flex-1 rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
      />
      <button type="submit" className="text-xs text-neutral-500 underline">
        save
      </button>
    </form>
  );
}
