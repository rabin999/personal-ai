import { useCallback, useEffect, useRef, useState } from "react";
import { Orb } from "./components/Orb";
import { MicPicker } from "./components/MicPicker";
import { TraceLog } from "./components/TraceLog";
import { AudioPlayer, MicCapture, listMicrophones } from "./lib/audio";
import type { ConnState, TraceEvent, TurnState } from "./lib/types";

const VOICES = ["alloy", "verse", "aria", "sage"];

export default function App() {
  const [token, setToken] = useState("static_token_abc");
  const [voice, setVoice] = useState(VOICES[0]);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [micId, setMicId] = useState<string>();
  const [conn, setConn] = useState<ConnState>("idle");
  const [turn, setTurn] = useState<TurnState>("idle");
  const [level, setLevel] = useState(0);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [reply, setReply] = useState("");
  const [heard, setHeard] = useState("");
  const [companion, setCompanion] = useState("Companion");

  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MicCapture | null>(null);
  const playerRef = useRef<AudioPlayer | null>(null);
  const talkingRef = useRef(false);

  useEffect(() => {
    listMicrophones().then(setDevices).catch(() => {});
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current) wsRef.current.close();
    setConn("connecting");
    setEvents([]);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/voice`);
    ws.binaryType = "arraybuffer";
    playerRef.current = new AudioPlayer((l) => {
      if (turnRef.current === "speaking") setLevel(l);
    });

    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token, voice }));
    ws.onclose = () => setConn("idle");
    ws.onerror = () => setConn("error");
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        playerRef.current?.enqueue(ev.data);
        return;
      }
      const msg = JSON.parse(ev.data);
      switch (msg.type) {
        case "ready":
          setConn("ready");
          setCompanion(msg.companion_name || "Companion");
          break;
        case "error":
          setConn("error");
          pushEvent({ stage: "error", message: msg.message, level: "error" });
          break;
        case "trace":
          handleTrace(msg as TraceEvent);
          break;
        case "audio_start":
          playerRef.current?.configure(msg.sample_rate);
          setTurnState("speaking");
          break;
        case "turn_end":
          setTurnState("idle");
          setLevel(0);
          break;
      }
    };
    wsRef.current = ws;
  }, [token, voice]);

  // Keep a ref of turn state so audio callbacks can read it without re-binding.
  const turnRef = useRef<TurnState>("idle");
  const setTurnState = (s: TurnState) => {
    turnRef.current = s;
    setTurn(s);
  };

  const pushEvent = (e: Partial<TraceEvent> & { stage: TraceEvent["stage"]; message: string }) =>
    setEvents((prev) => [
      ...prev,
      { session_id: "", ts: Date.now() / 1000, level: "info", data: {}, ...e } as TraceEvent,
    ]);

  const handleTrace = (e: TraceEvent) => {
    setEvents((prev) => [...prev, e]);
    if (e.stage === "stt" && typeof e.data.text === "string") setHeard(e.data.text);
    if (e.stage === "response" && typeof e.data.text === "string") setReply(e.data.text);
    if (["assembly", "router", "generation"].includes(e.stage)) setTurnState("thinking");
  };

  const startTalking = async () => {
    if (conn !== "ready" || talkingRef.current) return;
    talkingRef.current = true;
    playerRef.current?.stop(); // barge-in if the companion is mid-sentence
    setReply("");
    setHeard("");
    setTurnState("listening");
    wsRef.current?.send(JSON.stringify({ type: "start" }));
    micRef.current = new MicCapture();
    await micRef.current.start(
      micId,
      (pcm) => wsRef.current?.readyState === WebSocket.OPEN && wsRef.current.send(pcm),
      (l) => turnRef.current === "listening" && setLevel(l),
    );
    if (devices.length === 0) listMicrophones().then(setDevices).catch(() => {});
  };

  const stopTalking = async () => {
    if (!talkingRef.current) return;
    talkingRef.current = false;
    wsRef.current?.send(JSON.stringify({ type: "stop" }));
    await micRef.current?.stop();
    micRef.current = null;
    setLevel(0);
    setTurnState("thinking");
  };

  const ready = conn === "ready";

  return (
    <div className="flex h-full bg-slate-950 text-slate-100">
      <main className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-800 px-6 py-4">
          <div>
            <h1 className="text-lg font-semibold">{companion}</h1>
            <p className="text-xs text-slate-500">Voice-first personal companion</p>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span
              className={`h-2 w-2 rounded-full ${
                ready ? "bg-emerald-400" : conn === "error" ? "bg-red-400" : "bg-slate-600"
              }`}
            />
            <span className="text-slate-400">{conn}</span>
          </div>
        </header>

        <div className="flex flex-1 flex-col items-center justify-center gap-10 px-6">
          <Orb state={turn} level={level} />

          <div className="h-16 max-w-xl text-center">
            {heard && <p className="text-sm text-slate-500">you: “{heard}”</p>}
            {reply && <p className="mt-1 text-lg text-slate-100">{reply}</p>}
          </div>

          <button
            disabled={!ready}
            onMouseDown={startTalking}
            onMouseUp={stopTalking}
            onMouseLeave={stopTalking}
            onTouchStart={(e) => {
              e.preventDefault();
              startTalking();
            }}
            onTouchEnd={(e) => {
              e.preventDefault();
              stopTalking();
            }}
            className={`rounded-full px-10 py-5 text-base font-medium shadow-lg transition-all ${
              talkingRef.current
                ? "scale-105 bg-emerald-500 text-white"
                : "bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-40"
            }`}
          >
            {talkingRef.current ? "Listening — release to send" : "Hold to talk"}
          </button>
        </div>

        <footer className="flex flex-wrap items-end gap-4 border-t border-slate-800 px-6 py-4">
          <label className="flex flex-col gap-1.5 text-xs text-slate-400">
            <span className="uppercase tracking-wider">Token</span>
            <input
              value={token}
              onChange={(e) => setToken(e.target.value)}
              disabled={ready}
              className="w-52 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm outline-none focus:border-indigo-500 disabled:opacity-50"
            />
          </label>
          <MicPicker devices={devices} value={micId} onChange={setMicId} disabled={ready} />
          <label className="flex flex-col gap-1.5 text-xs text-slate-400">
            <span className="uppercase tracking-wider">Voice</span>
            <select
              value={voice}
              onChange={(e) => setVoice(e.target.value)}
              disabled={ready}
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm outline-none focus:border-indigo-500 disabled:opacity-50"
            >
              {VOICES.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <button
            onClick={ready ? () => wsRef.current?.close() : connect}
            className="ml-auto rounded-lg bg-slate-800 px-5 py-2.5 text-sm font-medium hover:bg-slate-700"
          >
            {ready ? "Disconnect" : "Connect"}
          </button>
        </footer>
      </main>

      <TraceLog events={events} />
    </div>
  );
}
