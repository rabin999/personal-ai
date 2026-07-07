import { useEffect, useState } from "react";
import {
  deleteEpisodicMemory,
  getEpisodicMemories,
  getProceduralMemories,
  getSemanticMemories,
  type EpisodicItem,
  type ProceduralItem,
  type SemanticItem,
} from "../lib/api";

// The user's own memory space, grouped by the supported types (no invented types):
// semantic facts (with validity), episodic events (timestamped, deletable),
// procedural rules (with confidence). Working memory is transient and not shown.
export default function MemoriesPage() {
  const [semantic, setSemantic] = useState<SemanticItem[]>([]);
  const [episodic, setEpisodic] = useState<EpisodicItem[]>([]);
  const [procedural, setProcedural] = useState<ProceduralItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const reloadEpisodic = () => getEpisodicMemories().then((r) => setEpisodic(r.items)).catch(() => {});

  useEffect(() => {
    Promise.all([getSemanticMemories(), getEpisodicMemories(), getProceduralMemories()])
      .then(([s, e, p]) => { setSemantic(s.items); setEpisodic(e.items); setProcedural(p.items); })
      .catch((err) => setError(String(err)));
  }, []);

  async function forget(id: string) {
    await deleteEpisodicMemory(id).catch(() => {});
    await reloadEpisodic();
  }

  return (
    <section className="space-y-6">
      <h1 className="text-xl font-semibold">Memories</h1>
      {error && <p className="text-sm text-red-600">{error}</p>}

      <Group title="Facts about you" subtitle="semantic — durable, distilled">
        {semantic.length === 0 && <Empty />}
        {semantic.map((f, i) => (
          <Row key={i}>
            <span>{f.fact}</span>
            {f.valid_to && <Tag>superseded {f.valid_to}</Tag>}
          </Row>
        ))}
      </Group>

      <Group title="Things that happened" subtitle="episodic — timestamped events">
        {episodic.length === 0 && <Empty />}
        {episodic.map((e) => (
          <Row key={e.id}>
            <span>
              {e.text}
              {e.timestamp && <span className="ml-2 text-xs text-neutral-400">{e.timestamp}</span>}
            </span>
            <button
              onClick={() => void forget(e.id)}
              className="text-xs text-red-500 hover:underline"
              title="Forget this"
            >
              Forget
            </button>
          </Row>
        ))}
      </Group>

      <Group title="How I've learned to talk with you" subtitle="procedural — learned patterns">
        {procedural.length === 0 && <Empty />}
        {procedural.map((p) => (
          <Row key={p.id}>
            <span>{p.rule}</span>
            <Tag>confidence {(p.confidence * 100).toFixed(0)}%</Tag>
          </Row>
        ))}
      </Group>
    </section>
  );
}

function Group({ title, subtitle, children }: {
  title: string; subtitle: string; children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-2">
        <h2 className="font-medium">{title}</h2>
        <p className="text-xs text-neutral-500">{subtitle}</p>
      </div>
      <div className="divide-y divide-neutral-200 rounded-lg border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
        {children}
      </div>
    </div>
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
