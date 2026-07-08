import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { format } from "date-fns";
import {
  getAttribution,
  listTraceSessions,
  type TraceSession,
  type VersionRow,
} from "../lib/api";

const PAGE = 10;

// Traces browser: server-side paginated + datetime-range filtered list of this
// user's traced sessions (same pattern as Conversations — no dropdown). Each row
// links to the full end-to-end trace detail (/traces/:sessionId), which reads the
// durable Mongo trace store and shows every span of every turn.
export default function TracesPage() {
  const [items, setItems] = useState<TraceSession[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [versions, setVersions] = useState<VersionRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await listTraceSessions({
        offset,
        limit: PAGE,
        from: from ? new Date(from).toISOString() : undefined,
        to: to ? new Date(to).toISOString() : undefined,
      });
      setItems(res.sessions);
      setTotal(res.total);
    } catch (e) {
      setError(String(e));
    }
  }, [offset, from, to]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    void getAttribution().then((r) => setVersions(r.by_prompt_version)).catch(() => {});
  }, []);

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

      <div className="mb-4 flex flex-wrap items-end gap-3 rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
        <Field label="From">
          <input type="datetime-local" value={from}
            onChange={(e) => { setOffset(0); setFrom(e.target.value); }}
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700" />
        </Field>
        <Field label="To">
          <input type="datetime-local" value={to}
            onChange={(e) => { setOffset(0); setTo(e.target.value); }}
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700" />
        </Field>
        {(from || to) && (
          <button onClick={() => { setFrom(""); setTo(""); setOffset(0); }}
            className="text-sm text-neutral-500 underline">clear</button>
        )}
      </div>

      {error && <p className="mb-3 text-sm text-red-600">{error}</p>}
      {items.length === 0 && !error && <p className="text-sm text-neutral-500">No traced conversations yet.</p>}

      <ul className="space-y-2">
        {items.map((s) => (
          <li key={s.session_id}>
            <Link
              to={`/traces/${encodeURIComponent(s.session_id)}`}
              className="flex items-center justify-between rounded-lg border border-neutral-200 px-4 py-3 hover:bg-neutral-50 dark:border-neutral-800 dark:hover:bg-neutral-900"
            >
              <span className="font-medium">{fmt(s.last_ts)}</span>
              <span className="text-sm text-neutral-500">{s.turn_count} {s.turn_count === 1 ? "turn" : "turns"} →</span>
            </Link>
          </li>
        ))}
      </ul>

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
        className="rounded border border-neutral-300 px-3 py-1 disabled:opacity-40 dark:border-neutral-700">Prev</button>
      <span className="text-neutral-500">{offset + 1}–{Math.min(offset + page, total)} of {total}</span>
      <button disabled={offset + page >= total} onClick={() => onChange(offset + page)}
        className="rounded border border-neutral-300 px-3 py-1 disabled:opacity-40 dark:border-neutral-700">Next</button>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-neutral-500">{label}{children}</label>
  );
}

function fmt(ts?: number): string {
  if (!ts) return "—";
  try {
    return format(new Date(ts * 1000), "PPp");
  } catch {
    return String(ts);
  }
}
