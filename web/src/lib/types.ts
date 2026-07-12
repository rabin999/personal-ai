export type Stage =
  | "session"
  | "vad"
  | "stt"
  | "endpoint"
  | "emotion"
  | "assembly"
  | "router"
  | "generation"
  | "response"
  | "reply_chunk"
  | "tts"
  | "barge_in"
  | "error";

export interface TraceEvent {
  session_id: string;
  turn: number;
  ts: number;
  stage: Stage;
  message: string;
  level: "info" | "debug" | "warn" | "error";
  data: Record<string, unknown>;
}

export interface TurnGroup {
  index: number; // 0 = pre-conversation/listening events
  events: TraceEvent[];
  heard: string; // what STT transcribed
  reply: string; // the companion's reply text
  streamed?: boolean; // reply was built from progressive reply_chunk events (== spoken)
  audio: ArrayBuffer[]; // collected TTS PCM chunks for replay
}

export type ConnState = "idle" | "connecting" | "active" | "error";
export type TurnState = "idle" | "listening" | "thinking" | "speaking";
