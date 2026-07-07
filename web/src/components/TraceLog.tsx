import type { TurnGroup } from "../lib/types";
import { TurnCard } from "./TurnCard";

interface Props {
  turns: TurnGroup[];
  openTurn: number | null;
  onToggle: (index: number) => void;
  onReplay: (turn: TurnGroup) => void;
  onFeedback?: (turn: TurnGroup, rating: "up" | "down", note: string) => void;
  mobileOpen: boolean;
  onCloseMobile: () => void;
}

// Conversation trace: one collapsible card per turn (newest expanded). On
// desktop it's a persistent right sidebar (~35% width); on mobile it's a
// slide-in drawer toggled from the header so the voice UI owns the small screen.
export function TraceLog({
  turns,
  openTurn,
  onToggle,
  onReplay,
  onFeedback,
  mobileOpen,
  onCloseMobile,
}: Props) {
  const realTurns = turns.filter((t) => t.index > 0).length;
  return (
    <>
      {/* Mobile scrim (drawer only) */}
      {mobileOpen && (
        <div
          onClick={onCloseMobile}
          aria-hidden
          className="fixed inset-0 z-20 bg-slate-900/30 backdrop-blur-sm lg:hidden dark:bg-slate-950/50"
        />
      )}

      <aside
        className={`fixed inset-y-0 right-0 z-30 flex w-full max-w-md flex-col border-l border-slate-200 bg-slate-50 shadow-2xl transition-transform duration-300 lg:static lg:z-auto lg:w-[35%] lg:min-w-[22rem] lg:max-w-[34rem] lg:translate-x-0 lg:bg-slate-100/60 lg:shadow-none dark:border-slate-800 dark:bg-slate-950 lg:dark:bg-slate-950/60 ${
          mobileOpen ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4 dark:border-slate-800">
          <div>
            <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
              Conversation trace
            </h2>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              Each turn, start to finish — tap to expand
            </p>
          </div>
          <div className="flex items-center gap-2">
            {realTurns > 0 && (
              <span className="rounded-full bg-indigo-100 px-2.5 py-1 text-xs font-medium text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300">
                {realTurns} {realTurns === 1 ? "turn" : "turns"}
              </span>
            )}
            <button
              onClick={onCloseMobile}
              aria-label="Close trace"
              className="grid h-8 w-8 place-items-center rounded-lg text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700 lg:hidden dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <path d="M18 6 6 18M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      <div className="thin-scroll flex-1 overflow-y-auto px-3 py-3">
        {turns.length === 0 && (
          <div className="mt-6 flex flex-col items-center gap-2 px-4 text-center">
            <div className="grid h-11 w-11 place-items-center rounded-full bg-slate-200 text-slate-400 dark:bg-slate-800 dark:text-slate-500">
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 6h16M4 12h10M4 18h7" />
              </svg>
            </div>
            <p className="text-sm text-slate-500 dark:text-slate-400">
              Start a conversation and just talk — each turn's pipeline shows up
              here.
            </p>
          </div>
        )}
        {[...turns].reverse().map((turn) => (
          <TurnCard
            key={turn.index}
            turn={turn}
            open={openTurn === turn.index}
            onToggle={() => onToggle(turn.index)}
            onReplay={() => onReplay(turn)}
            onFeedback={onFeedback ? (r, n) => onFeedback(turn, r, n) : undefined}
          />
        ))}
      </div>
      </aside>
    </>
  );
}
