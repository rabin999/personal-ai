import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { ThemeToggle } from "./ThemeToggle";
import { useTheme } from "../lib/theme";
import { getTools } from "../lib/api";

// The app's single, consistent header — same brand, nav, external-tool links, and
// theme control on EVERY page (F10). Data pages wrap their content in <Shell>,
// which renders this; the companion page renders it too, so the chrome never
// changes between routes. Mobile-first: the primary nav scrolls horizontally and
// the tool links collapse into a compact menu (F12).

const NAV = [
  { to: "/", label: "Asaathi", end: true },
  { to: "/conversations", label: "Conversations", end: false },
  { to: "/memories", label: "Memories", end: false },
];

interface ToolLinks {
  langfuse?: string;
  langgraph?: string;
}

export function AppHeader({ right }: { right?: React.ReactNode }) {
  const { pref, setPref } = useTheme();
  const [tools, setTools] = useState<ToolLinks>({});
  const [toolsOpen, setToolsOpen] = useState(false);

  useEffect(() => {
    getTools()
      .then((r) => setTools(r.tools ?? {}))
      .catch(() => {});
  }, []);

  const hasTools = Boolean(tools.langfuse || tools.langgraph);

  return (
    <header className="sticky top-0 z-20 border-b border-neutral-200 bg-white/85 backdrop-blur dark:border-neutral-800 dark:bg-neutral-900/85">
      <div className="mx-auto flex max-w-5xl items-center gap-2 px-3 py-2.5 sm:gap-3 sm:px-4">
        {/* Brand */}
        <NavLink to="/" className="flex shrink-0 items-center gap-2">
          <span className="grid h-8 w-8 place-items-center rounded-xl bg-gradient-to-br from-sky-500 to-cyan-500 text-white shadow-sm">
            <svg viewBox="0 0 24 24" className="h-4.5 w-4.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
              <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4M8 22h8" />
            </svg>
          </span>
          <span className="hidden text-sm font-semibold sm:inline">Asaathi</span>
        </NavLink>

        {/* Primary nav — scrolls horizontally on narrow screens (never clips). */}
        <nav className="thin-scroll -mx-1 flex flex-1 items-center gap-0.5 overflow-x-auto px-1">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap rounded-md px-2.5 py-1.5 text-sm font-medium transition-colors ${
                  isActive
                    ? "text-sky-600 dark:text-sky-400"
                    : "text-neutral-500 hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
                }`
              }
            >
              {n.label}
            </NavLink>
          ))}
        </nav>

        {right}

        {/* External tool UIs (F9): Langfuse dashboard + LangGraph Studio. */}
        {hasTools && (
          <div className="relative shrink-0">
            <button
              onClick={() => setToolsOpen((v) => !v)}
              onBlur={() => setTimeout(() => setToolsOpen(false), 150)}
              aria-label="External tools"
              title="External tools"
              className="grid h-9 w-9 place-items-center rounded-full border border-neutral-200 text-neutral-600 transition-colors hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.5 1.5" />
                <path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.5-1.5" />
              </svg>
            </button>
            {toolsOpen && (
              // preventDefault on mousedown keeps the trigger focused so its onBlur
              // doesn't close the menu before the link's click fires — the whole row
              // reliably navigates (fixes: only some clicks opened Langfuse).
              <div
                onMouseDown={(e) => e.preventDefault()}
                className="absolute right-0 top-11 z-30 w-52 overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-lg dark:border-neutral-700 dark:bg-neutral-900"
              >
                <p className="px-3 pb-1 pt-2.5 text-[11px] font-medium uppercase tracking-wider text-neutral-400">
                  Open tool
                </p>
                {tools.langfuse && (
                  <ToolLink href={tools.langfuse} label="Langfuse" sub="Traces · prompts · evals" />
                )}
                {tools.langgraph && (
                  <ToolLink href={tools.langgraph} label="LangGraph Studio" sub="Reasoning graph" />
                )}
              </div>
            )}
          </div>
        )}

        <ThemeToggle pref={pref} onChange={setPref} />
      </div>
    </header>
  );
}

function ToolLink({ href, label, sub }: { href: string; label: string; sub: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-2 px-3 py-2 text-sm hover:bg-neutral-100 dark:hover:bg-neutral-800"
    >
      <span className="flex-1">
        <span className="block font-medium">{label}</span>
        <span className="block text-xs text-neutral-500">{sub}</span>
      </span>
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 text-neutral-400" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M7 17L17 7M7 7h10v10" />
      </svg>
    </a>
  );
}
