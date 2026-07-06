import type { TurnGroup } from "../lib/types";
import { TurnCard } from "./TurnCard";

interface Props {
  turns: TurnGroup[];
  openTurn: number | null;
  onToggle: (index: number) => void;
  onReplay: (turn: TurnGroup) => void;
}

// Right sidebar (~35% of the viewport on desktop): one collapsible card per
// conversation turn. The most recent turn is expanded; the rest stay collapsed.
export function TraceLog({ turns, openTurn, onToggle, onReplay }: Props) {
  const realTurns = turns.filter((t) => t.index > 0).length;
  return (
    <aside className="flex h-full w-full shrink-0 flex-col border-l border-slate-200 bg-slate-100/60 lg:w-[35%] lg:min-w-[22rem] lg:max-w-[34rem] dark:border-slate-800 dark:bg-slate-950/60">
      <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4 dark:border-slate-800">
        <div>
          <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
            Conversation trace
          </h2>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Each turn, start to finish — tap to expand
          </p>
        </div>
        {realTurns > 0 && (
          <span className="rounded-full bg-indigo-100 px-2.5 py-1 text-xs font-medium text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300">
            {realTurns} {realTurns === 1 ? "turn" : "turns"}
          </span>
        )}
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
          />
        ))}
      </div>
    </aside>
  );
}
