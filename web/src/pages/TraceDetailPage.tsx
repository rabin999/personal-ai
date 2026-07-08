import { useEffect, useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import {
  getSessionTrace,
  sendFeedback,
  type TraceEvent,
  type TurnTotals,
} from "../lib/api";

// Full end-to-end TRACE DETAIL for one session: every turn, every pipeline span,
// with the COMPLETE internal story (C1) — voice/emotion perception, every LLM call
// with its PURPOSE, full params, verbatim prompt + reply, tokens/cost/latency/cache,
// parallel-vs-sequential ordering, the reasoning + intent + why-search decisions,
// the assembled prompt, self-reflection (draft→critique→revise), and what was
// written to memory. Reconstructable from this page alone. The timeline is exported
// as <TraceTimeline> so the conversation detail page reuses the same component.
export default function TraceDetailPage() {
  const { sessionId = "" } = useParams();
  return (
    <section className="mx-auto max-w-4xl">
      <div className="mb-4 flex items-center gap-3">
        <Link to="/traces" className="text-sm text-sky-600 hover:underline">← Traces</Link>
        <h1 className="truncate text-lg font-semibold">
          Trace · <span className="font-mono text-sm">{sessionId}</span>
        </h1>
      </div>
      <TraceTimeline sessionId={sessionId} />
    </section>
  );
}

// The reusable full-trace timeline for one session. Fetches the durable trace
// store (Mongo turn_traces) — works on prod with no Langfuse.
export function TraceTimeline({ sessionId }: { sessionId: string }) {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [totals, setTotals] = useState<TurnTotals[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!sessionId) return;
    setLoading(true);
    void getSessionTrace(sessionId)
      .then((r) => { setEvents(r.events); setTotals(r.turns ?? []); })
      .catch(() => { setEvents([]); setTotals([]); })
      .finally(() => setLoading(false));
  }, [sessionId]);

  const byTurn = useMemo(() => {
    const m = new Map<number, TraceEvent[]>();
    for (const e of [...events].sort((a, b) => (a.ts ?? 0) - (b.ts ?? 0))) {
      m.set(e.turn, [...(m.get(e.turn) ?? []), e]);
    }
    return m;
  }, [events]);
  const totalsByTurn = useMemo(() => new Map(totals.map((t) => [t.turn, t])), [totals]);
  const turns = [...byTurn.keys()].filter((t) => t > 0).sort((a, b) => a - b);

  if (loading) return <p className="text-sm text-neutral-500">Loading…</p>;
  if (turns.length === 0) return <p className="text-sm text-neutral-500">No trace events for this session.</p>;

  return (
    <div className="space-y-5">
      {turns.map((turn) => (
        <TurnDetail
          key={turn}
          sessionId={sessionId}
          turn={turn}
          spans={byTurn.get(turn) ?? []}
          totals={totalsByTurn.get(turn)}
        />
      ))}
    </div>
  );
}

type LlmCall = {
  event: TraceEvent;
  index: number;      // 1-based order within the turn
  concurrent: boolean; // overlapped the previous call's window → ran in parallel
};

function TurnDetail({
  sessionId, turn, spans, totals,
}: { sessionId: string; turn: number; spans: TraceEvent[]; totals?: TurnTotals }) {
  const said = str(find(spans, "session")?.data?.text)
    || str(find(spans, "stt")?.data?.text);
  const reply = str(find(spans, "response")?.data?.text) || str(find(spans, "response")?.message);

  // C1: order the LLM calls and mark which ran concurrently (parallel) vs. one
  // after another (sequential), from the precise start/end windows on each call.
  const calls: LlmCall[] = useMemo(() => {
    const cs = spans
      .filter((e) => e.message === "llm.call")
      .map((e) => e)
      .sort((a, b) => num(a.data?.start_ts) - num(b.data?.start_ts));
    let prevEnd = 0;
    return cs.map((event, i) => {
      const start = num(event.data?.start_ts);
      const concurrent = i > 0 && prevEnd > 0 && start < prevEnd - 0.05;
      prevEnd = Math.max(prevEnd, num(event.data?.end_ts));
      return { event, index: i + 1, concurrent };
    });
  }, [spans]);
  const callIndex = useMemo(() => new Map(calls.map((c) => [c.event, c])), [calls]);
  const anyParallel = calls.some((c) => c.concurrent);

  return (
    <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-neutral-100 bg-neutral-50 px-4 py-3 text-xs dark:border-neutral-800 dark:bg-neutral-900/50">
        <span className="font-semibold">Turn {turn}</span>
        {totals && <>
          <M>{totals.total_ms ? `${Math.round(totals.total_ms)} ms` : "—"}</M>
          <M>{totals.tokens_in + totals.tokens_out} tok</M>
          <M>${totals.cost_usd.toFixed(5)}</M>
          <M>{totals.llm_calls} LLM · {totals.tool_calls} tool</M>
          {calls.length > 1 && (
            <M>{anyParallel ? "some parallel" : "sequential"}</M>
          )}
          {totals.failures > 0 && <span className="text-rose-500">{totals.failures} failed</span>}
          {totals.reflected && <M>self-reflected</M>}
          {totals.langfuse_url && (
            <a href={totals.langfuse_url} target="_blank" rel="noreferrer" className="text-indigo-500 underline">Langfuse ↗</a>
          )}
        </>}
      </div>

      {(said || reply) && (
        <div className="border-b border-neutral-100 px-4 py-3 text-sm dark:border-neutral-800">
          {said && <p className="text-neutral-600 dark:text-neutral-300"><b>You:</b> {said}</p>}
          {reply && <p className="mt-1"><b>Companion:</b> {reply}</p>}
        </div>
      )}

      {/* C1: at-a-glance map of every model call this turn, in order, with role,
          model, and whether it ran in parallel — the "how many LLM calls / why /
          parallel-or-sequential" answer, before the full spans below. */}
      {calls.length > 0 && (
        <div className="border-b border-neutral-100 px-4 py-3 dark:border-neutral-800">
          <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
            {calls.length} model call{calls.length > 1 ? "s" : ""}
          </p>
          <ol className="space-y-1 text-xs">
            {calls.map((c) => (
              <li key={c.index} className="flex flex-wrap items-center gap-x-2">
                <span className="font-mono text-neutral-500 dark:text-neutral-400">#{c.index}</span>
                <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${purposeBadge(str(c.event.data?.purpose))}`}>
                  {str(c.event.data?.purpose) || "call"}
                </span>
                <span className="text-neutral-500">{shortModel(str(c.event.data?.model))}</span>
                <span className="text-neutral-500 dark:text-neutral-400">{num(c.event.data?.latency_ms)}ms</span>
                <span className="text-neutral-500 dark:text-neutral-400">${fmtCost(c.event.data?.cost_usd)}</span>
                <span className={c.concurrent ? "text-amber-500" : "text-neutral-500 dark:text-neutral-400"}>
                  {c.index === 1 ? "start" : c.concurrent ? "∥ parallel" : "→ sequential"}
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* Full ordered pipeline — every span, every field, verbatim. */}
      <ol className="divide-y divide-neutral-100 dark:divide-neutral-800">
        {spans.map((e, i) => <SpanRow key={i} event={e} call={callIndex.get(e)} />)}
      </ol>

      <div className="border-t border-neutral-100 px-4 dark:border-neutral-800">
        <Feedback sessionId={sessionId} turn={turn} />
      </div>
    </div>
  );
}

// One pipeline span, fully expanded per stage. LLM calls get the purpose, full
// params, verbatim prompt (each message) + reply; reasoning/reflection/judgment/
// assembly/tool/memory each render their own meaningful fields — nothing important
// truncated away (C1).
function SpanRow({ event, call }: { event: TraceEvent; call?: LlmCall }) {
  const d = (event.data ?? {}) as Record<string, unknown>;
  const stage = event.stage;
  const node = str(d.node);

  // ── LLM call: the richest span — purpose, params, prompt, reply ──────────
  if (event.message === "llm.call") {
    const params = (d.params ?? {}) as Record<string, unknown>;
    const messages = Array.isArray(d.messages) ? (d.messages as Array<Record<string, unknown>>) : [];
    return (
      <li className="px-4 py-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          {call && <span className="font-mono text-xs text-neutral-500 dark:text-neutral-400">#{call.index}</span>}
          <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${purposeBadge(str(d.purpose))}`}>
            {str(d.purpose) || "llm call"}
          </span>
          <span className="text-xs text-neutral-500">{str(d.model)}</span>
          <span className="text-[11px] text-neutral-500 dark:text-neutral-400">{str(d.tier)} tier</span>
          {call && (
            <span className={`text-[11px] ${call.concurrent ? "text-amber-500" : "text-neutral-500 dark:text-neutral-400"}`}>
              {call.index === 1 ? "" : call.concurrent ? "∥ parallel" : "→ after #" + (call.index - 1)}
            </span>
          )}
        </div>
        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-neutral-500">
          <span>in {num(d.input_tokens)} / out {num(d.output_tokens)} tok</span>
          <span>${fmtCost(d.cost_usd)}</span>
          <span>{num(d.latency_ms)} ms</span>
          <span>{d.cache_hit ? `cache hit (${num(d.cached_tokens)})` : "cache miss"}</span>
          <span>temp {str(params.temperature) || "—"}</span>
          <span>max_tokens {str(params.max_tokens) || "—"}</span>
          <span>format {str(params.response_format) || "text"}</span>
          {params.streamed ? <span>streamed</span> : null}
        </div>
        {messages.length > 0 && (
          <Expandable label={`prompt sent — ${messages.length} message${messages.length > 1 ? "s" : ""}`}>
            <div className="space-y-2">
              {messages.map((m, i) => (
                <div key={i}>
                  <p className="text-[10px] font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">{str(m.role)}</p>
                  <pre className="whitespace-pre-wrap break-words font-mono text-[11px] text-neutral-600 dark:text-neutral-300">{str(m.content)}</pre>
                </div>
              ))}
            </div>
          </Expandable>
        )}
        {str(d.completion) && (
          <Expandable label="reply">
            <pre className="whitespace-pre-wrap break-words font-mono text-[11px] text-neutral-600 dark:text-neutral-300">{str(d.completion)}</pre>
          </Expandable>
        )}
      </li>
    );
  }

  // ── every other stage: labelled fields (+ expandable long text) ──────────
  const rows: [string, string][] = [];
  const add = (label: string, v: unknown) => {
    if (v === undefined || v === null || v === "") return;
    if (Array.isArray(v) && v.length === 0) return;
    rows.push([label, typeof v === "object" ? JSON.stringify(v, null, 1) : String(v)]);
  };
  // reasoning / graph nodes (perceive · resolve_context · respond · reflect_log)
  add("node", node);
  add("intent", d.intent); add("emotional read", d.emotional_read);
  add("needs live info", d.needs_live_info); add("live query", d.live_query);
  add("relation", d.relation); add("refers to", d.refers_to); add("note", d.note);
  add("live-search suppressed", d.suppress_live_search ?? d.live_search_suppressed);
  add("emotion", d.emotion); add("persona context", d.persona_context);
  add("recent turns", d.recent_turns);
  add("prompt (managed)", d.prompt_name && `${str(d.prompt_name)} v${str(d.prompt_managed_version)} (${str(d.prompt_source)})`);
  add("action", d.action); add("available tools", d.available_tools);
  add("tool why-not", d.tool_why_not);
  // judgment
  add("salience", d.salience); add("novelty", d.novelty); add("complexity", d.complexity);
  add("ambiguity", d.ambiguity); add("boundary", d.boundary_flag);
  // reflection
  add("ran", d.ran); add("triggered by", d.triggered_by);
  add("checked", d.checked); add("revised", d.revised);
  add("scrubbed", d.scrubbed); add("clean after", d.clean_after);
  // tools
  add("tool", d.tool); add("type", d.tool_type); add("mode", d.mode);
  add("status", d.status); add("ok", d.ok); add("args", d.args);
  // memory / assembly
  add("stored", d.semantic || d.episodic || d.trades);
  add("entities", d.entities); add("recall source", d.recall_source);
  add("preferences", d.preferences); add("procedural", d.procedural); add("semantic facts", d.semantic_facts);
  add("prompt version", d.prompt_version);
  if (d.prompt_chars) add("prompt chars", d.prompt_chars);
  add("sections", d.sections); add("active traits", d.active_traits);
  add("style flags", Array.isArray(d.style_flags) && d.style_flags.length ? d.style_flags : undefined);
  add("total", d.total_ms !== undefined ? `${d.total_ms} ms` : undefined);

  // long verbatim blobs get their own expandable panels, never truncated
  const longs: [string, string][] = [];
  const addLong = (label: string, v: unknown) => {
    const s = typeof v === "string" ? v : v ? JSON.stringify(v, null, 2) : "";
    if (s) longs.push([label, s]);
  };
  addLong("draft", d.draft); addLong("critique", d.critique); addLong("revised text", d.revised_text);
  addLong("system prompt", d.system_prompt);
  addLong("trait text", d.trait_text);
  addLong("result", d.result);
  if (Array.isArray(d.messages) && stage === "assembly") addLong("assembled messages", d.messages);
  const voice = str(d.voice_text);

  return (
    <li className="px-4 py-2.5 text-sm">
      <div className="flex items-center gap-2">
        <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${badge(stage)}`}>
          {stage}{node ? `·${node}` : ""}
        </span>
        {event.level === "warn" && <span className="text-amber-500">⚠</span>}
        {event.message && <span className="truncate text-neutral-500">{event.message.slice(0, 120)}</span>}
      </div>
      {rows.length > 0 && (
        <dl className="mt-1.5 grid grid-cols-[auto,1fr] gap-x-3 gap-y-0.5 text-xs">
          {rows.map(([k, v], i) => (
            <div key={i} className="contents">
              <dt className="text-neutral-500 dark:text-neutral-400">{k}</dt>
              <dd className="break-words font-mono text-[11px] text-neutral-600 dark:text-neutral-300">{v.slice(0, 800)}</dd>
            </div>
          ))}
        </dl>
      )}
      {longs.map(([label, text], i) => (
        <Expandable key={i} label={label}>
          <pre className="whitespace-pre-wrap break-words font-mono text-[11px] text-neutral-600 dark:text-neutral-300">{text}</pre>
        </Expandable>
      ))}
      {voice && <p className="mt-1 break-words text-xs italic text-neutral-500 dark:text-neutral-400">voice: {voice.slice(0, 400)}</p>}
    </li>
  );
}

// Collapsible verbatim block — closed by default so a long prompt doesn't flood
// the page, one click to read the whole thing (C1: full prompt must be reachable).
function Expandable({ label, children }: { label: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="text-[11px] font-medium text-sky-600 hover:underline dark:text-sky-400"
      >
        {open ? "▾" : "▸"} {label}
      </button>
      {open && (
        <div className="mt-1 max-h-[28rem] overflow-auto rounded-md border border-neutral-200 bg-neutral-50 p-2 dark:border-neutral-800 dark:bg-neutral-900/60">
          {children}
        </div>
      )}
    </div>
  );
}

// Thumbs up/down per turn, tied to session_id + turn_id so a rating is
// inspectable with its trace (feeds prompt-version attribution).
function Feedback({ sessionId, turn }: { sessionId: string; turn: number }) {
  const [sent, setSent] = useState<"up" | "down" | null>(null);
  const [noting, setNoting] = useState(false);
  const [note, setNote] = useState("");

  async function submit(rating: "up" | "down", withNote = "") {
    setSent(rating);
    await sendFeedback({ session_id: sessionId, turn_id: String(turn), rating, note: withNote }).catch(() => {});
  }

  return (
    <div className="flex items-center gap-2 py-2">
      <button
        onClick={() => void submit("up")}
        className={`rounded px-2 py-1 text-sm ${sent === "up" ? "bg-green-100 dark:bg-green-900" : "hover:bg-neutral-100 dark:hover:bg-neutral-800"}`}
        title="Good response"
      >👍</button>
      <button
        onClick={() => { setSent("down"); setNoting(true); }}
        className={`rounded px-2 py-1 text-sm ${sent === "down" ? "bg-red-100 dark:bg-red-900" : "hover:bg-neutral-100 dark:hover:bg-neutral-800"}`}
        title="Poor response"
      >👎</button>
      {noting && (
        <form
          onSubmit={(e) => { e.preventDefault(); setNoting(false); void submit("down", note); }}
          className="flex flex-1 items-center gap-2"
        >
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="what went wrong? (optional)"
            className="flex-1 rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
          />
          <button type="submit" className="text-sm text-neutral-500 underline">save</button>
        </form>
      )}
    </div>
  );
}

function M({ children }: { children: React.ReactNode }) {
  return <span className="text-neutral-500">{children}</span>;
}

function badge(stage: string): string {
  if (stage === "llm") return "bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300";
  if (stage === "tool") return "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300";
  if (stage === "reasoning") return "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300";
  if (stage === "response") return "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300";
  if (stage === "reflection") return "bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-900/40 dark:text-fuchsia-300";
  if (stage === "judgment") return "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300";
  return "bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300";
}

// Distinct colour per LLM-call ROLE so the sequence of purposes reads at a glance.
function purposeBadge(purpose: string): string {
  const p = purpose || "";
  if (p === "context_intent") return "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300";
  if (p.startsWith("response")) return "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300";
  if (p === "search_summarize" || p === "tool_react") return "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300";
  if (p === "judge") return "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300";
  if (p.includes("reflect") || p === "style_rewrite" || p === "disclosure_polish") return "bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-900/40 dark:text-fuchsia-300";
  if (p.includes("memory") || p === "compaction") return "bg-teal-100 text-teal-700 dark:bg-teal-900/40 dark:text-teal-300";
  return "bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300";
}

function shortModel(model: string): string {
  return model.split("/").pop() ?? model;
}
function fmtCost(v: unknown): string {
  const n = num(v);
  return n > 0 ? n.toFixed(5) : "0";
}
function num(v: unknown): number {
  return typeof v === "number" ? v : typeof v === "string" ? parseFloat(v) || 0 : 0;
}
function find(spans: TraceEvent[], stage: string): TraceEvent | undefined {
  return spans.find((e) => e.stage === stage);
}
function str(v: unknown): string {
  return typeof v === "string" ? v : v === undefined || v === null ? "" : String(v);
}
