import { useCallback, useEffect, useState } from "react";
import { format } from "date-fns";
import {
  getConversation,
  listConversations,
  type ConversationHeader,
  type ConversationTurn,
} from "../lib/api";

const PAGE = 10;

// The user's conversation history: server-side paginated + server-side datetime
// range filter (from/to sent as ISO; the server filters on epoch). date-fns for
// display formatting only — no client-side range math.
export default function ConversationsPage() {
  const [items, setItems] = useState<ConversationHeader[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await listConversations({
        offset,
        limit: PAGE,
        from: from ? new Date(from).toISOString() : undefined,
        to: to ? new Date(to).toISOString() : undefined,
      });
      setItems(res.conversations);
      setTotal(res.total);
    } catch (e) {
      setError(String(e));
    }
  }, [offset, from, to]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Conversations</h1>

      <div className="mb-4 flex flex-wrap items-end gap-3 rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
        <Field label="From">
          <input
            type="datetime-local"
            value={from}
            onChange={(e) => { setOffset(0); setFrom(e.target.value); }}
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
          />
        </Field>
        <Field label="To">
          <input
            type="datetime-local"
            value={to}
            onChange={(e) => { setOffset(0); setTo(e.target.value); }}
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
          />
        </Field>
        {(from || to) && (
          <button
            onClick={() => { setFrom(""); setTo(""); setOffset(0); }}
            className="text-sm text-neutral-500 underline"
          >
            clear
          </button>
        )}
      </div>

      {error && <p className="mb-3 text-sm text-red-600">{error}</p>}
      {items.length === 0 && !error && <p className="text-sm text-neutral-500">No conversations yet.</p>}

      <ul className="space-y-2">
        {items.map((c) => (
          <li key={c.session_id} className="rounded-lg border border-neutral-200 dark:border-neutral-800">
            <button
              onClick={() => setOpen(open === c.session_id ? null : c.session_id)}
              className="flex w-full items-center justify-between px-4 py-3 text-left"
            >
              <span className="font-medium">{fmt(c.last_at_iso || c.started_at_iso)}</span>
              <span className="text-sm text-neutral-500">{c.turn_count} turns</span>
            </button>
            {open === c.session_id && <ConversationDetail sessionId={c.session_id} />}
          </li>
        ))}
      </ul>

      <Pager offset={offset} total={total} page={PAGE} onChange={setOffset} />
    </section>
  );
}

function ConversationDetail({ sessionId }: { sessionId: string }) {
  const [turns, setTurns] = useState<ConversationTurn[] | null>(null);
  useEffect(() => {
    void getConversation(sessionId).then((r) => setTurns(r.turns)).catch(() => setTurns([]));
  }, [sessionId]);
  if (turns === null) return <p className="px-4 pb-3 text-sm text-neutral-500">Loading…</p>;
  return (
    <div className="space-y-3 border-t border-neutral-200 px-4 py-3 dark:border-neutral-800">
      {turns.map((t) => (
        <div key={t.turn_index} className="space-y-1 text-sm">
          <p><span className="font-semibold">You:</span> {t.user_text}</p>
          <p className="text-neutral-600 dark:text-neutral-300">
            <span className="font-semibold">Companion:</span> {t.assistant_text}
          </p>
        </div>
      ))}
    </div>
  );
}

function Pager({ offset, total, page, onChange }: {
  offset: number; total: number; page: number; onChange: (o: number) => void;
}) {
  if (total <= page) return null;
  return (
    <div className="mt-4 flex items-center justify-between text-sm">
      <button disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - page))}
        className="rounded border border-neutral-300 px-3 py-1 disabled:opacity-40 dark:border-neutral-700">
        Prev
      </button>
      <span className="text-neutral-500">{offset + 1}–{Math.min(offset + page, total)} of {total}</span>
      <button disabled={offset + page >= total} onClick={() => onChange(offset + page)}
        className="rounded border border-neutral-300 px-3 py-1 disabled:opacity-40 dark:border-neutral-700">
        Next
      </button>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-neutral-500">
      {label}
      {children}
    </label>
  );
}

function fmt(iso?: string): string {
  if (!iso) return "—";
  try {
    return format(new Date(iso), "PPp");
  } catch {
    return iso;
  }
}
