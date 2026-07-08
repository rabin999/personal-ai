import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getConversation, type ConversationTurn } from "../lib/api";
import { TraceTimeline } from "./TraceDetailPage";

// One conversation, in full, as two TABS: the readable transcript (default), and
// the SAME full trace-detail timeline the app builds per turn — so from a
// conversation you can switch to exactly what the companion thought, remembered,
// looked up, and reflected on. Both read this user's own durable stores.
export default function ConversationDetailPage() {
  const { sessionId = "" } = useParams();
  const [turns, setTurns] = useState<ConversationTurn[] | null>(null);
  const [tab, setTab] = useState<"conversation" | "trace">("conversation");

  useEffect(() => {
    if (!sessionId) return;
    void getConversation(sessionId).then((r) => setTurns(r.turns)).catch(() => setTurns([]));
  }, [sessionId]);

  // Title = the first real user message (a short preview), not a generic label.
  const firstMsg = turns?.find((t) => t.user_text?.trim())?.user_text?.trim() ?? "";
  const title = firstMsg ? preview(firstMsg, 60) : "Conversation";

  return (
    <section className="mx-auto max-w-4xl">
      <div className="mb-4 flex items-center gap-3">
        <Link to="/conversations" className="shrink-0 text-sm text-sky-600 hover:underline">← Conversations</Link>
        <h1 className="truncate text-lg font-semibold" title={firstMsg || undefined}>{title}</h1>
      </div>

      {/* Tabs: Conversation (default) · Trace */}
      <div className="mb-5 flex gap-1 border-b border-neutral-200 dark:border-neutral-800">
        <TabButton active={tab === "conversation"} onClick={() => setTab("conversation")}>
          Conversation
        </TabButton>
        <TabButton active={tab === "trace"} onClick={() => setTab("trace")}>
          Trace
        </TabButton>
      </div>

      {tab === "conversation" ? (
        <div className="space-y-3 rounded-xl border border-neutral-200 p-4 dark:border-neutral-800">
          {turns === null && <p className="text-sm text-neutral-500">Loading…</p>}
          {turns?.length === 0 && <p className="text-sm text-neutral-500">No turns.</p>}
          {turns?.map((t) => (
            <div key={t.turn_index} className="space-y-1 text-sm">
              {t.user_text && <p><span className="font-semibold">You:</span> {t.user_text}</p>}
              <p className="text-neutral-600 dark:text-neutral-300">
                <span className="font-semibold">Companion:</span> {t.assistant_text}
              </p>
            </div>
          ))}
        </div>
      ) : (
        <TraceTimeline sessionId={sessionId} />
      )}
    </section>
  );
}

function preview(s: string, n: number): string {
  return s.length > n ? s.slice(0, n).trimEnd() + "…" : s;
}

function TabButton({
  active, onClick, children,
}: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
        active
          ? "border-sky-500 text-sky-600 dark:text-sky-400"
          : "border-transparent text-neutral-500 hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
      }`}
    >
      {children}
    </button>
  );
}
