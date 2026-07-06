import type { Stage, TurnGroup } from "../lib/types";

const STAGE_META: Record<Stage, { color: string; glyph: string; label: string }> = {
  session: { color: "text-slate-500 dark:text-slate-400", glyph: "◆", label: "SESSION" },
  vad: { color: "text-cyan-600 dark:text-cyan-400", glyph: "≋", label: "VAD §19" },
  stt: { color: "text-sky-600 dark:text-sky-400", glyph: "✎", label: "STT §20" },
  endpoint: { color: "text-teal-600 dark:text-teal-400", glyph: "⏱", label: "ENDPOINT §21" },
  emotion: { color: "text-pink-600 dark:text-pink-400", glyph: "♥", label: "EMOTION §22" },
  assembly: { color: "text-violet-600 dark:text-violet-400", glyph: "▤", label: "ASSEMBLY §10" },
  router: { color: "text-indigo-600 dark:text-indigo-400", glyph: "⇄", label: "ROUTER §11" },
  generation: { color: "text-fuchsia-600 dark:text-fuchsia-400", glyph: "✦", label: "GENERATE §12" },
  response: { color: "text-emerald-600 dark:text-emerald-300", glyph: "❝", label: "RESPONSE" },
  tts: { color: "text-amber-600 dark:text-amber-400", glyph: "♪", label: "TTS §23" },
  barge_in: { color: "text-orange-600 dark:text-orange-400", glyph: "⨯", label: "BARGE-IN §24" },
  error: { color: "text-red-600 dark:text-red-400", glyph: "!", label: "ERROR" },
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
    <div className="mb-2 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900/50">
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-slate-50 dark:hover:bg-slate-800/50"
      >
        <span
          className={`text-[10px] text-slate-400 transition-transform dark:text-slate-500 ${open ? "rotate-90" : ""}`}
        >
          ▶
        </span>
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-slate-700 dark:text-slate-200">
          {turn.index > 0 && (
            <span className="mr-1.5 text-slate-400 dark:text-slate-500">#{turn.index}</span>
          )}
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
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                e.stopPropagation();
                onReplay();
              }
            }}
            className="flex shrink-0 items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-700 transition-colors hover:bg-amber-200 dark:bg-amber-500/20 dark:text-amber-300 dark:hover:bg-amber-500/30"
            title="Replay reply audio"
          >
            ▶ play
          </span>
        )}
      </button>

      {open && (
        <div className="border-t border-slate-200 px-3 py-2.5 font-mono text-[11px] dark:border-slate-800">
          {turn.events.map((e, i) => {
            const meta = STAGE_META[e.stage] ?? STAGE_META.session;
            return (
              <div key={i} className="flex gap-2 py-0.5 leading-snug">
                <span className={`${meta.color} shrink-0`}>{meta.glyph}</span>
                <div className="min-w-0">
                  <span className={`${meta.color} mr-1.5`}>{meta.label}</span>
                  <span
                    className={
                      e.level === "error"
                        ? "text-red-600 dark:text-red-300"
                        : "text-slate-600 dark:text-slate-300"
                    }
                  >
                    {e.message}
                  </span>
                </div>
              </div>
            );
          })}
          {turn.reply && (
            <p className="mt-2 border-t border-slate-200 pt-2 text-emerald-700 dark:border-slate-800 dark:text-emerald-200">
              {turn.reply}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
