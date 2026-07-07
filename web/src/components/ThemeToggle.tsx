import type { ThemePref } from "../lib/theme";

interface Props {
  pref: ThemePref;
  onChange: (pref: ThemePref) => void;
}

const OPTIONS: { value: ThemePref; label: string; icon: React.ReactNode }[] = [
  { value: "light", label: "Light theme", icon: <SunIcon /> },
  { value: "system", label: "System theme", icon: <AutoIcon /> },
  { value: "dark", label: "Dark theme", icon: <MoonIcon /> },
];

// Segmented light / system / dark control. Purely presentational — the parent
// owns the persisted preference via useTheme.
export function ThemeToggle({ pref, onChange }: Props) {
  return (
    <div
      role="radiogroup"
      aria-label="Color theme"
      className="flex items-center gap-0.5 rounded-full border border-slate-200 bg-slate-100/70 p-0.5 dark:border-slate-700 dark:bg-slate-800/70"
    >
      {OPTIONS.map((o) => {
        const active = pref === o.value;
        return (
          <button
            key={o.value}
            role="radio"
            aria-checked={active}
            aria-label={o.label}
            title={o.label}
            onClick={() => onChange(o.value)}
            className={`grid h-7 w-7 place-items-center rounded-full transition-colors ${
              active
                ? "bg-white text-sky-600 shadow-sm dark:bg-slate-950 dark:text-sky-300"
                : "text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
            }`}
          >
            {o.icon}
          </button>
        );
      })}
    </div>
  );
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}

function AutoIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="4" width="20" height="14" rx="2" />
      <path d="M8 21h8M12 18v3" />
    </svg>
  );
}
