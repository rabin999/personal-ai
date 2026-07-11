import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, {
  type ForceGraphMethods,
  type NodeObject,
  type LinkObject,
} from "react-force-graph-2d";
import { getKnowledgeGraph, type GraphEdge, type GraphNode } from "../lib/api";
import { Loader } from "../components/States";

// The user's knowledge graph (brief U4): the connected picture of what the app knows
// about them — entities and their relationships, with temporal validity. Rendered as a
// real force-directed graph (react-force-graph-2d, canvas) so nodes spread out instead
// of piling onto one ring: zoom, pan, drag a node, click to focus, filter by relation and
// by current/superseded, and search to highlight. Facts are listed in a side panel.

// ── graph model ─────────────────────────────────────────────────────────────
// Node/link shapes the canvas engine mutates in place (adds x/y/vx/vy). We keep our own
// fields on top of NodeObject/LinkObject so accessors stay typed.
type GNode = NodeObject<{ id: string; label: string }>;
type GLink = LinkObject<
  { id: string; label: string },
  { fact: string; relation: string | null; current: boolean }
>;

// Resolve either end of a link to a node id (post-simulation the engine replaces the
// string id with the resolved node object).
function endId(end: string | number | GNode | undefined): string | undefined {
  if (end == null) return undefined;
  if (typeof end === "object") return String(end.id);
  return String(end);
}

// ── theme ─────────────────────────────────────────────────────────────────
// The app stamps the resolved theme onto <html data-theme> (see lib/theme.ts). The canvas
// paints in raw colors, not CSS classes, so we read that attribute and re-colour live when
// the user toggles the theme — without instantiating a second theme controller.
function useResolvedTheme(): "light" | "dark" {
  const read = (): "light" | "dark" =>
    document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  const [theme, setTheme] = useState<"light" | "dark">(read);
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(read()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  return theme;
}

interface Palette {
  node: string;
  nodeSelected: string;
  label: string;
  labelMuted: string;
  current: string;
  superseded: string;
  ring: string;
  dim: number; // opacity for de-emphasised elements
}
function palette(theme: "light" | "dark"): Palette {
  return theme === "dark"
    ? {
        node: "#38bdf8",
        nodeSelected: "#7dd3fc",
        label: "#e5e5e5",
        labelMuted: "#a3a3a3",
        current: "#38bdf8",
        superseded: "#f59e0b",
        ring: "#fbbf24",
        dim: 0.12,
      }
    : {
        node: "#0ea5e9",
        nodeSelected: "#0284c7",
        label: "#404040",
        labelMuted: "#737373",
        current: "#0284c7",
        superseded: "#d97706",
        ring: "#d97706",
        dim: 0.1,
      };
}

// ── container width ──────────────────────────────────────────────────────────
// A callback ref (not useEffect) so it attaches the observer the moment the box mounts —
// the box is rendered conditionally (only after loading), so a plain effect with []
// deps would run while the node is still absent and never measure it.
function useMeasure<T extends HTMLElement>() {
  const [width, setWidth] = useState(0);
  const roRef = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: T | null) => {
    roRef.current?.disconnect();
    if (!el) return;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) setWidth(e.contentRect.width);
    });
    ro.observe(el);
    roRef.current = ro;
  }, []);
  return { ref, width };
}

export default function KnowledgeGraphPage() {
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Controls.
  const [showSuperseded, setShowSuperseded] = useState(false);
  const [relation, setRelation] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);

  const theme = useResolvedTheme();
  const colors = useMemo(() => palette(theme), [theme]);
  const { ref: boxRef, width } = useMeasure<HTMLDivElement>();
  const graphRef = useRef<ForceGraphMethods<GNode, GLink> | undefined>(undefined);
  const height = 460;

  useEffect(() => {
    getKnowledgeGraph()
      .then((r) => {
        setNodes(r.nodes);
        setEdges(r.edges);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  // Distinct relations for the filter dropdown (over currently-visible-by-superseded edges).
  const relations = useMemo(() => {
    const s = new Set<string>();
    edges.forEach((e) => {
      if ((showSuperseded || e.current) && e.relation) s.add(e.relation);
    });
    return [...s].sort();
  }, [edges, showSuperseded]);

  // Edges that pass the superseded + relation filters and have both endpoints.
  const shown = useMemo(
    () =>
      edges.filter(
        (e) =>
          (showSuperseded ? true : e.current) &&
          e.source &&
          e.target &&
          (relation === "all" || e.relation === relation),
      ),
    [edges, showSuperseded, relation],
  );

  // Graph data for the engine. Referentially stable across focus/hover/search changes
  // (those only affect paint), so the simulation isn't reset and node positions persist.
  const graphData = useMemo(() => {
    const active = new Set<string>();
    shown.forEach((e) => {
      if (e.source) active.add(e.source);
      if (e.target) active.add(e.target);
    });
    const gnodes: GNode[] = nodes
      .filter((n) => active.has(n.id))
      .map((n) => ({ id: n.id, label: n.label }));
    const links: GLink[] = shown.map((e) => ({
      source: e.source as string,
      target: e.target as string,
      fact: e.fact,
      relation: e.relation,
      current: e.current,
    }));
    return { nodes: gnodes, links };
    // Only rebuild (and reset the simulation) when the visible set truly changes.
  }, [nodes, shown]);

  // Focus = selected (sticky) or hovered (transient). Its neighbours stay lit.
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

  // Search matches (case-insensitive substring on the label). Empty query = no matching.
  const q = query.trim().toLowerCase();
  const matches = useMemo(() => {
    const s = new Set<string>();
    if (!q) return s;
    graphData.nodes.forEach((n) => {
      if (String(n.label).toLowerCase().includes(q)) s.add(String(n.id));
    });
    return s;
  }, [q, graphData]);

  // Facts for the side panel: the selected node's edges, else all shown edges.
  const selectedEdges = useMemo(
    () => (selected ? shown.filter((e) => e.source === selected || e.target === selected) : []),
    [selected, shown],
  );
  const selectedLabel = useMemo(
    () => nodes.find((n) => n.id === selected)?.label ?? selected,
    [nodes, selected],
  );

  // ── canvas paint ───────────────────────────────────────────────────────────
  const paintNode = useCallback(
    (node: GNode, ctx: CanvasRenderingContext2D, scale: number) => {
      const id = String(node.id);
      const isSel = id === selected;
      const isFocus = id === focus;
      const lit = !focus || isFocus || neighbours.has(id);
      const isMatch = matches.has(id);
      const searching = q.length > 0;
      const dim = (!!focus && !lit) || (searching && !isMatch);
      const alpha = dim ? colors.dim : 1;

      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const r = isSel ? 6 : 4.5;

      ctx.globalAlpha = alpha;
      // Search / selection ring.
      if (isMatch || isSel) {
        ctx.beginPath();
        ctx.arc(x, y, r + 3, 0, 2 * Math.PI);
        ctx.strokeStyle = isMatch ? colors.ring : colors.nodeSelected;
        ctx.lineWidth = 1.5 / scale;
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = isSel ? colors.nodeSelected : colors.node;
      ctx.fill();

      // Label — screen-constant size so it stays legible at any zoom; hidden only when
      // zoomed far out and not otherwise highlighted (keeps the field readable, not noisy).
      const showLabel = scale > 0.55 || isFocus || isSel || isMatch;
      if (showLabel) {
        const label = String(node.label);
        const text = label.length > 26 ? label.slice(0, 25) + "…" : label;
        const fontSize = Math.max(11 / scale, 3);
        ctx.font = `${isSel || isFocus ? 600 : 400} ${fontSize}px "Plus Jakarta Sans Variable", sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle =
          isSel || isFocus ? colors.nodeSelected : dim ? colors.labelMuted : colors.label;
        ctx.fillText(text, x, y + r + 2 / scale);
      }
      ctx.globalAlpha = 1;
    },
    [selected, focus, neighbours, matches, q, colors],
  );

  // Hit area for pointer interaction (must cover the same disc we paint).
  const paintNodePointer = useCallback(
    (node: GNode, color: string, ctx: CanvasRenderingContext2D) => {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(node.x ?? 0, node.y ?? 0, 7, 0, 2 * Math.PI);
      ctx.fill();
    },
    [],
  );

  const linkColor = useCallback(
    (link: GLink) => {
      const s = endId(link.source);
      const t = endId(link.target);
      const touches = !focus || s === focus || t === focus;
      const base = link.current ? colors.current : colors.superseded;
      if (focus && !touches) return hexAlpha(base, 0.1);
      return hexAlpha(base, link.current ? 0.5 : 0.45);
    },
    [focus, colors],
  );
  const linkWidth = useCallback(
    (link: GLink) => {
      const s = endId(link.source);
      const t = endId(link.target);
      const touches = focus && (s === focus || t === focus);
      return touches ? 2.2 : 1;
    },
    [focus],
  );
  const linkDash = useCallback((link: GLink) => (link.current ? null : [4, 3]), []);
  const linkLabelText = useCallback(
    (link: GLink) => (link.relation ? link.relation.replace(/_/g, " ").toLowerCase() : ""),
    [],
  );

  // ── control actions ──────────────────────────────────────────────────────────
  const fit = useCallback(() => graphRef.current?.zoomToFit(400, 40), []);
  const zoomBy = useCallback((factor: number) => {
    const g = graphRef.current;
    if (!g) return;
    g.zoom(g.zoom() * factor, 200);
  }, []);
  const reset = useCallback(() => {
    setSelected(null);
    setHover(null);
    setQuery("");
    fit();
  }, [fit]);

  // Fit once the first layout settles.
  const onEngineStop = useCallback(() => fit(), [fit]);

  return (
    <section>
      <div className="mb-1 flex items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">Knowledge</h1>
        <label className="flex shrink-0 items-center gap-1.5 text-xs text-neutral-500">
          <input
            type="checkbox"
            checked={showSuperseded}
            onChange={(e) => setShowSuperseded(e.target.checked)}
          />
          show superseded
        </label>
      </div>
      <p className="mb-3 text-xs text-neutral-500">
        what I know about you, connected —{" "}
        <span className="text-neutral-400">drag to explore · scroll to zoom · click a node</span>
      </p>

      {error && <p className="mb-3 text-sm text-red-600">{error}</p>}
      {loading && <Loader label="Loading graph…" />}

      {!loading && !error && graphData.nodes.length === 0 && (
        <div className="rounded-xl border border-neutral-200 px-4 py-6 text-sm text-neutral-400 dark:border-neutral-800">
          {edges.length === 0
            ? "Nothing connected yet. As we talk and I learn facts about you, the people, places, and things in your life show up here linked together."
            : "No connections match these filters. Try enabling superseded facts or clearing the relation filter."}
        </div>
      )}

      {!loading && !error && graphData.nodes.length > 0 && (
        <>
          {/* Controls: search + relation filter + reset. */}
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <div className="relative min-w-0 flex-1">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search nodes…"
                aria-label="Search nodes"
                className="w-full rounded-lg border border-neutral-200 bg-white px-3 py-1.5 text-sm text-neutral-700 placeholder:text-neutral-400 focus:border-sky-400 focus:outline-none dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-200"
              />
              {query && (
                <button
                  onClick={() => setQuery("")}
                  aria-label="Clear search"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-200"
                >
                  ×
                </button>
              )}
            </div>
            <select
              value={relation}
              onChange={(e) => setRelation(e.target.value)}
              aria-label="Filter by relation"
              className="shrink-0 rounded-lg border border-neutral-200 bg-white px-2.5 py-1.5 text-sm text-neutral-700 focus:border-sky-400 focus:outline-none dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-200"
            >
              <option value="all">All relations</option>
              {relations.map((r) => (
                <option key={r} value={r}>
                  {r.replace(/_/g, " ").toLowerCase()}
                </option>
              ))}
            </select>
            <button
              onClick={reset}
              className="shrink-0 rounded-lg border border-neutral-200 px-2.5 py-1.5 text-sm text-neutral-600 transition-colors hover:bg-neutral-100 dark:border-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-800"
            >
              Reset
            </button>
          </div>
          {q && (
            <p className="mb-2 text-xs text-neutral-400">
              {matches.size} match{matches.size === 1 ? "" : "es"} for “{query.trim()}”
            </p>
          )}

          {/* The graph canvas. */}
          <div
            ref={boxRef}
            className="relative overflow-hidden rounded-xl border border-neutral-200 bg-neutral-50/40 dark:border-neutral-800 dark:bg-neutral-900/40"
            style={{ height }}
          >
            {width > 0 && (
              <ForceGraph2D<GNode, GLink>
                ref={graphRef}
                graphData={graphData}
                width={width}
                height={height}
                backgroundColor="rgba(0,0,0,0)"
                nodeRelSize={4.5}
                nodeLabel={(n) => String((n as GNode).label)}
                nodeCanvasObject={paintNode}
                nodePointerAreaPaint={paintNodePointer}
                linkColor={linkColor}
                linkWidth={linkWidth}
                linkLineDash={linkDash}
                linkLabel={linkLabelText}
                linkDirectionalArrowLength={3}
                linkDirectionalArrowRelPos={1}
                linkDirectionalArrowColor={linkColor}
                enableNodeDrag
                minZoom={0.3}
                maxZoom={8}
                cooldownTicks={120}
                onEngineStop={onEngineStop}
                onNodeClick={(n) => {
                  const id = String((n as GNode).id);
                  setSelected((cur) => (cur === id ? null : id));
                }}
                onNodeHover={(n) => setHover(n ? String((n as GNode).id) : null)}
                onBackgroundClick={() => setSelected(null)}
              />
            )}

            {/* Zoom / fit controls, overlaid. */}
            <div className="absolute right-2 top-2 flex flex-col gap-1">
              <ControlButton label="Zoom in" onClick={() => zoomBy(1.4)}>
                +
              </ControlButton>
              <ControlButton label="Zoom out" onClick={() => zoomBy(1 / 1.4)}>
                −
              </ControlButton>
              <ControlButton label="Fit to view" onClick={fit}>
                <FitIcon />
              </ControlButton>
            </div>

            {/* Legend. */}
            <div className="absolute bottom-2 left-2 flex items-center gap-3 rounded-lg bg-white/70 px-2.5 py-1 text-[11px] text-neutral-500 backdrop-blur dark:bg-neutral-900/70 dark:text-neutral-400">
              <span className="flex items-center gap-1.5">
                <span className="inline-block h-0.5 w-4" style={{ background: colors.current }} />
                current
              </span>
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block h-0.5 w-4"
                  style={{
                    backgroundImage: `repeating-linear-gradient(90deg, ${colors.superseded} 0 4px, transparent 4px 7px)`,
                  }}
                />
                superseded
              </span>
            </div>
          </div>

          {/* Side panel: focused node's facts, or the full list. */}
          <div className="mt-4">
            <div className="mb-2 flex items-center gap-2 text-sm">
              {selected ? (
                <>
                  <span className="font-medium text-sky-600 dark:text-sky-300">
                    {selectedLabel}
                  </span>
                  <span className="text-neutral-400">
                    · {selectedEdges.length} connection{selectedEdges.length === 1 ? "" : "s"}
                  </span>
                  <button
                    onClick={() => setSelected(null)}
                    className="ml-auto text-xs text-neutral-400 hover:underline"
                  >
                    clear
                  </button>
                </>
              ) : (
                <span className="text-neutral-400">
                  {shown.length} fact{shown.length === 1 ? "" : "s"} · click a node to focus
                </span>
              )}
            </div>
            <div className="divide-y divide-neutral-200 rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
              {(selected ? selectedEdges : shown).map((e, i) => (
                <div
                  key={i}
                  className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm"
                >
                  <span
                    className={
                      e.current ? "" : "text-neutral-400 line-through decoration-neutral-400/50"
                    }
                  >
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
        </>
      )}
    </section>
  );
}

// Small square control button used for the zoom/fit overlay.
function ControlButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      title={label}
      className="grid h-8 w-8 place-items-center rounded-lg border border-neutral-200 bg-white/80 text-neutral-600 backdrop-blur transition-colors hover:bg-white hover:text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900/80 dark:text-neutral-300 dark:hover:bg-neutral-800 dark:hover:text-neutral-100"
    >
      {children}
    </button>
  );
}

function FitIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3" />
    </svg>
  );
}

// Apply an alpha channel to a #rrggbb colour, for canvas stroke/fill.
function hexAlpha(hex: string, a: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}
