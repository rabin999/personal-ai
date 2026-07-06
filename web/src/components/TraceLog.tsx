import { useEffect, useRef } from "react";
import type { Stage, TraceEvent } from "../lib/types";

// Colour + glyph per pipeline stage so the trace reads at a glance.
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

export function TraceLog({ events }: { events: TraceEvent[] }) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  return (
    <aside className="flex h-full w-96 shrink-0 flex-col border-l border-slate-800 bg-slate-950/60">
      <div className="border-b border-slate-800 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-200">Trace</h2>
        <p className="text-xs text-slate-500">Start-to-finish, every turn</p>
      </div>
      <div className="flex-1 overflow-y-auto px-3 py-2 font-mono text-xs">
        {events.length === 0 && (
          <p className="px-1 py-4 text-slate-600">Press and hold to talk — the pipeline shows up here.</p>
        )}
        {events.map((e, i) => {
          const meta = STAGE_META[e.stage] ?? STAGE_META.session;
          return (
            <div key={i} className="flex gap-2 py-1 leading-snug">
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
        <div ref={endRef} />
      </div>
    </aside>
  );
}
