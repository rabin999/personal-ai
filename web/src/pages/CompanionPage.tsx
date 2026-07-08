import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Waveform } from "../components/Waveform";
import { MicPicker } from "../components/MicPicker";
import { ModelPicker } from "../components/ModelPicker";
import { TraceLog } from "../components/TraceLog";
import { AppHeader } from "../components/AppHeader";
import { ProfilePanel } from "../components/ProfilePanel";
import {
  AudioPlayer,
  MicCapture,
  listMicrophones,
} from "../lib/audio";
import { fetchMe, logout, type Me } from "../lib/session";
import {
  getModels,
  getVoices,
  type VoiceItem,
  sendFeedback,
  setModel as saveModel,
  setReasoningModel as saveReasoningModel,
  setVoiceEngine as saveVoiceEngine,
} from "../lib/api";
import type { ConnState, TraceEvent, TurnGroup, TurnState } from "../lib/types";

const FIELD =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition-colors focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800/80 dark:text-slate-100";
const FIELD_LABEL =
  "text-[11px] font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400";

const CONN_LABEL: Record<ConnState, string> = {
  idle: "Offline",
  connecting: "Connecting…",
  active: "Connected",
  error: "Error",
};

const DEFAULT_VOICE = "orion"; // natural, less-warm default (26 available, fetched live)

// Live caption is a single-line ticker: only the last N words (yours or the
// reply's) are shown at once, so it never wraps or grows into a paragraph.
const CAPTION_WINDOW = 8;

// The companion route (/). Owns the real-time voice session: WebSocket
// connect/auth/start/stop, mic capture, full-duplex playback + barge-in, the
// talking orb, per-turn trace, and the profile panel.
export default function CompanionPage() {
  const navigate = useNavigate();
  const [me, setMe] = useState<Me | null>(null);
  const [voice, setVoice] = useState(DEFAULT_VOICE);
  const [voices, setVoices] = useState<VoiceItem[]>([]);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [micId, setMicId] = useState<string>();
  const [conn, setConn] = useState<ConnState>("idle");
  const [turnState, setTurnState] = useState<TurnState>("idle");
  const [level, setLevel] = useState(0);
  const [turns, setTurns] = useState<TurnGroup[]>([]);
  const [openTurn, setOpenTurn] = useState<number | null>(null);
  const [profileOpen, setProfileOpen] = useState(false);
  const [traceOpen, setTraceOpen] = useState(false); // mobile trace drawer
  const [showSetup, setShowSetup] = useState(false); // mobile: collapse config so the orb + Start are the hero
  const [micMuted, setMicMuted] = useState(false); // user-controlled mic mute
  const [caption, setCaption] = useState(""); // live subtitle: your words / the reply

  const micMutedRef = useRef(false);
  // Word-by-word reveal timer for the reply caption (cleared on new turn/stop).
  const captionIvRef = useRef<number | null>(null);
  // Set when the user said goodbye: the conversation ends once the companion's
  // farewell finishes playing (see the player's onEnded), so the button returns
  // to "Start conversation" instead of stopping mid-sentence.
  const pendingEndRef = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MicCapture | null>(null);
  const playerRef = useRef<AudioPlayer | null>(null);
  const sampleRateRef = useRef(24_000);
  const audioTurnRef = useRef(0);
  const sessionIdRef = useRef("");
  // Voice runtime to A/B before starting: "native" (our asyncio loop) or
  // "pipecat" (framework VAD/endpointing/barge-in). Read at connect time.
  const [runtime, setRuntime] = useState<"native" | "pipecat">("native");
  const runtimeRef = useRef(runtime);
  runtimeRef.current = runtime;
  // Fast/flash LLM the user can pick (§4). Empty = the tier default.
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel_] = useState<string>("");
  // Full live catalog (all models) so the picker can search everything, not just the
  // configured few. Falls back to the curated choices if the catalog is empty.
  const [catalog, setCatalog] = useState<string[]>([]);
  // F8: the mature "thinking"/reasoning model for the main turn. Empty = default.
  const [reasoningModels, setReasoningModels] = useState<string[]>([]);
  const [reasoningModel, setReasoningModel_] = useState<string>("");
  useEffect(() => {
    getModels()
      .then((m) => {
        setModels(m.choices);
        setCatalog(m.catalog?.length ? m.catalog : m.choices);
        setModel_(m.selected ?? "");
        setReasoningModels(m.reasoning_choices ?? []);
        setReasoningModel_(m.reasoning_model ?? "");
        // §11: restore the persisted voice engine so the client reconnects to
        // the same runtime the user last chose.
        if (m.voice_engine === "native" || m.voice_engine === "pipecat") {
          setRuntime(m.voice_engine);
        }
      })
      .catch(() => {});
  }, []);
  // Tear down the caption reveal timer on unmount.
  useEffect(() => () => {
    if (captionIvRef.current !== null) clearInterval(captionIvRef.current);
  }, []);
  // Load the full live voice roster (#19) for the picker.
  useEffect(() => {
    getVoices().then((r) => setVoices(r.voices)).catch(() => {});
  }, []);
  const turnStateRef = useRef<TurnState>("idle");

  useEffect(() => {
    // Populate the device list WITHOUT opening the mic. Opening getUserMedia on
    // page load put phones/tablets into COMMUNICATION (call) audio mode, so the
    // hardware volume keys controlled the call stream instead of media — even
    // before starting a conversation. enumerateDevices() doesn't trigger that;
    // the actual mic (and its call-mode) opens only on the Start gesture.
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

  const stopCaptionReveal = () => {
    if (captionIvRef.current !== null) {
      clearInterval(captionIvRef.current);
      captionIvRef.current = null;
    }
  };

  // Reveal the reply word-by-word (~speaking pace) as a single-line ticker: show
  // only the trailing CAPTION_WINDOW words being spoken now, not the whole
  // growing sentence, so the caption stays one line and reads as live captions.
  const revealCaption = (full: string) => {
    stopCaptionReveal();
    const words = full.split(/\s+/).filter(Boolean);
    if (words.length === 0) return;
    let i = 1;
    setCaption(words[0]);
    captionIvRef.current = window.setInterval(() => {
      if (i >= words.length) {
        stopCaptionReveal();
        return;
      }
      i += 1;
      setCaption(words.slice(Math.max(0, i - CAPTION_WINDOW), i).join(" "));
    }, 170);
  };

  const connect = useCallback(() => {
    setTurns([]);
    setConn("connecting");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const path = runtimeRef.current === "pipecat" ? "/ws/voice-pipecat" : "/ws/voice";
    const ws = new WebSocket(`${proto}://${location.host}${path}`);
    ws.binaryType = "arraybuffer";
    // Release the previous connection's playback context + hidden speaker-routing
    // <audio> element before opening a new one (avoid accumulation on reconnect).
    void playerRef.current?.close();
    playerRef.current = new AudioPlayer(
      (l) => {
        // Drive the waveform from the companion's TTS while it speaks. `l` is the
        // analyser's frame-rate level (already scaled to the mic's range), so the
        // bar reacts to voice output as strongly as to voice input.
        if (turnStateRef.current === "speaking") setLevel(l);
      },
      () => {
        // Reply finished playing → back to listening. Full-duplex: the mic never
        // stopped streaming, so barge-in stays available throughout.
        setLevel(0);
        setTurn("idle");
        // If the user said goodbye, end the conversation now that the companion's
        // farewell has finished playing → the button returns to "Start".
        if (pendingEndRef.current) {
          pendingEndRef.current = false;
          void stopConversation();
        }
      },
    );

    // Identity rides the session cookie sent on the WS handshake (Google SSO);
    // the first message only carries the voice selection.
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", voice }));
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
          // No static welcome text: the companion SPEAKS a fresh, dynamic greeting the
          // moment the conversation opens, and its words arrive as a "response" caption
          // (below). Clear any stale caption until that lands (no hard-coded UI copy).
          setCaption("");
          sessionIdRef.current = msg.session_id || "";
          sampleRateRef.current = msg.sample_rate ?? 24_000;
          playerRef.current?.configure(sampleRateRef.current);
          playerRef.current?.setSpeed(msg.voice_speed ?? 1.0); // C7: per-user rate
          ws.send(JSON.stringify({ type: "start_conversation" }));
          await startCapture();
          break;
        case "error":
          setConn("error");
          break;
        case "trace": {
          const ev2 = msg as TraceEvent;
          // Server detected an interruption → stop playback AND drop any trailing
          // audio for the interrupted reply until the next one starts (§24 / C2).
          // Server buffering + the WS/OS send buffer can deliver already-synthesized
          // audio just after this signal; a plain stop() would let the next chunk
          // re-schedule playback ("the voice keeps playing"). interrupt() mutes it.
          if (ev2.stage === "barge_in") {
            playerRef.current?.interrupt();
            stopCaptionReveal();
            setCaption(""); // interrupted → drop the stale reply caption
          }
          // The next reply is synthesizing → accept its audio again.
          if (ev2.stage === "tts") playerRef.current?.resume();
          // User said "bye"/"close the conversation": end once the farewell plays out.
          if (ev2.data?.end_conversation) pendingEndRef.current = true;
          // Live caption (12px, under the animation): show your words as STT
          // finalizes them, then reveal the reply word-by-word as it's spoken.
          if (ev2.stage === "vad") {
            stopCaptionReveal();
            setCaption(""); // fresh turn: clear until STT lands
          }
          if (ev2.stage === "stt" && typeof ev2.data?.text === "string") {
            stopCaptionReveal();
            // Same single-line ticker for your words: show only the trailing window.
            setCaption(tailWords(cleanCaption(ev2.data.text as string)));
          }
          if (ev2.stage === "response" && !ev2.data?.delivered) {
            revealCaption(cleanCaption((ev2.data?.voice_text as string) ?? ev2.message ?? ""));
          }
          handleTrace(ev2);
          break;
        }
        case "conversation_ended":
          setTurn("idle");
          break;
      }
    };
    wsRef.current = ws;
  }, [voice, micId]);

  // Live voice-speed: when the profile slider changes it, apply to the RUNNING
  // player immediately (mid-conversation), not just on the next connect.
  useEffect(() => {
    const onSpeed = (e: Event) => {
      const v = (e as CustomEvent<number>).detail;
      if (typeof v === "number") playerRef.current?.setSpeed(v);
    };
    window.addEventListener("asaathi:voice-speed", onSpeed);
    return () => window.removeEventListener("asaathi:voice-speed", onSpeed);
  }, []);

  // Load the signed-in user for the header avatar; no session → back to login.
  useEffect(() => {
    fetchMe().then((u) => {
      if (u) setMe(u);
      else navigate("/login", { replace: true });
    });
  }, [navigate]);

  const startCapture = async () => {
    micRef.current = new MicCapture();
    await micRef.current.start(
      micId,
      (pcm) => {
        // FULL-DUPLEX (§24 barge-in): keep streaming the mic to the server WHILE
        // the companion is speaking, so a user talking over the reply is actually
        // heard and can interrupt. (The old half-duplex gate — muting the mic
        // during playback — is exactly what made barge-in impossible: the server
        // never saw the interrupting speech.) Browser echo cancellation
        // (getUserMedia echoCancellation:true) keeps the mic from hearing our own
        // TTS, and the server requires sustained *fresh* speech (_BARGE_IN_FRAMES,
        // ~256ms) so a brief residual-echo blip can't self-interrupt the reply.
        if (!micMutedRef.current && wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(pcm);
        }
      },
      // Don't let the mic level fight the companion's playback level on the orb.
      (l) => turnStateRef.current !== "speaking" && !micMutedRef.current && setLevel(l),
    );
    if (devices.length === 0) listMicrophones().then(setDevices).catch(() => {});
  };

  const stopLocalCapture = async () => {
    await micRef.current?.stop();
    micRef.current = null;
    micMutedRef.current = false;
    setMicMuted(false);
    setLevel(0);
    setTurn("idle");
    setConn("idle");
  };

  const stopConversation = async () => {
    // Stop the OUTGOING AUDIO immediately (buffered playback was draining for a
    // beat after Stop) + stop sending mic, THEN tell the server + close the socket.
    playerRef.current?.interrupt(); // flush + mute playback now
    micRef.current?.stop().catch(() => {});
    stopCaptionReveal();
    setCaption("");
    setLevel(0);
    setTurn("idle");
    try {
      wsRef.current?.send(JSON.stringify({ type: "stop_conversation" }));
    } catch {
      /* socket may already be closing */
    }
    wsRef.current?.close();
    wsRef.current = null;
    await stopLocalCapture();
  };

  const toggleMute = () => {
    const next = !micMutedRef.current;
    micMutedRef.current = next;
    setMicMuted(next);
    if (next) setLevel(0);
  };

  const replay = (turn: TurnGroup) =>
    playerRef.current?.replay(turn.audio, sampleRateRef.current);

  const signOut = async () => {
    setProfileOpen(false);
    setTraceOpen(false);
    if (conn === "active" || conn === "connecting") await stopConversation();
    setTurns([]);
    await logout(); // clears the server session, then navigates to /login
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
    <div className="flex h-full flex-col bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      {/* Full-width app header (F10), then the body row: orb + trace sidebar under it,
          so the trace lives INSIDE the page body on the right, not as a detached column. */}
      <AppHeader
          right={
            <div className="flex items-center gap-2">
              <div className="hidden items-center gap-2 rounded-full border border-neutral-200 bg-white px-2.5 py-1 text-xs sm:flex dark:border-neutral-700 dark:bg-neutral-800/70">
                <span className={`h-2 w-2 rounded-full ${dotColor}`} />
                <span className="text-neutral-600 dark:text-neutral-300">{CONN_LABEL[conn]}</span>
              </div>
              <button
                onClick={() => setTraceOpen(true)}
                aria-label="Open conversation trace"
                title="Conversation trace"
                className="relative grid h-9 w-9 shrink-0 place-items-center rounded-full border border-neutral-200 text-neutral-600 transition-colors hover:bg-neutral-100 lg:hidden dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
              >
                <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M4 6h16M4 12h10M4 18h7" />
                </svg>
                {realTurns > 0 && (
                  <span className="absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-sky-600 px-1 text-[10px] font-semibold leading-none text-white">
                    {realTurns}
                  </span>
                )}
              </button>
              <button
                onClick={() => setProfileOpen(true)}
                aria-label="Open your profile"
                title="Your profile"
                className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-gradient-to-br from-sky-500 to-cyan-500 text-xs font-semibold text-white shadow-sm outline-none transition-transform hover:scale-105 focus-visible:ring-2 focus-visible:ring-sky-500"
              >
                {me?.picture ? (
                  <img
                    src={me.picture}
                    alt=""
                    referrerPolicy="no-referrer"
                    className="h-full w-full rounded-full object-cover"
                  />
                ) : (
                  avatarInitials(me?.name || me?.email || "U")
                )}
              </button>
            </div>
          }
        />

      <div className="flex min-h-0 flex-1">
        <main className="flex min-w-0 flex-1 flex-col">
        <div className="relative flex flex-1 flex-col items-center justify-center gap-10 overflow-hidden px-4 py-8 sm:px-6">
          {/* Soft backdrop wash */}
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_50%_at_50%_40%,rgba(14,165,233,0.08),transparent_70%)]" />
          <StatusChip state={turnState} />
          {/* Single hero voice-bar (replaces the orb): reacts to your voice while
              listening AND the companion's TTS while speaking, colour-coded by
              state. Shown once connected; a flat idle line before that. */}
          <Waveform level={level} state={conn === "active" ? turnState : "idle"} />
          {/* Live caption: your words while listening, the reply revealed
              word-by-word while speaking. 12px, muted. Pinned to the bottom of the
              animation panel so it's always visible (mobile + desktop), never
              pushed below the fold by the orb. */}
          {conn === "active" && caption && (
            <p className="pointer-events-none absolute bottom-4 left-1/2 z-10 max-w-[92%] -translate-x-1/2 truncate text-center text-xs leading-relaxed text-slate-500 sm:max-w-xl dark:text-slate-400">
              {caption}
            </p>
          )}
        </div>

        {/* Bottom action bar */}
        <div className="border-t border-slate-200 bg-white/80 px-4 py-4 backdrop-blur sm:px-6 dark:border-slate-800 dark:bg-slate-900/50">
          {/* Mobile: collapse the config behind a Setup toggle so the orb + Start
              are the hero. Desktop always shows it inline. */}
          {!active && (
            <button
              onClick={() => setShowSetup((v) => !v)}
              className="mb-3 flex w-full items-center justify-between rounded-lg border border-slate-200 px-3 py-2.5 text-sm text-slate-600 lg:hidden dark:border-slate-800 dark:text-slate-300"
            >
              <span>
                Setup · <span className="font-medium capitalize">{voice}</span> ·{" "}
                {runtime === "pipecat" ? "Pipecat" : "Native"}
              </span>
              <svg
                viewBox="0 0 24 24"
                className={`h-4 w-4 transition-transform ${showSetup ? "rotate-180" : ""}`}
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              >
                <path d="M6 9l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end">
            <div
              className={`min-w-0 flex-1 grid-cols-1 gap-3 sm:grid-cols-2 lg:grid ${showSetup && !active ? "grid" : "hidden lg:grid"}`}
            >
              <MicPicker devices={devices} value={micId} onChange={setMicId} disabled={active} />
              <label className="flex min-w-0 flex-col gap-1.5">
                <span className={FIELD_LABEL}>Voice</span>
                <select
                  value={voice}
                  onChange={(e) => setVoice(e.target.value)}
                  disabled={active}
                  className={FIELD}
                >
                  {(voices.length ? voices : [{ voice_id: DEFAULT_VOICE, name: "Orion", gender: "" }]).map((v) => (
                    <option key={v.voice_id} value={v.voice_id}>
                      {v.name}{v.gender ? ` (${v.gender})` : ""}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex min-w-0 flex-col gap-1.5">
                <span className={FIELD_LABEL}>Voice engine</span>
                <select
                  value={runtime}
                  onChange={(e) => {
                    const eng = e.target.value as "native" | "pipecat";
                    setRuntime(eng);
                    void saveVoiceEngine(eng).catch(() => {}); // §11: persist the choice
                  }}
                  disabled={active}
                  className={FIELD}
                  title="Switch between the native asyncio runtime and the Pipecat pipeline (VAD/barge-in). Choose before starting."
                >
                  <option value="native">Native</option>
                  <option value="pipecat">Pipecat</option>
                </select>
              </label>
              <label className="flex min-w-0 flex-col gap-1.5">
                <span className={FIELD_LABEL}>Fast model</span>
                <ModelPicker
                  value={model}
                  options={catalog.length ? catalog : models}
                  onChange={(v) => {
                    setModel_(v);
                    void saveModel(v || null);
                  }}
                  title="Search any model (gemini-2.5-flash is the default). Applies from the next turn."
                />
              </label>
              <label className="flex min-w-0 flex-col gap-1.5">
                <span className={FIELD_LABEL}>Thinking model</span>
                <ModelPicker
                  value={reasoningModel}
                  options={catalog.length ? catalog : reasoningModels}
                  onChange={(v) => {
                    setReasoningModel_(v);
                    void saveReasoningModel(v || null).catch(() => {});
                  }}
                  title="The mature model for the main reasoning turn. Applies from the next turn; shown in the trace."
                />
              </label>
            </div>

            <div className="flex shrink-0 items-center gap-2">
              {active && (
                <button
                  onClick={toggleMute}
                  aria-label={micMuted ? "Unmute microphone" : "Mute microphone"}
                  title={micMuted ? "Unmute mic" : "Mute mic"}
                  className={`grid h-12 w-12 shrink-0 place-items-center rounded-xl border transition-colors ${
                    micMuted
                      ? "border-rose-300 bg-rose-50 text-rose-600 dark:border-rose-500/40 dark:bg-rose-950/40 dark:text-rose-300"
                      : "border-slate-200 text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                  }`}
                >
                  {micMuted ? (
                    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M2 2l20 20M9 9v3a3 3 0 0 0 5.1 2.1M15 9.3V5a3 3 0 0 0-5.9-.7M17 11v1a5 5 0 0 1-.4 2M12 19v3" />
                    </svg>
                  ) : (
                    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
                      <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4M8 22h8" />
                    </svg>
                  )}
                </button>
              )}
              <button
                onClick={active ? stopConversation : connect}
                className={`group flex shrink-0 items-center justify-center gap-2.5 rounded-xl px-8 py-3 text-sm font-semibold text-white shadow-md transition-all active:scale-[0.98] lg:min-w-[13rem] ${
                  active
                    ? "bg-rose-600 shadow-rose-600/20 hover:bg-rose-500"
                    : "bg-sky-600 shadow-sky-600/20 hover:bg-sky-500"
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
          </div>
          {/* Only surface the non-obvious bit (barge-in); no filler when idle. */}
          {active && (
            <p className="mt-3 text-center text-xs text-slate-500 lg:text-left dark:text-slate-400">
              Talk over me any time to interrupt.
            </p>
          )}
        </div>
      </main>

      <TraceLog
        turns={turns}
        openTurn={openTurn}
        onToggle={(i) => setOpenTurn((cur) => (cur === i ? null : i))}
        onReplay={replay}
        onFeedback={(turn, rating, note) => {
          if (!sessionIdRef.current) return;
          void sendFeedback({
            session_id: sessionIdRef.current,
            turn_id: String(turn.index),
            rating,
            note,
          });
        }}
        mobileOpen={traceOpen}
        onCloseMobile={() => setTraceOpen(false)}
      />
      </div>

      <ProfilePanel
        open={profileOpen}
        onClose={() => setProfileOpen(false)}
        onSignOut={signOut}
      />
    </div>
  );
}

// Live turn status, pinned to the top-right of the conversation body (out of the
// way of the flame) rather than sitting on top of the animation.
const STATUS: Record<TurnState, { label: string; dot: string }> = {
  idle: { label: "Ready", dot: "#38bdf8" },
  listening: { label: "Listening", dot: "#34d399" },
  thinking: { label: "Thinking", dot: "#a78bfa" },
  speaking: { label: "Speaking", dot: "#22d3ee" },
};

function StatusChip({ state }: { state: TurnState }) {
  const s = STATUS[state];
  return (
    <div className="pointer-events-none absolute right-4 top-4 z-10 flex items-center gap-2 rounded-full border border-slate-200/70 bg-white/70 px-3 py-1.5 text-sm font-medium text-slate-600 backdrop-blur-md sm:right-6 dark:border-slate-700/60 dark:bg-slate-900/50 dark:text-slate-200">
      <span
        className={`h-2 w-2 rounded-full ${state !== "idle" ? "animate-pulse" : ""}`}
        style={{ background: s.dot, boxShadow: `0 0 8px ${s.dot}` }}
      />
      {s.label}
    </div>
  );
}

// Caption text for display: drop inline TTS tags (e.g. "[warm]") and markdown
// emphasis, collapse whitespace. What's shown is what's spoken/heard, nothing else.
function cleanCaption(text: string): string {
  // Strip BOTH inline TTS tag forms the backend emits — [square] and <angle>
  // (see _BRACKET_TOKEN in core/reasoning/response_gen.py). voice_text keeps the
  // tags for the voice, so the caption must remove them itself.
  return (text ?? "")
    .replace(/\[[^\[\]]*\]|<[^<>]*>/g, "")
    .replace(/[*_`]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

// Keep only the trailing CAPTION_WINDOW words so the caption stays a single line.
function tailWords(text: string, n = CAPTION_WINDOW): string {
  const w = text.split(/\s+/).filter(Boolean);
  return w.slice(Math.max(0, w.length - n)).join(" ");
}

// Two-letter avatar seed derived from the bearer token / demo user id.
function avatarInitials(token: string): string {
  const m = token.match(/[a-z0-9]+/gi);
  if (!m) return "U";
  const seed = m[m.length - 1];
  return seed.slice(0, 2).toUpperCase();
}
