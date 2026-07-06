import { useCallback, useEffect, useRef, useState } from "react";
import { Orb } from "./components/Orb";
import { MicPicker } from "./components/MicPicker";
import { TraceLog } from "./components/TraceLog";
import { AudioPlayer, MicCapture, listMicrophones } from "./lib/audio";
import type { ConnState, TraceEvent, TurnGroup, TurnState } from "./lib/types";

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

  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MicCapture | null>(null);
  const playerRef = useRef<AudioPlayer | null>(null);
  const sampleRateRef = useRef(24_000);
  const audioTurnRef = useRef(0);
  const turnStateRef = useRef<TurnState>("idle");

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
    playerRef.current = new AudioPlayer((l) => {
      if (turnStateRef.current === "speaking") setLevel(l);
    });

    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token, voice }));
    ws.onclose = () => stopLocalCapture();
    ws.onerror = () => setConn("error");
    ws.onmessage = async (ev) => {
      if (ev.data instanceof ArrayBuffer) {
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
      (pcm) => wsRef.current?.readyState === WebSocket.OPEN && wsRef.current.send(pcm),
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

  const active = conn === "active" || conn === "connecting";

  return (
    <div className="flex h-full bg-slate-950 text-slate-100">
      <main className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-800 px-6 py-4">
          <div>
            <h1 className="text-lg font-semibold">{companion}</h1>
            <p className="text-xs text-slate-500">Voice-first companion · Grok voice</p>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span
              className={`h-2 w-2 rounded-full ${
                conn === "active" ? "bg-emerald-400" : conn === "error" ? "bg-red-400" : "bg-slate-600"
              }`}
            />
            <span className="text-slate-400">{conn}</span>
          </div>
        </header>

        <div className="flex flex-1 flex-col items-center justify-center gap-10 px-6">
          <Orb state={turnState} level={level} />

          <button
            onClick={active ? stopConversation : connect}
            className={`rounded-full px-10 py-5 text-base font-medium shadow-lg transition-all ${
              active
                ? "bg-rose-600 text-white hover:bg-rose-500"
                : "bg-emerald-600 text-white hover:bg-emerald-500"
            }`}
          >
            {active ? "Stop conversation" : "Start conversation"}
          </button>
          <p className="-mt-4 text-xs text-slate-500">
            {active ? "Just talk — I take turns on my own. Talk over me to interrupt." : "Press start and speak naturally."}
          </p>
        </div>

        <footer className="flex flex-wrap items-end gap-4 border-t border-slate-800 px-6 py-4">
          <label className="flex flex-col gap-1.5 text-xs text-slate-400">
            <span className="uppercase tracking-wider">Token</span>
            <input
              value={token}
              onChange={(e) => setToken(e.target.value)}
              disabled={active}
              className="w-52 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm outline-none focus:border-indigo-500 disabled:opacity-50"
            />
          </label>
          <MicPicker devices={devices} value={micId} onChange={setMicId} disabled={active} />
          <label className="flex flex-col gap-1.5 text-xs text-slate-400">
            <span className="uppercase tracking-wider">Voice</span>
            <select
              value={voice}
              onChange={(e) => setVoice(e.target.value)}
              disabled={active}
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm capitalize outline-none focus:border-indigo-500 disabled:opacity-50"
            >
              {VOICES.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </label>
        </footer>
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
