import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Orb } from "../components/Orb";
import { MicPicker } from "../components/MicPicker";
import { TraceLog } from "../components/TraceLog";
import { ThemeToggle } from "../components/ThemeToggle";
import { ProfilePanel } from "../components/ProfilePanel";
import {
  AudioPlayer,
  MicCapture,
  listMicrophones,
  requestMicAccess,
} from "../lib/audio";
import { setEntered } from "../lib/session";
import { useTheme } from "../lib/theme";
import type { ConnState, TraceEvent, TurnGroup, TurnState } from "../lib/types";

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

// The companion route (/). Owns the real-time voice session: WebSocket
// connect/auth/start/stop, mic capture, full-duplex playback + barge-in, the
// talking orb, per-turn trace, and the profile panel.
export default function CompanionPage() {
  const navigate = useNavigate();
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
  const [profileOpen, setProfileOpen] = useState(false);
  const [traceOpen, setTraceOpen] = useState(false); // mobile trace drawer
  const { pref: themePref, setPref: setThemePref } = useTheme();

  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MicCapture | null>(null);
  const playerRef = useRef<AudioPlayer | null>(null);
  const sampleRateRef = useRef(24_000);
  const audioTurnRef = useRef(0);
  const turnStateRef = useRef<TurnState>("idle");

  useEffect(() => {
    // This route only mounts once the user has entered, so proactively prompt
    // for mic access on mount so device labels populate and capture works on
    // first Start. Silent if denied — MicPicker still explains how to grant it.
    requestMicAccess().then((granted) => {
      if (granted) listMicrophones().then(setDevices).catch(() => {});
    });
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
        // Reply finished playing → back to listening. Full-duplex: the mic never
        // stopped streaming, so barge-in stays available throughout.
        setLevel(0);
        setTurn("idle");
      },
    );

    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token, voice }));
    ws.onclose = () => stopLocalCapture();
    ws.onerror = () => setConn("error");
    ws.onmessage = async (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        // Full-duplex: enqueue the reply audio but keep the mic streaming so the
        // user can talk over it. Browser echoCancellation handles the echo.
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
          // Server detected an interruption → flush the companion's buffered
          // playback immediately so barge-in feels instant (§24).
          if ((msg as TraceEvent).stage === "barge_in") playerRef.current?.stop();
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
        // Full-duplex: always stream mic audio, even while the reply plays, so
        // the user can barge in (§24). Browser AEC suppresses the echo.
        if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(pcm);
      },
      // Don't let the mic level fight the companion's playback level on the orb.
      (l) => turnStateRef.current !== "speaking" && setLevel(l),
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

  const signOut = async () => {
    setProfileOpen(false);
    setTraceOpen(false);
    if (conn === "active" || conn === "connecting") await stopConversation();
    setTurns([]);
    setEntered(false);
    navigate("/login", { replace: true });
  };

  const active = conn === "active" || conn === "connecting";
  const realTurns = turns.filter((t) => t.index > 0).length;
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
        <header className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 sm:px-6 sm:py-4 dark:border-slate-800">
          <div className="flex min-w-0 items-center gap-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-fuchsia-500 text-white shadow-sm">
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
                <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4M8 22h8" />
              </svg>
            </div>
            <div className="min-w-0">
              <h1 className="truncate text-lg font-semibold leading-tight">{companion}</h1>
              <p className="truncate text-xs text-slate-500 dark:text-slate-400">
                Voice-first companion · Grok voice
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 sm:gap-3">
            {/* Links to the per-user data pages (real routes). */}
            <nav className="hidden items-center gap-1 text-xs sm:flex">
              <button onClick={() => navigate("/conversations")} className="rounded-md px-2 py-1 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800">Conversations</button>
              <button onClick={() => navigate("/memories")} className="rounded-md px-2 py-1 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800">Memories</button>
              <button onClick={() => navigate("/traces")} className="rounded-md px-2 py-1 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800">Traces</button>
            </nav>
            <div className="hidden items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs sm:flex dark:border-slate-700 dark:bg-slate-800/70">
              <span className={`h-2 w-2 rounded-full ${dotColor}`} />
              <span className="text-slate-600 dark:text-slate-300">{CONN_LABEL[conn]}</span>
            </div>
            {/* Trace drawer toggle — mobile only; the trace is a persistent sidebar on desktop. */}
            <button
              onClick={() => setTraceOpen(true)}
              aria-label="Open conversation trace"
              title="Conversation trace"
              className="relative grid h-9 w-9 shrink-0 place-items-center rounded-full border border-slate-200 text-slate-600 transition-colors hover:bg-slate-100 lg:hidden dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 6h16M4 12h10M4 18h7" />
              </svg>
              {realTurns > 0 && (
                <span className="absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-indigo-600 px-1 text-[10px] font-semibold leading-none text-white">
                  {realTurns}
                </span>
              )}
            </button>
            <ThemeToggle pref={themePref} onChange={setThemePref} />
            <span className="hidden h-6 w-px bg-slate-200 sm:block dark:bg-slate-800" />
            <button
              onClick={() => setProfileOpen(true)}
              aria-label="Open your profile"
              title="Your profile"
              className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-gradient-to-br from-indigo-500 to-fuchsia-500 text-xs font-semibold text-white shadow-sm outline-none ring-offset-2 ring-offset-white transition-transform hover:scale-105 focus-visible:ring-2 focus-visible:ring-indigo-500 dark:ring-offset-slate-950"
            >
              {avatarInitials(token)}
            </button>
          </div>
        </header>

        <div className="relative flex flex-1 flex-col items-center justify-center gap-10 overflow-hidden px-4 py-8 sm:px-6">
          {/* Soft backdrop wash */}
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_50%_at_50%_40%,rgba(99,102,241,0.08),transparent_70%)]" />
          <Orb state={turnState} level={level} />
        </div>

        {/* Bottom action bar */}
        <div className="border-t border-slate-200 bg-white/80 px-4 py-4 backdrop-blur sm:px-6 dark:border-slate-800 dark:bg-slate-900/50">
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
              className={`group flex shrink-0 items-center justify-center gap-2.5 rounded-xl px-8 py-3 text-sm font-semibold text-white shadow-md transition-all active:scale-[0.98] lg:min-w-[13rem] ${
                active
                  ? "bg-rose-600 shadow-rose-600/20 hover:bg-rose-500"
                  : "bg-indigo-600 shadow-indigo-600/20 hover:bg-indigo-500"
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
        mobileOpen={traceOpen}
        onCloseMobile={() => setTraceOpen(false)}
      />

      <ProfilePanel
        open={profileOpen}
        token={token}
        onClose={() => setProfileOpen(false)}
        onSignOut={signOut}
      />
    </div>
  );
}

// Two-letter avatar seed derived from the bearer token / demo user id.
function avatarInitials(token: string): string {
  const m = token.match(/[a-z0-9]+/gi);
  if (!m) return "U";
  const seed = m[m.length - 1];
  return seed.slice(0, 2).toUpperCase();
}
