import { useEffect, useMemo, useRef, useState } from "react";

// A searchable model combobox: a fixed search bar on top + a scrollable list that
// shows ~5 rows at a time (the full OpenRouter catalog is hundreds of models). Used
// for both the fast and thinking model pickers. Value "" means "Default (auto)".
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
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = needle ? options.filter((o) => o.toLowerCase().includes(needle)) : options;
    return list.slice(0, 200); // cap render; search narrows it further
  }, [options, q]);

  const pick = (v: string) => {
    onChange(v);
    setOpen(false);
    setQ("");
  };

  const FIELD =
    "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition-colors focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800/80 dark:text-slate-100";

  return (
    <div ref={ref} className="relative min-w-0">
      <button
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

      {open && (
        <div className="absolute z-50 mt-1 w-full overflow-hidden rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-800">
          <div className="border-b border-slate-200 p-1.5 dark:border-slate-700">
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search models…"
              className="w-full rounded-md bg-slate-100 px-2.5 py-1.5 text-sm text-slate-900 outline-none dark:bg-slate-900/60 dark:text-slate-100"
            />
          </div>
          {/* ~5 rows tall, then scroll */}
          <ul className="max-h-[11rem] overflow-y-auto py-1 text-sm">
            <Row label="Default (auto)" active={value === ""} onClick={() => pick("")} muted />
            {filtered.map((o) => (
              <Row key={o} label={o} active={o === value} onClick={() => pick(o)} />
            ))}
            {filtered.length === 0 && (
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
