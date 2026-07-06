import type { TurnGroup } from "../lib/types";
import { TurnCard } from "./TurnCard";

interface Props {
  turns: TurnGroup[];
  openTurn: number | null;
  onToggle: (index: number) => void;
  onReplay: (turn: TurnGroup) => void;
}

// Right sidebar: one collapsible card per conversation turn. The most recent
// turn is expanded; the rest stay collapsed until toggled.
export function TraceLog({ turns, openTurn, onToggle, onReplay }: Props) {
  return (
    <aside className="flex h-full w-96 shrink-0 flex-col border-l border-slate-800 bg-slate-950/60">
      <div className="border-b border-slate-800 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-200">Conversation trace</h2>
        <p className="text-xs text-slate-500">Each turn, start to finish — tap to expand</p>
      </div>
      <div className="flex-1 overflow-y-auto px-3 py-2">
        {turns.length === 0 && (
          <p className="px-1 py-4 font-mono text-xs text-slate-600">
            Start a conversation and just talk — each turn's pipeline shows up here.
          </p>
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
