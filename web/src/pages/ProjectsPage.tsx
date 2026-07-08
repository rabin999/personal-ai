import { useEffect, useState } from "react";
import { getProjects, type ProjectSummary } from "../lib/api";
import { Loader } from "../components/States";

// The user's dynamic projects (brief U3): ongoing threads the companion tracks —
// a share position, a goal, a plan — each with its current status and last activity.
export default function ProjectsPage() {
  const [items, setItems] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getProjects()
      .then((r) => setItems(r.items))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <section>
      <h1 className="mb-1 text-xl font-semibold">Projects</h1>
      <p className="mb-4 text-xs text-neutral-500">ongoing things I'm helping you track</p>
      {error && <p className="mb-3 text-sm text-red-600">{error}</p>}
      {loading && <Loader label="Loading projects…" />}
      {!loading && items.length === 0 && (
        <div className="rounded-xl border border-neutral-200 px-4 py-6 text-sm text-neutral-400 dark:border-neutral-800">
          No projects yet. Tell me about something you're tracking — a share position, a goal, a plan —
          and it'll show up here.
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        {items.map((p) => (
          <div
            key={p.id}
            className="rounded-xl border border-neutral-200 p-4 dark:border-neutral-800"
          >
            <div className="flex items-start justify-between gap-2">
              <h2 className="font-medium">{p.name}</h2>
              <span className="shrink-0 rounded-full bg-neutral-100 px-2 py-0.5 text-xs text-neutral-500 dark:bg-neutral-800">
                {p.type.replace(/_/g, " ")}
              </span>
            </div>
            <p className="mt-1.5 text-sm text-neutral-700 dark:text-neutral-300">{p.status}</p>
            <div className="mt-3 flex items-center gap-3 text-xs text-neutral-400">
              <span>{p.entry_count} update{p.entry_count === 1 ? "" : "s"}</span>
              {p.last_activity && <span>· last {p.last_activity.slice(0, 10)}</span>}
              {p.pending_insight && (
                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                  insight waiting
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
