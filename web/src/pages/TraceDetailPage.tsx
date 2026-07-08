import { useEffect, useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import {
  getSessionTrace,
  sendFeedback,
  type TraceEvent,
  type TurnTotals,
} from "../lib/api";

// Full end-to-end TRACE DETAIL for one session: every turn, every pipeline span,
// with the reasoning story (context connection, persona read, tool why-not,
// self-reflection), the model calls (tokens/cost/latency), and per-turn totals —
// reconstructable from this page alone (spec §3 / addendum A5/A9). The timeline is
// exported as <TraceTimeline> so the conversation detail page reuses the exact
// same component.
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

function TurnDetail({
  sessionId, turn, spans, totals,
}: { sessionId: string; turn: number; spans: TraceEvent[]; totals?: TurnTotals }) {
  const said = str(find(spans, "session")?.data?.text)
    || str(find(spans, "stt")?.data?.text);
  const reply = str(find(spans, "response")?.data?.text) || str(find(spans, "response")?.message);
  return (
    <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-neutral-100 bg-neutral-50 px-4 py-3 text-xs dark:border-neutral-800 dark:bg-neutral-900/50">
        <span className="font-semibold">Turn {turn}</span>
        {totals && <>
          <M>{totals.total_ms ? `${Math.round(totals.total_ms)} ms` : "—"}</M>
          <M>{totals.tokens_in + totals.tokens_out} tok</M>
          <M>${totals.cost_usd.toFixed(5)}</M>
          <M>{totals.llm_calls} LLM · {totals.tool_calls} tool</M>
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

      {/* Full ordered pipeline — every span, every field. */}
      <ol className="divide-y divide-neutral-100 dark:divide-neutral-800">
        {spans.map((e, i) => <SpanRow key={i} event={e} />)}
      </ol>

      <div className="border-t border-neutral-100 px-4 dark:border-neutral-800">
        <Feedback sessionId={sessionId} turn={turn} />
      </div>
    </div>
  );
}

// One pipeline span, fully expanded: stage badge, message, and all the
// engineering fields (model/tokens/cost/latency, tool status/args/result,
// reasoning node fields incl. the why-not, raw voice_text, prompt version).
function SpanRow({ event }: { event: TraceEvent }) {
  const d = (event.data ?? {}) as Record<string, unknown>;
  const stage = event.stage;
  const node = str(d.node);
  const rows: [string, string][] = [];
  const add = (label: string, v: unknown) => {
    if (v === undefined || v === null || v === "") return;
    rows.push([label, typeof v === "object" ? JSON.stringify(v) : String(v)]);
  };
  // model call
  add("model", d.model); add("tier", d.tier);
  if (d.input_tokens ?? d.tokens_in) add("tokens in", d.input_tokens ?? d.tokens_in);
  if (d.output_tokens ?? d.tokens_out) add("tokens out", d.output_tokens ?? d.tokens_out);
  if (d.cost_usd ?? d.usd) add("cost", `$${d.cost_usd ?? d.usd}`);
  add("latency", d.latency_ms !== undefined ? `${d.latency_ms} ms` : undefined);
  if (d.cache_hit !== undefined) add("cache", d.cache_hit ? `hit (${d.cached_tokens})` : "miss");
  // reasoning / graph nodes
  add("node", node);
  add("relation", d.relation); add("refers to", d.refers_to); add("note", d.note);
  add("live-search suppressed", d.suppress_live_search ?? d.live_search_suppressed);
  add("emotion", d.emotion); add("persona context", d.persona_context);
  add("action", d.action); add("available tools", d.available_tools);
  add("tool why-not", d.tool_why_not);
  // multi-utterance
  add("decision", d.decision); add("reason", d.reason);
  // tools
  add("tool", d.tool); add("type", d.tool_type); add("status", d.status);
  add("args", d.args); add("result", d.result);
  // memory / assembly
  add("stored", d.semantic || d.episodic || d.trades);
  add("prompt version", d.prompt_version);
  if (d.prompt_chars) add("prompt chars", d.prompt_chars);
  add("style flags", Array.isArray(d.style_flags) && d.style_flags.length ? d.style_flags : undefined);
  add("total", d.total_ms !== undefined ? `${d.total_ms} ms` : undefined);
  const voice = str(d.voice_text);

  return (
    <li className="px-4 py-2.5 text-sm">
      <div className="flex items-center gap-2">
        <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${badge(stage)}`}>
          {stage}{node ? `·${node}` : ""}
        </span>
        {event.level === "warn" && <span className="text-amber-500">⚠</span>}
        {event.message && <span className="truncate text-neutral-500">{event.message.slice(0, 100)}</span>}
      </div>
      {rows.length > 0 && (
        <dl className="mt-1.5 grid grid-cols-[auto,1fr] gap-x-3 gap-y-0.5 text-xs">
          {rows.map(([k, v], i) => (
            <div key={i} className="contents">
              <dt className="text-neutral-400">{k}</dt>
              <dd className="break-words font-mono text-[11px] text-neutral-600 dark:text-neutral-300">{v.slice(0, 400)}</dd>
            </div>
          ))}
        </dl>
      )}
      {voice && <p className="mt-1 break-words text-xs italic text-neutral-400">voice: {voice.slice(0, 300)}</p>}
    </li>
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
  return "bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300";
}

function find(spans: TraceEvent[], stage: string): TraceEvent | undefined {
  return spans.find((e) => e.stage === stage);
}
function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}
