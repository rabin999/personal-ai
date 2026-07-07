import { useEffect, useMemo, useState } from "react";
import {
  getAttribution,
  getSessionTrace,
  listTraceSessions,
  sendFeedback,
  type TraceEvent,
  type TurnTotals,
  type VersionRow,
} from "../lib/api";

// A READABLE, user-facing view of what happened each turn — what they said, what
// the companion remembered/retrieved, what it looked up, what it replied — built
// from the durable trace store. Not the raw engineering spans; those still exist
// in the backend. Each turn carries a thumbs up/down + optional note (feedback),
// tied to session_id + turn_id so a thumbs-down is inspectable with its trace.
export default function TracesPage() {
  const [sessions, setSessions] = useState<{ session_id: string; last_ts: number }[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [totals, setTotals] = useState<TurnTotals[]>([]);
  const [versions, setVersions] = useState<VersionRow[]>([]);

  useEffect(() => {
    void listTraceSessions().then((r) => {
      setSessions(r.sessions);
      if (r.sessions[0]) setSelected(r.sessions[0].session_id);
    }).catch(() => {});
    void getAttribution().then((r) => setVersions(r.by_prompt_version)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!selected) return;
    void getSessionTrace(selected)
      .then((r) => { setEvents(r.events); setTotals(r.turns ?? []); })
      .catch(() => { setEvents([]); setTotals([]); });
  }, [selected]);

  const turns = useMemo(() => groupByTurn(events), [events]);
  const totalsByTurn = useMemo(
    () => new Map(totals.map((t) => [t.turn, t])),
    [totals],
  );
  const spansByTurn = useMemo(() => {
    const m = new Map<number, TraceEvent[]>();
    for (const e of events) m.set(e.turn, [...(m.get(e.turn) ?? []), e]);
    return m;
  }, [events]);

  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Traces</h1>

      {versions.length > 0 && (
        <div className="mb-4 rounded-lg border border-neutral-200 p-3 text-sm dark:border-neutral-800">
          <p className="mb-2 font-semibold">Response quality by prompt version</p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-neutral-500">
                <tr><th className="text-left">version</th><th>👍</th><th>👎</th><th>up-rate</th><th>judge</th></tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.prompt_version} className="border-t border-neutral-100 dark:border-neutral-800">
                    <td className="py-1 font-mono">{v.prompt_version}</td>
                    <td className="text-center">{v.thumbs_up}</td>
                    <td className="text-center">{v.thumbs_down}</td>
                    <td className="text-center">{v.up_rate === null ? "—" : `${Math.round(v.up_rate * 100)}%`}</td>
                    <td className="text-center">{v.avg_judge_score ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {sessions.length === 0 && <p className="text-sm text-neutral-500">No traced conversations yet.</p>}

      {sessions.length > 0 && (
        <select
          value={selected ?? ""}
          onChange={(e) => setSelected(e.target.value)}
          className="mb-4 rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
        >
          {sessions.map((s) => (
            <option key={s.session_id} value={s.session_id}>{s.session_id}</option>
          ))}
        </select>
      )}

      <div className="space-y-3">
        {turns.map((t) => (
          <TurnView
            key={t.turn}
            sessionId={selected!}
            turn={t}
            totals={totalsByTurn.get(t.turn)}
            spans={spansByTurn.get(t.turn) ?? []}
          />
        ))}
      </div>
    </section>
  );
}

interface Turn {
  turn: number;
  said?: string;
  retrieved: string[];
  lookedUp: string[];
  stored?: string;
  reflected: boolean;
  reply?: string;
}

function groupByTurn(events: TraceEvent[]): Turn[] {
  const map = new Map<number, Turn>();
  for (const e of events) {
    const t = map.get(e.turn) ?? { turn: e.turn, retrieved: [], lookedUp: [], reflected: false };
    if (e.stage === "stt") t.said = str(e.data?.text) || t.said;
    if (e.stage === "retrieval") t.retrieved.push(e.message);
    if (e.stage === "memory") t.stored = e.message;
    if (e.stage === "reflection") t.reflected = true;
    if (e.stage === "generation" && String(e.message).includes("tool")) t.lookedUp.push(e.message);
    if (e.stage === "response") t.reply = str(e.data?.text) || e.message;
    map.set(e.turn, t);
  }
  return [...map.values()].filter((t) => t.turn > 0).sort((a, b) => a.turn - b.turn);
}

function TurnView({
  sessionId, turn, totals, spans,
}: { sessionId: string; turn: Turn; totals?: TurnTotals; spans: TraceEvent[] }) {
  const [showRaw, setShowRaw] = useState(false);
  return (
    <div className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      {turn.said && <Line label="You said" value={turn.said} />}
      {turn.retrieved.length > 0 && <Line label="Remembered" value={turn.retrieved.join("; ")} />}
      {turn.lookedUp.length > 0 && <Line label="Looked up" value={turn.lookedUp.join("; ")} />}
      {turn.stored && <Line label="Stored" value={turn.stored} />}
      {turn.reply && <Line label="Replied" value={turn.reply} strong />}

      {totals && (
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-neutral-500">
          <span>{totals.total_ms ? `${Math.round(totals.total_ms)} ms` : "—"}</span>
          <span>{totals.tokens_in + totals.tokens_out} tok</span>
          <span>${totals.cost_usd.toFixed(5)}</span>
          <span>{totals.llm_calls} LLM · {totals.tool_calls} tool</span>
          {totals.failures > 0 && <span className="text-red-500">{totals.failures} failed</span>}
          {totals.reflected && <span>self-reflected</span>}
        </div>
      )}

      <button
        onClick={() => setShowRaw((v) => !v)}
        className="mt-2 text-xs text-neutral-500 underline"
      >
        {showRaw ? "hide" : "show"} technical trace ({spans.length} steps)
      </button>
      {showRaw && (
        <div className="mt-2 max-h-80 overflow-auto rounded bg-neutral-50 p-2 dark:bg-neutral-900">
          {spans.map((e, i) => (
            <SpanRow key={i} event={e} />
          ))}
        </div>
      )}

      <Feedback sessionId={sessionId} turn={turn.turn} />
    </div>
  );
}

// One raw pipeline step: stage + message + the key engineering fields
// (model/tokens/cost/latency/status) so a turn is fully reconstructable (§3).
function SpanRow({ event }: { event: TraceEvent }) {
  const d = event.data ?? {};
  const bits: string[] = [];
  const push = (k: string, label = k) => {
    if (d[k] !== undefined && d[k] !== null && d[k] !== "") bits.push(`${label}=${d[k]}`);
  };
  push("model");
  push("tier");
  if (d.input_tokens ?? d.tokens_in) bits.push(`in=${d.input_tokens ?? d.tokens_in}`);
  if (d.output_tokens ?? d.tokens_out) bits.push(`out=${d.output_tokens ?? d.tokens_out}`);
  if (d.cost_usd ?? d.usd) bits.push(`$${d.cost_usd ?? d.usd}`);
  push("latency_ms", "ms");
  push("tool");
  push("tool_type", "type");
  push("status");
  push("action");
  const voice = str(d.voice_text);
  return (
    <div className="border-b border-neutral-100 py-1 font-mono text-[11px] last:border-0 dark:border-neutral-800">
      <span className="font-semibold text-neutral-700 dark:text-neutral-300">{event.stage}</span>
      {event.level === "warn" && <span className="ml-1 text-amber-500">⚠</span>}
      {event.message && <span className="ml-2 text-neutral-500">{event.message.slice(0, 80)}</span>}
      {bits.length > 0 && <span className="ml-2 text-neutral-400">{bits.join(" · ")}</span>}
      {voice && <div className="text-neutral-400">voice: {voice.slice(0, 120)}</div>}
    </div>
  );
}

function Line({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <p className={`text-sm ${strong ? "" : "text-neutral-600 dark:text-neutral-300"}`}>
      <span className="font-semibold">{label}:</span> {value}
    </p>
  );
}

function Feedback({ sessionId, turn }: { sessionId: string; turn: number }) {
  const [sent, setSent] = useState<"up" | "down" | null>(null);
  const [noting, setNoting] = useState(false);
  const [note, setNote] = useState("");

  async function submit(rating: "up" | "down", withNote = "") {
    setSent(rating);
    await sendFeedback({ session_id: sessionId, turn_id: String(turn), rating, note: withNote })
      .catch(() => {});
  }

  return (
    <div className="mt-3 flex items-center gap-2 border-t border-neutral-100 pt-2 dark:border-neutral-800">
      <button
        onClick={() => void submit("up")}
        className={`rounded px-2 py-1 text-sm ${sent === "up" ? "bg-green-100 dark:bg-green-900" : "hover:bg-neutral-100 dark:hover:bg-neutral-800"}`}
        title="Good response"
      >
        👍
      </button>
      <button
        onClick={() => { setSent("down"); setNoting(true); }}
        className={`rounded px-2 py-1 text-sm ${sent === "down" ? "bg-red-100 dark:bg-red-900" : "hover:bg-neutral-100 dark:hover:bg-neutral-800"}`}
        title="Poor response"
      >
        👎
      </button>
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

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}
