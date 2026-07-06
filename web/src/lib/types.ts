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
  | "tts"
  | "barge_in"
  | "error";

export interface TraceEvent {
  session_id: string;
  ts: number;
  stage: Stage;
  message: string;
  level: "info" | "debug" | "warn" | "error";
  data: Record<string, unknown>;
}

export type ConnState = "idle" | "connecting" | "ready" | "error";
export type TurnState = "idle" | "listening" | "thinking" | "speaking";
