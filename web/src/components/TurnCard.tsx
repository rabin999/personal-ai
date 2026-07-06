import type { Stage, TurnGroup } from "../lib/types";

const STAGE_META: Record<Stage, { color: string; glyph: string; label: string }> = {
  session: { color: "text-slate-400", glyph: "◆", label: "SESSION" },
  vad: { color: "text-cyan-400", glyph: "≋", label: "VAD §19" },
  stt: { color: "text-sky-400", glyph: "✎", label: "STT §20" },
  endpoint: { color: "text-teal-400", glyph: "⏱", label: "ENDPOINT §21" },
  emotion: { color: "text-pink-400", glyph: "♥", label: "EMOTION §22" },
  assembly: { color: "text-violet-400", glyph: "▤", label: "ASSEMBLY §10" },
  router: { color: "text-indigo-400", glyph: "⇄", label: "ROUTER §11" },
  generation: { color: "text-fuchsia-400", glyph: "✦", label: "GENERATE §12" },
  response: { color: "text-emerald-300", glyph: "❝", label: "RESPONSE" },
  tts: { color: "text-amber-400", glyph: "♪", label: "TTS §23" },
  barge_in: { color: "text-orange-400", glyph: "⨯", label: "BARGE-IN §24" },
  error: { color: "text-red-400", glyph: "!", label: "ERROR" },
};

interface Props {
  turn: TurnGroup;
  open: boolean;
  onToggle: () => void;
  onReplay: () => void;
}

// One collapsible conversation turn: header (what was heard / replied) plus the
// per-stage pipeline trace, and a replay button for the reply audio.
export function TurnCard({ turn, open, onToggle, onReplay }: Props) {
  const title =
    turn.index === 0 ? "Listening…" : turn.heard ? `“${turn.heard}”` : `Turn ${turn.index}`;

  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-slate-800 bg-slate-900/40">
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-slate-800/40"
      >
        <span className={`text-xs transition-transform ${open ? "rotate-90" : ""}`}>▶</span>
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-slate-200">
          {turn.index > 0 && <span className="mr-1.5 text-slate-500">#{turn.index}</span>}
          {title}
        </span>
        {turn.audio.length > 0 && (
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation();
              onReplay();
            }}
            className="shrink-0 rounded-full bg-amber-500/20 px-2 py-0.5 text-[11px] text-amber-300 hover:bg-amber-500/30"
            title="Replay reply audio"
          >
            ▶ play
          </span>
        )}
      </button>

      {open && (
        <div className="border-t border-slate-800 px-3 py-2 font-mono text-[11px]">
          {turn.events.map((e, i) => {
            const meta = STAGE_META[e.stage] ?? STAGE_META.session;
            return (
              <div key={i} className="flex gap-2 py-0.5 leading-snug">
                <span className={`${meta.color} shrink-0`}>{meta.glyph}</span>
                <div className="min-w-0">
                  <span className={`${meta.color} mr-1.5`}>{meta.label}</span>
                  <span className={e.level === "error" ? "text-red-300" : "text-slate-300"}>
                    {e.message}
                  </span>
                </div>
              </div>
            );
          })}
          {turn.reply && (
            <p className="mt-2 border-t border-slate-800 pt-2 text-emerald-200">{turn.reply}</p>
          )}
        </div>
      )}
    </div>
  );
}
