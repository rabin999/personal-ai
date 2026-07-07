// Authed API client for the per-user pages (conversations / memories / traces /
// feedback). Uses the bearer token the app holds; every endpoint is user-scoped
// server-side. Thin wrappers — pagination + range filtering happen on the server.

import { getToken } from "./session";

async function authed<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      Authorization: `Bearer ${getToken()}`,
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

// ── conversations ────────────────────────────────────────────────────────
export interface ConversationHeader {
  session_id: string;
  turn_count: number;
  started_at_iso?: string;
  last_at_iso?: string;
  last_ts?: number;
}
export interface ConversationTurn {
  turn_index: number;
  user_text: string;
  assistant_text: string;
  created_at?: string;
}

export function listConversations(params: {
  offset?: number;
  limit?: number;
  from?: string;
  to?: string;
}): Promise<{ total: number; offset: number; limit: number; conversations: ConversationHeader[] }> {
  const q = new URLSearchParams();
  if (params.offset) q.set("offset", String(params.offset));
  if (params.limit) q.set("limit", String(params.limit));
  if (params.from) q.set("from", params.from);
  if (params.to) q.set("to", params.to);
  return authed(`/api/conversations?${q.toString()}`);
}

export function getConversation(
  sessionId: string,
): Promise<{ session_id: string; turns: ConversationTurn[] }> {
  return authed(`/api/conversations/${encodeURIComponent(sessionId)}`);
}

// ── memories ─────────────────────────────────────────────────────────────
export interface SemanticItem { fact: string; valid_from?: string | null; valid_to?: string | null }
export interface EpisodicItem { id: string; text: string; timestamp?: string; session_id?: string }
export interface ProceduralItem {
  id: string; rule: string; trigger: string; confidence: number; evidence_count: number;
}

export function getSemanticMemories(): Promise<{ items: SemanticItem[] }> {
  return authed("/api/memories/semantic");
}
export function getEpisodicMemories(
  offset = 0,
  limit = 30,
): Promise<{ total: number; items: EpisodicItem[] }> {
  return authed(`/api/memories/episodic?offset=${offset}&limit=${limit}`);
}
export function getProceduralMemories(): Promise<{ items: ProceduralItem[] }> {
  return authed("/api/memories/procedural");
}
export function deleteEpisodicMemory(id: string): Promise<{ deleted: string }> {
  return authed(`/api/memories/episodic/${encodeURIComponent(id)}`, { method: "DELETE" });
}
export function correctSemanticFact(fact: string): Promise<{ recorded: string }> {
  return authed("/api/memories/semantic", { method: "POST", body: JSON.stringify({ fact }) });
}

// ── traces ───────────────────────────────────────────────────────────────
export interface TraceEvent {
  turn: number; stage: string; message: string; level?: string; ts?: number;
  data?: Record<string, unknown>;
}
export function listTraceSessions(): Promise<{ sessions: { session_id: string; last_ts: number }[] }> {
  return authed("/debug/traces");
}
export function getSessionTrace(sessionId: string): Promise<{ events: TraceEvent[] }> {
  return authed(`/debug/traces/${encodeURIComponent(sessionId)}`);
}

// ── feedback ─────────────────────────────────────────────────────────────
export function sendFeedback(body: {
  session_id: string; rating: "up" | "down"; turn_id?: string; note?: string;
}): Promise<{ id: string; rating: string }> {
  return authed("/api/feedback", { method: "POST", body: JSON.stringify(body) });
}
