import { useCallback, useEffect, useRef, useState } from "react";
import { Orb } from "./components/Orb";
import { MicPicker } from "./components/MicPicker";
import { TraceLog } from "./components/TraceLog";
import { ThemeToggle } from "./components/ThemeToggle";
import { AudioPlayer, MicCapture, listMicrophones } from "./lib/audio";
import { useTheme } from "./lib/theme";
import type { ConnState, TraceEvent, TurnGroup, TurnState } from "./lib/types";

const FIELD =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition-colors focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800/80 dark:text-slate-100";
const FIELD_LABEL =
  "text-[11px] font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400";

const CONN_LABEL: Record<ConnState, string> = {
  idle: "Offline",
  connecting: "Connecting…",
  active: "Connected",
  error: "Error",
};

const VOICES = ["eve", "ara", "leo", "rex", "sal"]; // xAI Grok voices

export default function App() {
  const [token, setToken] = useState("static_token_abc");
  const [voice, setVoice] = useState(VOICES[0]);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [micId, setMicId] = useState<string>();
  const [conn, setConn] = useState<ConnState>("idle");
  const [turnState, setTurnState] = useState<TurnState>("idle");
  const [level, setLevel] = useState(0);
  const [turns, setTurns] = useState<TurnGroup[]>([]);
  const [openTurn, setOpenTurn] = useState<number | null>(null);
  const [companion, setCompanion] = useState("Companion");
  const { pref: themePref, setPref: setThemePref } = useTheme();

  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MicCapture | null>(null);
  const playerRef = useRef<AudioPlayer | null>(null);
  const sampleRateRef = useRef(24_000);
  const audioTurnRef = useRef(0);
  const turnStateRef = useRef<TurnState>("idle");
  const mutedRef = useRef(false); // half-duplex: mute mic while the reply plays

  useEffect(() => {
    listMicrophones().then(setDevices).catch(() => {});
  }, []);

  const setTurn = (s: TurnState) => {
    turnStateRef.current = s;
    setTurnState(s);
  };

  const upsertTurn = (index: number, mut: (t: TurnGroup) => void) =>
    setTurns((prev) => {
      const idx = prev.findIndex((x) => x.index === index);
      const t: TurnGroup =
        idx >= 0 ? { ...prev[idx] } : { index, events: [], heard: "", reply: "", audio: [] };
      mut(t);
      const next = [...prev];
      if (idx >= 0) next[idx] = t;
      else {
        next.push(t);
        next.sort((a, b) => a.index - b.index);
      }
      return next;
    });

  const handleTrace = (e: TraceEvent) => {
    upsertTurn(e.turn, (t) => {
      t.events = [...t.events, e];
      if (e.stage === "stt" && typeof e.data.text === "string") t.heard = e.data.text;
      if (e.stage === "response" && typeof e.data.text === "string") t.reply = e.data.text;
    });
    if (e.turn > 0) setOpenTurn(e.turn); // keep the newest turn expanded
    if (e.stage === "vad") setTurn("listening");
    if (["assembly", "router", "generation"].includes(e.stage)) setTurn("thinking");
    if (e.stage === "tts") {
      audioTurnRef.current = e.turn;
      setTurn("speaking");
    }
  };

  const connect = useCallback(() => {
    setTurns([]);
    setConn("connecting");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/voice`);
    ws.binaryType = "arraybuffer";
    playerRef.current = new AudioPlayer(
      (l) => {
        if (turnStateRef.current === "speaking") setLevel(l);
      },
      () => {
        // Reply finished playing → un-mute the mic and go back to listening.
        mutedRef.current = false;
        setLevel(0);
        setTurn("idle");
      },
    );

    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token, voice }));
    ws.onclose = () => stopLocalCapture();
    ws.onerror = () => setConn("error");
    ws.onmessage = async (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        mutedRef.current = true; // companion is speaking — don't capture the echo
        playerRef.current?.enqueue(ev.data);
        const turn = audioTurnRef.current;
        upsertTurn(turn, (t) => (t.audio = [...t.audio, ev.data]));
        return;
      }
      const msg = JSON.parse(ev.data);
      switch (msg.type) {
        case "ready":
          setConn("active");
          setCompanion(msg.companion_name || "Companion");
          sampleRateRef.current = msg.sample_rate ?? 24_000;
          playerRef.current?.configure(sampleRateRef.current);
          ws.send(JSON.stringify({ type: "start_conversation" }));
          await startCapture();
          break;
        case "error":
          setConn("error");
          break;
        case "trace":
          handleTrace(msg as TraceEvent);
          break;
        case "conversation_ended":
          setTurn("idle");
          break;
      }
    };
    wsRef.current = ws;
  }, [token, voice, micId]);

  const startCapture = async () => {
    micRef.current = new MicCapture();
    await micRef.current.start(
      micId,
      (pcm) => {
        // Half-duplex: stop sending mic audio while the reply is playing so the
        // companion never hears (and answers) its own voice (§19/§24 need AEC).
        if (mutedRef.current) return;
        if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(pcm);
      },
      (l) => !mutedRef.current && setLevel(l),
    );
    if (devices.length === 0) listMicrophones().then(setDevices).catch(() => {});
  };

  const stopLocalCapture = async () => {
    await micRef.current?.stop();
    micRef.current = null;
    setLevel(0);
    setTurn("idle");
    setConn("idle");
  };

  const stopConversation = async () => {
    wsRef.current?.send(JSON.stringify({ type: "stop_conversation" }));
    wsRef.current?.close();
    wsRef.current = null;
    await stopLocalCapture();
  };

  const replay = (turn: TurnGroup) =>
    playerRef.current?.replay(turn.audio, sampleRateRef.current);

  const active = conn === "active" || conn === "connecting";
  const dotColor =
    conn === "active"
      ? "bg-emerald-500"
      : conn === "error"
        ? "bg-red-500"
        : conn === "connecting"
          ? "bg-amber-500"
          : "bg-slate-400 dark:bg-slate-600";

  return (
    <div className="flex h-full bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-4 border-b border-slate-200 px-6 py-4 dark:border-slate-800">
          <div className="flex items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-fuchsia-500 text-white shadow-sm">
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
                <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4M8 22h8" />
              </svg>
            </div>
            <div>
              <h1 className="text-lg font-semibold leading-tight">{companion}</h1>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                Voice-first companion · Grok voice
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800/70">
              <span className={`h-2 w-2 rounded-full ${dotColor}`} />
              <span className="text-slate-600 dark:text-slate-300">{CONN_LABEL[conn]}</span>
            </div>
            <ThemeToggle pref={themePref} onChange={setThemePref} />
          </div>
        </header>

        <div className="relative flex flex-1 flex-col items-center justify-center gap-10 overflow-hidden px-6">
          {/* Soft backdrop wash */}
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_50%_at_50%_40%,rgba(99,102,241,0.08),transparent_70%)]" />
          <Orb state={turnState} level={level} />
        </div>

        {/* Bottom action bar */}
        <div className="border-t border-slate-200 bg-white/80 px-6 py-4 backdrop-blur dark:border-slate-800 dark:bg-slate-900/50">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end">
            <div className="grid flex-1 grid-cols-1 gap-3 sm:grid-cols-3">
              <label className="flex min-w-0 flex-col gap-1.5">
                <span className={FIELD_LABEL}>Token</span>
                <input
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  disabled={active}
                  className={FIELD}
                />
              </label>
              <MicPicker devices={devices} value={micId} onChange={setMicId} disabled={active} />
              <label className="flex min-w-0 flex-col gap-1.5">
                <span className={FIELD_LABEL}>Voice</span>
                <select
                  value={voice}
                  onChange={(e) => setVoice(e.target.value)}
                  disabled={active}
                  className={`${FIELD} capitalize`}
                >
                  {VOICES.map((v) => (
                    <option key={v} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <button
              onClick={active ? stopConversation : connect}
              className={`group flex shrink-0 items-center justify-center gap-2.5 rounded-xl px-8 py-3 text-sm font-semibold text-white shadow-lg transition-all active:scale-[0.98] lg:min-w-[13rem] ${
                active
                  ? "bg-rose-600 shadow-rose-600/25 hover:bg-rose-500"
                  : "bg-indigo-600 shadow-indigo-600/25 hover:bg-indigo-500"
              }`}
            >
              {active ? (
                <>
                  <span className="h-3 w-3 rounded-[3px] bg-white" />
                  Stop conversation
                </>
              ) : (
                <>
                  <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor">
                    <path d="M8 5v14l11-7z" />
                  </svg>
                  Start conversation
                </>
              )}
            </button>
          </div>
          <p className="mt-3 text-center text-xs text-slate-500 lg:text-left dark:text-slate-400">
            {active
              ? "Just talk — I take turns on my own. Talk over me to interrupt."
              : "Press start and speak naturally."}
          </p>
        </div>
      </main>

      <TraceLog
        turns={turns}
        openTurn={openTurn}
        onToggle={(i) => setOpenTurn((cur) => (cur === i ? null : i))}
        onReplay={replay}
      />
    </div>
  );
}
