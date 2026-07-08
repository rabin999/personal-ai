import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { format } from "date-fns";
import {
  listConversations,
  type ConversationHeader,
} from "../lib/api";
import { EmptyState, Loader } from "../components/States";

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
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setError(null);
    setLoading(true);
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
    } finally {
      setLoading(false);
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
            max={to || undefined}
            onChange={(e) => {
              setOffset(0);
              setFrom(e.target.value);
              if (to && e.target.value > to) setTo(""); // keep From ≤ To
            }}
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
          />
        </Field>
        <Field label="To">
          <input
            type="datetime-local"
            value={to}
            min={from || undefined} // can't pick a time before From
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
      {loading && <Loader label="Loading conversations…" />}
      {!loading && items.length === 0 && !error && (
        <EmptyState
          title={from || to ? "No conversations in this range" : "No conversations yet"}
          hint={from || to ? "Try widening the date range." : "Start talking on the home page — your conversations show up here."}
        />
      )}

      {!loading && items.length > 0 && (
      <ul className="divide-y divide-neutral-200 overflow-hidden rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
        {items.map((c) => (
          <li key={c.session_id}>
            <Link
              to={`/conversations/${encodeURIComponent(c.session_id)}`}
              className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-neutral-50 active:bg-neutral-100 dark:hover:bg-neutral-900 dark:active:bg-neutral-800"
            >
              <div className="min-w-0 flex-1">
                {/* F11: first message as a single-line preview (≤70 chars). */}
                <p className="truncate text-sm font-medium">
                  {preview(c.first_message) || <span className="text-neutral-400">(no message)</span>}
                </p>
                <p className="mt-0.5 text-xs text-neutral-500">
                  {fmt(c.last_at_iso || c.started_at_iso)} · {c.turn_count} {c.turn_count === 1 ? "turn" : "turns"}
                </p>
              </div>
              <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0 text-neutral-400" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M9 6l6 6-6 6" />
              </svg>
            </Link>
          </li>
        ))}
      </ul>
      )}

      <Pager offset={offset} total={total} page={PAGE} onChange={setOffset} />
    </section>
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

// F11: trim the first message to a single line, max 70 chars with an ellipsis.
function preview(msg?: string): string {
  const s = (msg ?? "").replace(/\s+/g, " ").trim();
  return s.length > 70 ? s.slice(0, 69).trimEnd() + "…" : s;
}

function fmt(iso?: string): string {
  if (!iso) return "—";
  try {
    return format(new Date(iso), "PPp");
  } catch {
    return iso;
  }
}
