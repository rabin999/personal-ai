import { useEffect, useMemo, useState } from "react";
import { getKnowledgeGraph, type GraphEdge, type GraphNode } from "../lib/api";
import { Loader } from "../components/States";

// The user's knowledge graph (brief U4): the connected picture of what the app knows
// about them — entities (people, places, holdings, routines) and their relationships,
// with temporal validity (current vs. superseded). Read-only; from Graphiti/Neo4j.
export default function KnowledgeGraphPage() {
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSuperseded, setShowSuperseded] = useState(false);

  useEffect(() => {
    getKnowledgeGraph()
      .then((r) => { setNodes(r.nodes); setEdges(r.edges); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const shown = useMemo(
    () => edges.filter((e) => (showSuperseded ? true : e.current) && e.source && e.target),
    [edges, showSuperseded],
  );

  // Circular layout: place each node on a circle; draw edges as lines between them.
  const size = 520, r = 210, cx = size / 2, cy = size / 2;
  const pos = useMemo(() => {
    const active = new Set<string>();
    shown.forEach((e) => { if (e.source) active.add(e.source); if (e.target) active.add(e.target); });
    const list = nodes.filter((n) => active.has(n.id));
    const m = new Map<string, { x: number; y: number }>();
    list.forEach((n, i) => {
      const a = (2 * Math.PI * i) / Math.max(list.length, 1) - Math.PI / 2;
      m.set(n.id, { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) });
    });
    return m;
  }, [nodes, shown]);

  return (
    <section>
      <div className="mb-1 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Knowledge</h1>
        <label className="flex items-center gap-1.5 text-xs text-neutral-500">
          <input type="checkbox" checked={showSuperseded} onChange={(e) => setShowSuperseded(e.target.checked)} />
          show superseded
        </label>
      </div>
      <p className="mb-4 text-xs text-neutral-500">what I know about you, connected</p>
      {error && <p className="mb-3 text-sm text-red-600">{error}</p>}
      {loading && <Loader label="Loading graph…" />}
      {!loading && shown.length === 0 && (
        <div className="rounded-xl border border-neutral-200 px-4 py-6 text-sm text-neutral-400 dark:border-neutral-800">
          Nothing connected yet. As we talk and I learn facts about you, the people, places, and things
          in your life show up here linked together.
        </div>
      )}

      {shown.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-neutral-200 dark:border-neutral-800">
          <svg viewBox={`0 0 ${size} ${size}`} className="mx-auto block h-auto w-full max-w-[540px]">
            {shown.map((e, i) => {
              const a = pos.get(e.source!); const b = pos.get(e.target!);
              if (!a || !b) return null;
              return (
                <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                  stroke={e.current ? "#38bdf8" : "#f59e0b"} strokeOpacity={e.current ? 0.5 : 0.35}
                  strokeWidth={1.5} strokeDasharray={e.current ? undefined : "4 3"} />
              );
            })}
            {[...pos.entries()].map(([id, p]) => (
              <g key={id}>
                <circle cx={p.x} cy={p.y} r={6} fill="#0ea5e9" />
                <text x={p.x} y={p.y - 10} textAnchor="middle" className="fill-neutral-600 dark:fill-neutral-300" fontSize={11}>
                  {id.length > 22 ? id.slice(0, 21) + "…" : id}
                </text>
              </g>
            ))}
          </svg>
        </div>
      )}

      {/* Relationship list — the same facts, readable, with validity. */}
      {shown.length > 0 && (
        <div className="mt-4 divide-y divide-neutral-200 rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
          {shown.map((e, i) => (
            <div key={i} className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm">
              <span className={e.current ? "" : "text-neutral-400 line-through decoration-neutral-400/50"}>
                {e.fact}
              </span>
              {!e.current && (
                <span className="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                  superseded
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
