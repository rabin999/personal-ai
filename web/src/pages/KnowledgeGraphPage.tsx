import { useEffect, useMemo, useState } from "react";
import { getKnowledgeGraph, type GraphEdge, type GraphNode } from "../lib/api";
import { Loader } from "../components/States";

// The user's knowledge graph (brief U4): the connected picture of what the app knows
// about them — entities and their relationships, with temporal validity. Interactive:
// click a node to focus it (highlights its links + neighbours, lists its relationships).
export default function KnowledgeGraphPage() {
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSuperseded, setShowSuperseded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);

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

  // Circular layout of the connected nodes.
  const size = 560, r = 220, cx = size / 2, cy = size / 2;
  const { pos, list } = useMemo(() => {
    const active = new Set<string>();
    shown.forEach((e) => { if (e.source) active.add(e.source); if (e.target) active.add(e.target); });
    const list = nodes.filter((n) => active.has(n.id));
    const m = new Map<string, { x: number; y: number }>();
    list.forEach((n, i) => {
      const a = (2 * Math.PI * i) / Math.max(list.length, 1) - Math.PI / 2;
      m.set(n.id, { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) });
    });
    return { pos: m, list };
  }, [nodes, shown]);

  // Neighbours + edges of the focused (selected or hovered) node.
  const focus = selected ?? hover;
  const neighbours = useMemo(() => {
    const s = new Set<string>();
    if (!focus) return s;
    shown.forEach((e) => {
      if (e.source === focus && e.target) s.add(e.target);
      if (e.target === focus && e.source) s.add(e.source);
    });
    return s;
  }, [focus, shown]);

  const isDim = (id: string) => focus != null && id !== focus && !neighbours.has(id);
  const edgeActive = (e: GraphEdge) => focus == null || e.source === focus || e.target === focus;

  const selectedEdges = useMemo(
    () => (selected ? shown.filter((e) => e.source === selected || e.target === selected) : []),
    [selected, shown],
  );

  return (
    <section>
      <div className="mb-1 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Knowledge</h1>
        <label className="flex items-center gap-1.5 text-xs text-neutral-500">
          <input type="checkbox" checked={showSuperseded} onChange={(e) => setShowSuperseded(e.target.checked)} />
          show superseded
        </label>
      </div>
      <p className="mb-4 text-xs text-neutral-500">
        what I know about you, connected — <span className="text-neutral-400">click a node to focus it</span>
      </p>
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
          <svg
            viewBox={`0 0 ${size} ${size}`}
            className="mx-auto block h-auto w-full max-w-[600px] select-none"
            onClick={() => setSelected(null)}
          >
            {shown.map((e, i) => {
              const a = pos.get(e.source!); const b = pos.get(e.target!);
              if (!a || !b) return null;
              const active = edgeActive(e);
              const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
              return (
                <g key={i} opacity={active ? 1 : 0.12}>
                  <line x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                    stroke={e.current ? "#38bdf8" : "#f59e0b"} strokeOpacity={e.current ? 0.55 : 0.4}
                    strokeWidth={focus && active ? 2.2 : 1.4} strokeDasharray={e.current ? undefined : "4 3"} />
                  {focus && active && e.relation && (
                    <text x={mid.x} y={mid.y} textAnchor="middle" className="fill-neutral-400" fontSize={9}>
                      {e.relation.replace(/_/g, " ").toLowerCase()}
                    </text>
                  )}
                </g>
              );
            })}
            {list.map((n) => {
              const p = pos.get(n.id)!;
              const sel = selected === n.id;
              const dim = isDim(n.id);
              return (
                <g key={n.id} opacity={dim ? 0.3 : 1} className="cursor-pointer"
                  onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}
                  onClick={(ev) => { ev.stopPropagation(); setSelected(sel ? null : n.id); }}>
                  {/* larger invisible hit area */}
                  <circle cx={p.x} cy={p.y} r={16} fill="transparent" />
                  <circle cx={p.x} cy={p.y} r={sel ? 9 : 6}
                    fill={sel ? "#0284c7" : "#0ea5e9"} stroke={sel ? "#fff" : "none"} strokeWidth={2} />
                  <text x={p.x} y={p.y - 12} textAnchor="middle"
                    className={sel ? "fill-sky-600 dark:fill-sky-300 font-medium" : "fill-neutral-600 dark:fill-neutral-300"}
                    fontSize={11}>
                    {n.label.length > 22 ? n.label.slice(0, 21) + "…" : n.label}
                  </text>
                </g>
              );
            })}
          </svg>
        </div>
      )}

      {/* Focused node's relationships, or the full list. */}
      {shown.length > 0 && (
        <div className="mt-4">
          {selected && (
            <div className="mb-2 flex items-center gap-2 text-sm">
              <span className="font-medium text-sky-600 dark:text-sky-300">{selected}</span>
              <span className="text-neutral-400">· {selectedEdges.length} connection{selectedEdges.length === 1 ? "" : "s"}</span>
              <button onClick={() => setSelected(null)} className="ml-auto text-xs text-neutral-400 hover:underline">clear</button>
            </div>
          )}
          <div className="divide-y divide-neutral-200 rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
            {(selected ? selectedEdges : shown).map((e, i) => (
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
        </div>
      )}
    </section>
  );
}
