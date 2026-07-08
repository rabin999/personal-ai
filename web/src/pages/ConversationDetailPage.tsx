import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getConversation, type ConversationTurn } from "../lib/api";
import { TraceTimeline } from "./TraceDetailPage";

// One conversation, in full: the readable transcript up top, then the SAME full
// trace-detail component the Traces page uses — so from a conversation you can
// see exactly what the companion thought, remembered, looked up, and reflected on
// each turn. Both views read this user's own durable stores (user-scoped).
export default function ConversationDetailPage() {
  const { sessionId = "" } = useParams();
  const [turns, setTurns] = useState<ConversationTurn[] | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    void getConversation(sessionId).then((r) => setTurns(r.turns)).catch(() => setTurns([]));
  }, [sessionId]);

  return (
    <section className="mx-auto max-w-4xl">
      <div className="mb-4 flex items-center gap-3">
        <Link to="/conversations" className="text-sm text-sky-600 hover:underline">← Conversations</Link>
        <h1 className="truncate text-lg font-semibold">Conversation</h1>
      </div>

      <div className="mb-6 space-y-3 rounded-xl border border-neutral-200 p-4 dark:border-neutral-800">
        {turns === null && <p className="text-sm text-neutral-500">Loading…</p>}
        {turns?.length === 0 && <p className="text-sm text-neutral-500">No turns.</p>}
        {turns?.map((t) => (
          <div key={t.turn_index} className="space-y-1 text-sm">
            <p><span className="font-semibold">You:</span> {t.user_text}</p>
            <p className="text-neutral-600 dark:text-neutral-300">
              <span className="font-semibold">Companion:</span> {t.assistant_text}
            </p>
          </div>
        ))}
      </div>

      <h2 className="mb-3 text-sm font-semibold text-neutral-500">Full trace</h2>
      <TraceTimeline sessionId={sessionId} />
    </section>
  );
}
