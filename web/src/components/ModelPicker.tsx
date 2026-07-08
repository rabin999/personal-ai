import Fuse from "fuse.js";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

// A searchable model combobox: fuzzy search (fuse.js) in a search bar fixed at the
// top, a scrollable list (~5 rows), and a FIXED-position dropdown anchored to the
// button so it's never clipped by a parent's overflow and always stays inside the
// viewport (flips above the button when there isn't room below — mobile included).
// Value "" means "Default (auto)".

interface Pos {
  left: number;
  top: number;
  width: number;
  maxHeight: number;
  up: boolean;
}

export function ModelPicker({
  value,
  options,
  onChange,
  disabled,
  title,
}: {
  value: string;
  options: string[];
  onChange: (v: string) => void;
  disabled?: boolean;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [pos, setPos] = useState<Pos | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const fuse = useMemo(
    () => new Fuse(options, { threshold: 0.4, ignoreLocation: true }),
    [options],
  );
  const results = useMemo(() => {
    const needle = q.trim();
    if (!needle) return options.slice(0, 200);
    return fuse.search(needle, { limit: 200 }).map((r) => r.item);
  }, [q, options, fuse]);

  // Position the fixed dropdown against the button, flipping up if needed. Runs
  // when opening and on scroll/resize so it tracks the anchor.
  const place = () => {
    const b = btnRef.current?.getBoundingClientRect();
    if (!b) return;
    const margin = 8;
    const below = window.innerHeight - b.bottom - margin;
    const above = b.top - margin;
    const up = below < 220 && above > below;
    const maxHeight = Math.min(320, Math.max(140, (up ? above : below)));
    setPos({ left: b.left, top: up ? b.top : b.bottom, width: b.width, maxHeight, up });
  };

  useLayoutEffect(() => {
    if (open) place();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || popRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const reflow = () => place();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", reflow);
    window.addEventListener("scroll", reflow, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", reflow);
      window.removeEventListener("scroll", reflow, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const pick = (v: string) => {
    onChange(v);
    setOpen(false);
    setQ("");
  };

  const FIELD =
    "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition-colors focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800/80 dark:text-slate-100";

  return (
    <div className="min-w-0">
      <button
        ref={btnRef}
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        title={title}
        className={`${FIELD} flex items-center justify-between gap-2 text-left`}
      >
        <span className={`min-w-0 truncate ${value ? "" : "text-slate-400"}`}>
          {value || "Default (auto)"}
        </span>
        <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0 text-slate-400" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M6 9l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && pos && (
        <div
          ref={popRef}
          style={{
            position: "fixed",
            left: pos.left,
            width: pos.width,
            ...(pos.up
              ? { bottom: window.innerHeight - pos.top + 4 }
              : { top: pos.top + 4 }),
          }}
          className="z-[100] overflow-hidden rounded-lg border border-slate-200 bg-white shadow-xl dark:border-slate-700 dark:bg-slate-800"
        >
          <div className="border-b border-slate-200 p-1.5 dark:border-slate-700">
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search models…"
              className="w-full rounded-md bg-slate-100 px-2.5 py-1.5 text-sm text-slate-900 outline-none dark:bg-slate-900/60 dark:text-slate-100"
            />
          </div>
          <ul className="overflow-y-auto py-1 text-sm" style={{ maxHeight: pos.maxHeight - 52 }}>
            <Row label="Default (auto)" active={value === ""} onClick={() => pick("")} muted />
            {results.map((o) => (
              <Row key={o} label={o} active={o === value} onClick={() => pick(o)} />
            ))}
            {results.length === 0 && (
              <li className="px-3 py-2 text-slate-400">No models match “{q}”.</li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}

function Row({ label, active, onClick, muted }: { label: string; active: boolean; onClick: () => void; muted?: boolean }) {
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className={`flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left hover:bg-slate-100 dark:hover:bg-slate-700/60 ${
          active ? "bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-300" : muted ? "text-slate-500" : "text-slate-800 dark:text-slate-200"
        }`}
      >
        <span className="min-w-0 truncate">{label}</span>
        {active && (
          <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M20 6L9 17l-5-5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
      </button>
    </li>
  );
}
