import { useEffect, useMemo, useRef } from "react";
import type { TurnGroup, TurnState } from "../lib/types";

interface Props {
  turns: TurnGroup[];
  turnState: TurnState; // live phase, so the chat shows WHICH side is processing right now
  onReplay: (turn: TurnGroup) => void;
  onStopReplay: () => void;
  playingIndex: number | null; // turn.index currently replaying, or null
  mobileOpen: boolean;
  onCloseMobile: () => void;
}

// A single spoken line in the live transcript: who said it and what was said.
interface Line {
  key: string;
  turn: TurnGroup;
  role: "user" | "companion";
  text: string;
  ts: number; // when this line was said (ms epoch), for the message timestamp
}

// "08:42:36 PM" — clock time a line was said, in the VIEWER'S local timezone. The backend
// stamps events with time.time() (epoch SECONDS); JS Date wants milliseconds, so scale up when
// the value looks like seconds (< year ~33658). toLocaleTimeString then renders in the browser's
// own timezone — i.e. the user's actual local time (Nepal, etc.), not the server's.
function fmtTime(ts: number): string {
  const ms = ts < 1e12 ? ts * 1000 : ts;
  return new Date(ms).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
}

// LIVE conversation transcript: nothing but what was SAID, streaming in as it
// happens — the user's words the moment STT lands, the companion's reply the
// moment it's generated. No pipeline steps here; the rich per-step trace lives
// on the dedicated Trace page. Desktop: a persistent right column; mobile: a
// slide-in drawer toggled from the header.
export function TraceLog({
  turns,
  turnState,
  onReplay,
  onStopReplay,
  playingIndex,
  mobileOpen,
  onCloseMobile,
}: Props) {
  // Flatten grouped turns into an ordered list of spoken lines: the user line
  // (from STT) then the companion line (from the reply), turn by turn. Each
  // appears as soon as its source event has arrived — a true live transcript.
  const lines = useMemo<Line[]>(() => {
    const out: Line[] = [];
    for (const t of [...turns].sort((a, b) => a.index - b.index)) {
      const heard = clean(t.heard);
      const reply = clean(t.reply);
      const at = (stage: string) =>
        t.events.find((e) => e.stage === stage)?.ts ?? t.events[t.events.length - 1]?.ts ?? Date.now();
      if (heard)
        out.push({ key: `${t.index}-u`, turn: t, role: "user", text: heard, ts: at("stt") });
      if (reply)
        out.push({
          key: `${t.index}-c`,
          turn: t,
          role: "companion",
          text: reply,
          ts: at("response"),
        });
    }
    return out;
  }, [turns]);

  // Which side is processing RIGHT NOW, so the dots-wave sits on that side of the chat
  // (user while their speech is being transcribed, companion while it's thinking up the
  // reply). Once the reply streams in as text, the growing bubble is the indicator.
  const pending: "user" | "companion" | null =
    turnState === "listening" ? "user" : turnState === "thinking" ? "companion" : null;

  // Keep the newest line in view as the conversation streams (and as the dots appear/move).
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [lines.length, pending]);

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
        className={`fixed inset-y-0 right-0 z-30 flex w-full max-w-md flex-col border-l border-slate-200 bg-slate-50 shadow-2xl transition-transform duration-300 lg:static lg:z-auto lg:w-[35%] lg:min-w-[24rem] lg:max-w-none lg:translate-x-0 lg:bg-slate-100/60 lg:shadow-none dark:border-slate-800 dark:bg-slate-950 lg:dark:bg-slate-950/60 ${
          mobileOpen ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4 dark:border-slate-800">
          <div>
            <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
              Live transcript
            </h2>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              What's said, as it's said
            </p>
          </div>
          <button
            onClick={onCloseMobile}
            aria-label="Close transcript"
            className="grid h-8 w-8 place-items-center rounded-lg text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700 lg:hidden dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="thin-scroll flex-1 space-y-3 overflow-y-auto px-4 py-4">
          {lines.length === 0 && (
            <div className="mt-6 flex flex-col items-center gap-2 px-4 text-center">
              <div className="grid h-11 w-11 place-items-center rounded-full bg-slate-200 text-slate-400 dark:bg-slate-800 dark:text-slate-500">
                <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M8 12h.01M12 12h.01M16 12h.01M21 12a9 9 0 0 1-13.5 7.8L3 21l1.2-4.5A9 9 0 1 1 21 12z" />
                </svg>
              </div>
              <p className="text-sm text-slate-500 dark:text-slate-400">
                Start talking — your words and the reply appear here live.
              </p>
            </div>
          )}
          {lines.map((line) => (
            <Bubble
            key={line.key}
            line={line}
            onReplay={onReplay}
            onStopReplay={onStopReplay}
            playing={playingIndex === line.turn.index}
          />
          ))}
          {pending && <PendingBubble side={pending} />}
          <div ref={endRef} />
        </div>
      </aside>
    </>
  );
}

// One transcript line. User: right-aligned, sky. Companion: left-aligned,
// neutral, with an unobtrusive replay control when its audio is available.
function Bubble({
  line,
  onReplay,
  onStopReplay,
  playing,
}: {
  line: Line;
  onReplay: (turn: TurnGroup) => void;
  onStopReplay: () => void;
  playing: boolean;
}) {
  const isUser = line.role === "user";
  const canReplay = !isUser && line.turn.audio.length > 0;
  return (
    <div className={`flex flex-col gap-1 ${isUser ? "items-end" : "items-start"}`}>
      <span
        className={`text-[10px] font-semibold uppercase tracking-wider ${
          isUser ? "text-sky-600 dark:text-sky-400" : "text-emerald-600 dark:text-emerald-400"
        }`}
      >
        {isUser ? "You" : "Saathi"}
      </span>
      <div
        className={`max-w-[85%] rounded-2xl px-3.5 py-2 text-sm leading-relaxed shadow-sm ${
          isUser
            ? "rounded-tr-sm bg-sky-600 text-white"
            : "rounded-tl-sm bg-white text-slate-700 dark:bg-slate-800 dark:text-slate-100"
        }`}
      >
        {line.text}
        {canReplay && (
          <button
            onClick={() => (playing ? onStopReplay() : onReplay(line.turn))}
            title={playing ? "Stop playback" : "Replay reply audio"}
            aria-label={playing ? "Stop playback" : "Replay reply audio"}
            className="ml-2 inline-flex translate-y-px items-center text-emerald-600 transition-colors hover:text-emerald-500 dark:text-emerald-400"
          >
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="currentColor">
              {playing ? <rect x="6" y="6" width="12" height="12" rx="1.5" /> : <path d="M8 5v14l11-7z" />}
            </svg>
          </button>
        )}
        {/* clock time the line was said — bottom-right, its own line */}
        <div
          className={`mt-1 text-right text-[10px] tabular-nums ${
            isUser ? "text-sky-100/80" : "text-slate-400 dark:text-slate-500"
          }`}
        >
          {fmtTime(line.ts)}
        </div>
      </div>
    </div>
  );
}

// A live "processing" bubble on the side that's currently working — the user's side while
// their speech is transcribed, the companion's side while it thinks up the reply — so it's
// always clear WHICH part is busy, right in the chat (not just on the orb). A typing-style
// dots-wave stands in for the words that haven't landed yet.
function PendingBubble({ side }: { side: "user" | "companion" }) {
  const isUser = side === "user";
  return (
    <div className={`flex flex-col gap-1 ${isUser ? "items-end" : "items-start"}`}>
      <span
        className={`text-[10px] font-semibold uppercase tracking-wider ${
          isUser ? "text-sky-600 dark:text-sky-400" : "text-emerald-600 dark:text-emerald-400"
        }`}
      >
        {isUser ? "You" : "Saathi"}
      </span>
      <div
        className={`rounded-2xl px-4 py-3 shadow-sm ${
          isUser
            ? "rounded-tr-sm bg-sky-600"
            : "rounded-tl-sm bg-white dark:bg-slate-800"
        }`}
        aria-label={isUser ? "Transcribing your speech" : "Thinking"}
      >
        <DotWave color={isUser ? "#ffffff" : "#34d399"} />
      </div>
    </div>
  );
}

// Typing-style dots-wave (shared keyframe `asaathi-dot-wave` in index.css).
function DotWave({ color }: { color: string }) {
  return (
    <span className="inline-flex items-end gap-[3px]" aria-hidden>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-1.5 w-1.5 rounded-full"
          style={{
            background: color,
            boxShadow: `0 0 6px ${color}`,
            animation: "asaathi-dot-wave 1.1s ease-in-out infinite",
            animationDelay: `${i * 160}ms`,
          }}
        />
      ))}
    </span>
  );
}

// Strip inline TTS tags ([warm], <angle>) and markdown emphasis so the
// transcript shows exactly what was said — nothing markup-ish leaks through.
function clean(text: string): string {
  return (text ?? "")
    .replace(/\[[^[\]]*\]|<[^<>]*>/g, "")
    .replace(/[*_`]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}
