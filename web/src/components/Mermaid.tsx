import { useEffect, useRef, useState } from "react";

// Renders a Mermaid diagram themed to match the app (sky/slate), with zoom / pan / reset
// controls. Mermaid is imported dynamically so its (large) bundle only loads on the page
// that uses it (/how-it-works), not the main app.
export function Mermaid({ chart, dark }: { chart: string; dark: boolean }) {
  const [svg, setSvg] = useState<string>("");
  const [failed, setFailed] = useState(false);
  const vpRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: "base",
          themeVariables: {
            fontFamily: "inherit",
            fontSize: "13px",
            primaryColor: dark ? "#0f2035" : "#eff6ff",
            primaryBorderColor: "#0ea5e9",
            primaryTextColor: dark ? "#e2e8f0" : "#0f172a",
            lineColor: dark ? "#5b7089" : "#94a3b8",
            secondaryColor: dark ? "#0b1220" : "#f0f9ff",
            tertiaryColor: dark ? "#0b1220" : "#f8fafc",
            clusterBkg: dark ? "rgba(15,23,42,0.45)" : "rgba(255,255,255,0.6)",
            clusterBorder: dark ? "#334155" : "#cbd5e1",
            titleColor: dark ? "#7dd3fc" : "#0284c7",
            edgeLabelBackground: dark ? "#0b1220" : "#ffffff",
          },
          // Fit to the container WIDTH so the diagram is readable by default; zoom for detail.
          flowchart: { curve: "basis", padding: 16, useMaxWidth: true },
        });
        const id = "mmd-" + Math.random().toString(36).slice(2);
        const { svg } = await mermaid.render(id, chart);
        if (active) {
          setSvg(svg);
          setScale(1);
          setPos({ x: 0, y: 0 });
        }
      } catch {
        if (active) setFailed(true);
      }
    })();
    return () => {
      active = false;
    };
  }, [chart, dark]);

  const clampS = (s: number) => Math.min(3, Math.max(0.4, +s.toFixed(3)));
  const zoom = (factor: number) => setScale((s) => clampS(s * factor));
  const reset = () => {
    setScale(1);
    setPos({ x: 0, y: 0 });
  };

  const onDown = (e: React.PointerEvent) => {
    drag.current = { x: e.clientX, y: e.clientY, ox: pos.x, oy: pos.y };
    setDragging(true);
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
  };
  const onMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    setPos({ x: drag.current.ox + (e.clientX - drag.current.x), y: drag.current.oy + (e.clientY - drag.current.y) });
  };
  const onUp = () => {
    drag.current = null;
    setDragging(false);
  };

  if (failed) {
    return (
      <p className="rounded-xl border border-slate-200 bg-white/60 p-4 text-sm text-slate-500 dark:border-slate-800 dark:bg-slate-900/50">
        Diagram couldn't render here — the architecture is described in the sections below.
      </p>
    );
  }

  return (
    <div className="relative">
      {/* Controls */}
      <div className="absolute right-2 top-2 z-10 flex items-center gap-1 rounded-xl border border-slate-200/80 bg-white/85 p-1 shadow-sm backdrop-blur dark:border-slate-700/70 dark:bg-slate-900/80">
        <Ctrl label="Zoom in" onClick={() => zoom(1.25)}>
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
        </Ctrl>
        <Ctrl label="Zoom out" onClick={() => zoom(0.8)}>
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M5 12h14" /></svg>
        </Ctrl>
        <Ctrl label="Reset" onClick={reset}>
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" /></svg>
        </Ctrl>
      </div>

      <div
        ref={vpRef}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerLeave={onUp}
        className={`h-[440px] touch-none select-none overflow-hidden rounded-xl bg-slate-50/40 sm:h-[560px] dark:bg-slate-950/30 ${dragging ? "cursor-grabbing" : "cursor-grab"}`}
      >
        <div
          className="flex h-full w-full items-center justify-center"
          style={{
            transform: `translate(${pos.x}px, ${pos.y}px) scale(${scale})`,
            transformOrigin: "center center",
            transition: dragging ? "none" : "transform 120ms ease",
          }}
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
      <p className="mt-2 text-center text-xs text-slate-400 dark:text-slate-500">
        Drag to pan · use the controls to zoom
      </p>
    </div>
  );
}

function Ctrl({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className="grid h-8 w-8 place-items-center rounded-lg text-slate-600 transition-colors hover:bg-sky-500/10 hover:text-sky-600 dark:text-slate-300 dark:hover:text-sky-400"
    >
      {children}
    </button>
  );
}
