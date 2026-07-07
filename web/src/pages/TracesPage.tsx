import { useEffect, useMemo, useState } from "react";
import {
  getSessionTrace,
  listTraceSessions,
  sendFeedback,
  type TraceEvent,
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

  useEffect(() => {
    void listTraceSessions().then((r) => {
      setSessions(r.sessions);
      if (r.sessions[0]) setSelected(r.sessions[0].session_id);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!selected) return;
    void getSessionTrace(selected).then((r) => setEvents(r.events)).catch(() => setEvents([]));
  }, [selected]);

  const turns = useMemo(() => groupByTurn(events), [events]);

  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Traces</h1>
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
          <TurnView key={t.turn} sessionId={selected!} turn={t} />
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

function TurnView({ sessionId, turn }: { sessionId: string; turn: Turn }) {
  return (
    <div className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      {turn.said && <Line label="You said" value={turn.said} />}
      {turn.retrieved.length > 0 && <Line label="Remembered" value={turn.retrieved.join("; ")} />}
      {turn.lookedUp.length > 0 && <Line label="Looked up" value={turn.lookedUp.join("; ")} />}
      {turn.stored && <Line label="Stored" value={turn.stored} />}
      {turn.reply && <Line label="Replied" value={turn.reply} strong />}
      <Feedback sessionId={sessionId} turn={turn.turn} />
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
