// Authed API client for the per-user pages (conversations / memories / traces /
// feedback). Auth is the session cookie (Google SSO) — sent automatically with
// credentials:"include"; no bearer token. A 401 means the session lapsed → bounce
// to the login screen. Thin wrappers — pagination/filtering happen server-side.

async function authed<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("401 Unauthorized");
  }
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
  first_message?: string; // F11: first user message, for the list preview
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
export interface PersonaItem {
  id: string; text: string; kind: "style" | "interest" | "sensitivity";
  dimension: string | null; confidence: number; evidence_count: number; active: boolean;
}
export function getPersonaMemories(): Promise<{ items: PersonaItem[] }> {
  return authed("/api/memories/persona");
}

// ── projects (U3) ─────────────────────────────────────────────────────────
export interface ProjectSummary {
  id: string; name: string; type: string; status: string; entry_count: number;
  last_activity: string; last_entry: Record<string, unknown> | null;
  pending_insight: boolean; metrics: Record<string, unknown>;
}
export function getProjects(): Promise<{ items: ProjectSummary[] }> {
  return authed("/api/projects");
}

// ── knowledge graph (U4) ──────────────────────────────────────────────────
export interface GraphNode { id: string; label: string }
export interface GraphEdge {
  source: string | null; target: string | null; fact: string; relation: string | null;
  valid_from: string | null; valid_to: string | null; current: boolean;
}
export function getKnowledgeGraph(): Promise<{ nodes: GraphNode[]; edges: GraphEdge[] }> {
  return authed("/api/knowledge-graph");
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
export interface TurnTotals {
  turn: number; tokens_in: number; tokens_out: number; cost_usd: number;
  llm_calls: number; tool_calls: number; failures: number; total_ms: number;
  reflected: boolean; langfuse_url?: string;
}
export interface TraceSession { session_id: string; last_ts: number; turn_count: number }
export function listTraceSessions(params: {
  offset?: number; limit?: number; from?: string; to?: string;
} = {}): Promise<{ total: number; offset: number; limit: number; sessions: TraceSession[] }> {
  const q = new URLSearchParams();
  if (params.offset) q.set("offset", String(params.offset));
  if (params.limit) q.set("limit", String(params.limit));
  if (params.from) q.set("from", params.from);
  if (params.to) q.set("to", params.to);
  return authed(`/debug/traces?${q.toString()}`);
}
export function getSessionTrace(
  sessionId: string,
): Promise<{ events: TraceEvent[]; turns: TurnTotals[] }> {
  return authed(`/debug/traces/${encodeURIComponent(sessionId)}`);
}
export interface VersionRow {
  prompt_version: string; thumbs_up: number; thumbs_down: number;
  n: number; up_rate: number | null; avg_judge_score: number | null;
}
export function getAttribution(): Promise<{ by_prompt_version: VersionRow[] }> {
  return authed("/debug/attribution");
}

// ── model selection (§4 / F8) ─────────────────────────────────────────────
export function getModels(): Promise<{
  choices: string[];
  selected: string | null;
  default: string;
  reasoning_choices: string[];
  reasoning_model: string | null;
  reasoning_default: string;
  voice_engines: string[];
  voice_engine: string;
}> {
  return authed("/api/models");
}
export function setModel(fast_model: string | null): Promise<{ selected: string | null }> {
  return authed("/api/models", { method: "PATCH", body: JSON.stringify({ fast_model }) });
}
// F8: the mature "thinking" model for the main reasoning turn.
export function setReasoningModel(
  reasoning_model: string | null,
): Promise<{ reasoning_model: string | null }> {
  return authed("/api/models", { method: "PATCH", body: JSON.stringify({ reasoning_model }) });
}
export function setVoiceEngine(voice_engine: string): Promise<{ voice_engine: string }> {
  return authed("/api/models", { method: "PATCH", body: JSON.stringify({ voice_engine }) });
}

// ── external tool UIs (F9) ─────────────────────────────────────────────────
export function getTools(): Promise<{ tools: { langfuse?: string; langgraph?: string } }> {
  return authed("/api/tools");
}

// ── feedback ─────────────────────────────────────────────────────────────
export function sendFeedback(body: {
  session_id: string; rating: "up" | "down"; turn_id?: string; note?: string;
}): Promise<{ id: string; rating: string }> {
  return authed("/api/feedback", { method: "POST", body: JSON.stringify(body) });
}
