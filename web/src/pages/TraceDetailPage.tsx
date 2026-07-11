import { useEffect, useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import {
  getSessionTrace,
  sendFeedback,
  type TraceEvent,
  type TurnTotals,
} from "../lib/api";

// Full end-to-end TRACE for one session, redesigned to READ as the story of each
// turn (C1): each turn is a COLLAPSIBLE card (first open by default) with a metrics
// header, the exchange, a model-call map (purpose · model · cost · parallel/
// sequential), then a vertical TIMELINE of the pipeline — every step titled in plain
// language, key facts as tidy lists/rows (never raw JSON walls), verbatim prompts one
// click away. Exported as <TraceTimeline> so the conversation Trace tab reuses it.
export default function TraceDetailPage() {
  const { sessionId = "" } = useParams();
  return (
    <section>
      <div className="mb-4 flex items-center gap-3">
        <Link to="/conversations" className="text-sm text-sky-600 hover:underline">← Conversations</Link>
        <h1 className="truncate text-lg font-semibold">Trace</h1>
      </div>
      <TraceTimeline sessionId={sessionId} />
    </section>
  );
}

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
  if (turns.length === 0) return <p className="text-sm text-neutral-500">No trace for this session yet.</p>;

  return (
    <div className="space-y-4">
      {turns.map((turn, i) => (
        <TurnDetail
          key={turn}
          sessionId={sessionId}
          turn={turn}
          spans={byTurn.get(turn) ?? []}
          totals={totalsByTurn.get(turn)}
          defaultOpen={i === 0} // first turn open by default
        />
      ))}
    </div>
  );
}

type LlmCall = { event: TraceEvent; index: number; concurrent: boolean };

function TurnDetail({
  sessionId, turn, spans, totals, defaultOpen,
}: { sessionId: string; turn: number; spans: TraceEvent[]; totals?: TurnTotals; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const said = str(find(spans, "session")?.data?.text) || str(find(spans, "stt")?.data?.text);
  const reply = str(find(spans, "response")?.data?.text) || str(find(spans, "response")?.message);

  const calls: LlmCall[] = useMemo(() => {
    const cs = spans
      .filter((e) => e.message === "llm.call")
      .sort((a, b) => num(a.data?.start_ts) - num(b.data?.start_ts));
    let prevEnd = 0;
    return cs.map((event, i) => {
      const start = num(event.data?.start_ts);
      const concurrent = i > 0 && prevEnd > 0 && start < prevEnd - 0.05;
      prevEnd = Math.max(prevEnd, num(event.data?.end_ts));
      return { event, index: i + 1, concurrent };
    });
  }, [spans]);
  const anyParallel = calls.some((c) => c.concurrent);
  const callMeta = useMemo(() => new Map(calls.map((c) => [c.event, c])), [calls]);

  // Canonical event timeline: EVERYTHING that happened this turn (LLM calls + every
  // pipeline stage), in chronological order, each stamped with its offset from the
  // turn's first event — so you read the turn as "what happened, when".
  const timeline = useMemo(() => {
    const evs = spans
      .filter((e) => !isNoise(e))
      .map((e) => ({ e, t: num(e.data?.start_ts) || e.ts || 0 }))
      .filter((x) => x.t > 0)
      .sort((a, b) => a.t - b.t);
    const t0 = evs.length ? evs[0].t : 0;
    return evs.map(({ e, t }) => ({ e, offsetMs: Math.max(0, (t - t0) * 1000) }));
  }, [spans]);

  return (
    <div className="overflow-hidden rounded-2xl border border-neutral-200 bg-white shadow-sm dark:border-neutral-800 dark:bg-neutral-900/40">
      {/* Collapsible header: turn + headline metrics + the gist */}
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full flex-wrap items-center gap-x-2 gap-y-1 px-4 py-3.5 text-left hover:bg-neutral-50 sm:px-5 dark:hover:bg-neutral-900/60"
      >
        <span className="text-neutral-500 dark:text-neutral-400">{open ? "▾" : "▸"}</span>
        <span className="text-sm font-semibold">Turn {turn}</span>
        {totals && <>
          <span className="mx-1 hidden h-3.5 w-px bg-neutral-300 sm:block dark:bg-neutral-700" />
          <span className="flex flex-wrap items-center gap-1.5">
            <Pill>{totals.total_ms ? fmtMs(totals.total_ms) : "—"}</Pill>
            <Pill>{fmtNum(totals.tokens_in + totals.tokens_out)} tok</Pill>
            <Pill>${totals.cost_usd.toFixed(4)}</Pill>
            <Pill>{totals.llm_calls} LLM · {totals.tool_calls} tool</Pill>
            {calls.length > 1 && <Pill>{anyParallel ? "some parallel" : "sequential"}</Pill>}
            {totals.reflected && <Pill tone="fuchsia">self-reflected</Pill>}
            {totals.failures > 0 && <Pill tone="rose">{totals.failures} failed</Pill>}
          </span>
        </>}
        {!open && said && (
          <span className="ml-auto hidden max-w-[45%] truncate text-[15px] text-neutral-500 dark:text-neutral-400 md:block">{said}</span>
        )}
      </button>

      {open && (
        <div className="border-t border-neutral-100 dark:border-neutral-800">
          {/* Exchange */}
          {(said || reply) && (
            <div className="space-y-2 border-b border-neutral-100 px-4 py-4 sm:px-5 dark:border-neutral-800">
              {said && (
                <p className="text-[15px] leading-relaxed">
                  <span className="mr-1.5 rounded bg-neutral-100 px-1.5 py-0.5 text-sm font-semibold uppercase tracking-wide text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400">You</span>
                  {said}
                </p>
              )}
              {reply && (
                <p className="text-[15px] leading-relaxed text-neutral-700 dark:text-neutral-200">
                  <span className="mr-1.5 rounded bg-sky-100 px-1.5 py-0.5 text-sm font-semibold uppercase tracking-wide text-sky-700 dark:bg-sky-900/50 dark:text-sky-300">Saathi</span>
                  {reply}
                </p>
              )}
            </div>
          )}

          {/* Canonical event timeline — everything in chronological order, stamped
              with the offset from the turn start. */}
          <div className="px-4 py-4 sm:px-5">
            <div className="mb-3 flex items-baseline justify-between">
              <p className="text-sm font-semibold uppercase tracking-wider text-neutral-500 dark:text-neutral-400">Timeline</p>
              <p className="text-xs text-neutral-400">
                {timeline.length} events · {calls.length} model call{calls.length === 1 ? "" : "s"}
                {calls.length > 1 ? ` · ${anyParallel ? "some parallel" : "sequential"}` : ""}
              </p>
            </div>
            <ol>
              {timeline.map(({ e, offsetMs }, i) => (
                <TimelineItem key={i} event={e} offsetMs={offsetMs} call={callMeta.get(e)} />
              ))}
            </ol>
          </div>

          <div className="border-t border-neutral-100 px-5 dark:border-neutral-800">
            <Feedback sessionId={sessionId} turn={turn} />
          </div>
        </div>
      )}
    </div>
  );
}

// ── one LLM call, as a card ────────────────────────────────────────────────
function CallCard({ call }: { call: LlmCall }) {
  const d = (call.event.data ?? {}) as Record<string, unknown>;
  const [open, setOpen] = useState(false);
  const params = (d.params ?? {}) as Record<string, unknown>;
  const messages = Array.isArray(d.messages) ? (d.messages as Array<Record<string, unknown>>) : [];
  const completion = str(d.completion);
  return (
    <div className="rounded-xl border border-neutral-200 bg-neutral-50/50 dark:border-neutral-800 dark:bg-neutral-900/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 text-[15px]">
        <span className="font-mono text-neutral-500 dark:text-neutral-400">#{call.index}</span>
        <span className={`rounded-md px-2 py-0.5 text-sm font-semibold ${purposeBadge(str(d.purpose))}`}>
          {prettyPurpose(str(d.purpose))}
        </span>
        <span className="text-neutral-500">{shortModel(str(d.model))}</span>
        <span className="text-neutral-500 dark:text-neutral-400">{fmtMs(num(d.latency_ms))}</span>
        <span className="text-neutral-500 dark:text-neutral-400">${fmtCost(d.cost_usd)}</span>
        <span className="text-neutral-500 dark:text-neutral-400">{num(d.input_tokens)}→{num(d.output_tokens)} tok</span>
        <span className={call.concurrent ? "font-medium text-amber-500" : "text-neutral-500 dark:text-neutral-400"}>
          {call.index === 1 ? "" : call.concurrent ? "∥ parallel" : "→ sequential"}
        </span>
        {(messages.length > 0 || completion) && (
          <button onClick={() => setOpen((o) => !o)}
            className="ml-auto text-sm font-medium text-sky-600 hover:underline dark:text-sky-400">
            {open ? "hide prompt" : "view prompt & reply"}
          </button>
        )}
      </div>
      {open && (
        <div className="space-y-3 border-t border-neutral-200 px-2.5 py-3 sm:px-3 dark:border-neutral-800">
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-neutral-500">
            <span>temp {str(params.temperature) || "—"}</span>
            <span>max_tokens {str(params.max_tokens) || "—"}</span>
            <span>format {str(params.response_format) || "text"}</span>
            <span>{d.cache_hit ? `cache hit (${num(d.cached_tokens)})` : "cache miss"}</span>
            {params.streamed ? <span>streamed</span> : null}
          </div>
          {messages.map((m, i) => (
            <div key={i}>
              <p className="mb-0.5 text-[13px] font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">{str(m.role)}</p>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-white p-2.5 font-mono text-sm leading-relaxed text-neutral-600 dark:bg-neutral-950/60 dark:text-neutral-300">{str(m.content)}</pre>
            </div>
          ))}
          {completion && (
            <div>
              <p className="mb-0.5 text-[13px] font-semibold uppercase tracking-wide text-emerald-500">reply</p>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-emerald-50/60 p-2.5 font-mono text-sm leading-relaxed text-neutral-700 dark:bg-emerald-950/20 dark:text-neutral-200">{completion}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── one chronological timeline entry: [time] │● content ────────────────────
function TimelineItem({
  event, offsetMs, call,
}: { event: TraceEvent; offsetMs: number; call?: LlmCall }) {
  const isCall = event.message === "llm.call";
  const d = (event.data ?? {}) as Record<string, unknown>;
  const dot = isCall ? "bg-violet-400" : stageMeta(event.stage, str(d.node)).dot;
  return (
    <li className="flex gap-3">
      <span className="w-14 shrink-0 pt-0.5 text-right font-mono text-xs tabular-nums text-neutral-400">
        {fmtOffset(offsetMs)}
      </span>
      <div className="relative flex-1 border-l border-neutral-200 pb-5 pl-5 dark:border-neutral-800">
        <span className={`absolute -left-[7px] top-1 h-3.5 w-3.5 rounded-full ring-4 ring-white dark:ring-neutral-900/40 ${dot}`} />
        {isCall && call ? <CallCard call={call} /> : <StepBody event={event} />}
      </div>
    </li>
  );
}

// The content of a non-LLM pipeline step (title + fields + expandables).
function StepBody({ event }: { event: TraceEvent }) {
  const d = (event.data ?? {}) as Record<string, unknown>;
  const meta = stageMeta(event.stage, str(d.node));
  const rows = fieldRows(event.stage, d);
  const longs = longFields(d);
  const warn = event.level === "warn" || event.stage === "error";
  return (
    <>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className={`text-[15px] font-semibold ${warn ? "text-amber-600 dark:text-amber-400" : ""}`}>{meta.title}</span>
        {meta.sub && <span className="text-sm text-neutral-500 dark:text-neutral-400">{meta.sub}</span>}
        {warn && <span className="text-amber-500">⚠</span>}
      </div>
      {rows.length > 0 && (
        <dl className="mt-1.5 grid grid-cols-[minmax(0,7rem),1fr] gap-x-3 gap-y-1 text-[15px]">
          {rows.map(([k, v], i) => (
            <div key={i} className="contents">
              <dt className="text-neutral-500 dark:text-neutral-400">{k}</dt>
              <dd className="min-w-0 text-neutral-700 dark:text-neutral-300">{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {longs.map(([label, text], i) => <Expandable key={i} label={label} text={text} />)}
    </>
  );
}

function Cell({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    const items = value.filter((x) => str(x).trim());
    if (items.length === 0) return <span className="text-neutral-500 dark:text-neutral-400">none</span>;
    const shown = items.slice(0, 6);
    return (
      <ul className="space-y-0.5">
        {shown.map((it, i) => <li key={i} className="break-words">• {clean(str(it))}</li>)}
        {items.length > shown.length && <li className="text-neutral-500 dark:text-neutral-400">+{items.length - shown.length} more</li>}
      </ul>
    );
  }
  if (typeof value === "boolean") return <span>{value ? "yes" : "no"}</span>;
  if (value && typeof value === "object") {
    return <span className="break-words font-mono text-[13px]">{JSON.stringify(value)}</span>;
  }
  return <span className="break-words">{clean(str(value))}</span>;
}

function Expandable({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-2">
      <button onClick={() => setOpen((o) => !o)}
        className="text-sm font-medium text-sky-600 hover:underline dark:text-sky-400">
        {open ? "▾" : "▸"} {label}
      </button>
      {open && (
        <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-neutral-200 bg-neutral-50 p-2.5 font-mono text-sm leading-relaxed text-neutral-600 dark:border-neutral-800 dark:bg-neutral-950/50 dark:text-neutral-300">{text}</pre>
      )}
    </div>
  );
}

function fieldRows(stage: string, d: Record<string, unknown>): [string, React.ReactNode][] {
  const rows: [string, React.ReactNode][] = [];
  const add = (label: string, v: unknown) => {
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0)) return;
    rows.push([label, <Cell value={v} />]);
  };
  const node = str(d.node);
  if (node === "resolve_context") {
    add("intent", d.intent); add("feeling", d.emotional_read);
    add("relation", d.relation); add("refers to", d.refers_to);
    if (d.needs_live_info) add("needs live info", `yes — “${str(d.live_query)}”`);
    add("note", d.note);
    if (d.prompt_name) add("prompt", `${str(d.prompt_name)} v${str(d.prompt_managed_version)} · ${str(d.prompt_source)}`);
  } else if (node === "perceive") {
    add("persona used", d.persona_context); add("recent turns", d.recent_turns);
    if (d.emotion) add("voice emotion", d.emotion);
  } else if (node === "reflect_log") {
    add("action", d.action); add("tools available", d.available_tools);
    add("style flags", Array.isArray(d.style_flags) && d.style_flags.length ? d.style_flags : undefined);
  } else if (node === "respond") {
    add("model tier", d.model_tier); add("used context", d.context_used);
  } else if (stage === "retrieval") {
    add("episodic", d.episodic); add("facts", d.semantic_facts ?? d.semantic);
    add("preferences", d.preferences); add("procedures", d.procedural);
    add("entities", d.entities); add("recall source", d.recall_source);
    add("user context", d.user_context_signals);
  } else if (stage === "assembly") {
    add("complexity", d.complexity); add("prompt version", d.prompt_version);
    add("sections", d.sections); add("active traits", d.active_traits);
    add("user context", d.user_context_signals);
  } else if (stage === "judgment") {
    add("intent", d.intent); add("salience", d.salience); add("novelty", d.novelty);
    add("complexity", d.complexity); add("ambiguity", d.ambiguity); add("boundary", d.boundary_flag);
  } else if (stage === "reflection") {
    add("ran", d.ran); add("revised", d.revised);
    add("critique", d.critique); add("checked", d.checked);
  } else if (stage === "tool") {
    add("tool", d.tool); add("status", d.status); add("mode", d.mode);
    add("args", d.args); add("latency", d.latency_ms ? fmtMs(num(d.latency_ms)) : undefined);
  } else if (stage === "memory") {
    add("stored", d.semantic || d.episodic || d.trades);
  } else if (stage === "router") {
    add("tier", d.tier); add("model override", d.model_override);
  } else if (stage === "session") {
    if (d.total_ms) add("total", fmtMs(num(d.total_ms)));
  }
  return rows;
}

function longFields(d: Record<string, unknown>): [string, string][] {
  const out: [string, string][] = [];
  const add = (label: string, v: unknown) => {
    const s = typeof v === "string" ? v : v ? JSON.stringify(v, null, 2) : "";
    if (s.trim()) out.push([label, s]);
  };
  add("draft", d.draft);
  if (!d.revised_text) add("critique (full)", d.critique);
  add("revised reply", d.revised_text);
  add("system prompt", d.system_prompt); add("trait text", d.trait_text);
  add("tool result", d.result);
  return out;
}

function stageMeta(stage: string, node: string): { title: string; sub: string; dot: string } {
  const gray = "bg-neutral-300 dark:bg-neutral-600";
  if (stage === "reasoning") {
    const m: Record<string, [string, string]> = {
      perceive: ["Perceived the message", "read persona + recent context"],
      resolve_context: ["Worked out intent & context", "what you meant + what it connects to"],
      respond: ["Reasoned & responded", "the main thinking step"],
      reflect_log: ["Logged the decision", "action + why-not tools"],
    };
    const [title, sub] = m[node] ?? ["Reasoned", node];
    return { title, sub, dot: "bg-sky-400" };
  }
  const map: Record<string, [string, string, string]> = {
    session: ["Turn", "", gray],
    vad: ["Detected speech", "", gray],
    stt: ["Transcribed", "speech → text", gray],
    endpoint: ["Endpointed", "decided you finished", gray],
    emotion: ["Read your tone", "acoustic emotion", "bg-rose-300"],
    retrieval: ["Recalled from memory", "before reasoning", "bg-teal-400"],
    assembly: ["Built the prompt", "", "bg-indigo-400"],
    router: ["Chose a model", "", gray],
    judgment: ["Judged the message", "salience · novelty · intent", "bg-indigo-400"],
    reflection: ["Reviewed its own draft", "self-reflection", "bg-fuchsia-400"],
    memory: ["Saved to memory", "", "bg-teal-400"],
    generation: ["Produced the reply", "", "bg-emerald-400"],
    response: ["Replied", "", "bg-emerald-400"],
    tool: ["Used a tool", "", "bg-amber-400"],
    barge_in: ["Interrupted", "stopped + listened", "bg-rose-400"],
    error: ["Error", "", "bg-rose-500"],
  };
  const [title, sub, dot] = map[stage] ?? [stage, "", gray];
  return { title, sub, dot };
}

// Only the empty session START marker is hidden (its text is the "You" line / turn
// header). EVERY other real pipeline span is shown, so no trace goes missing.
function isNoise(e: TraceEvent): boolean {
  return e.stage === "session" && !e.data?.total_ms;
}

function Feedback({ sessionId, turn }: { sessionId: string; turn: number }) {
  const [sent, setSent] = useState<"up" | "down" | null>(null);
  const [noting, setNoting] = useState(false);
  const [note, setNote] = useState("");
  async function submit(rating: "up" | "down", withNote = "") {
    setSent(rating);
    await sendFeedback({ session_id: sessionId, turn_id: String(turn), rating, note: withNote }).catch(() => {});
  }
  return (
    <div className="flex items-center gap-2 py-3">
      <span className="text-[15px] text-neutral-500 dark:text-neutral-400">Rate this turn</span>
      <button onClick={() => void submit("up")}
        className={`rounded-lg px-2 py-1 text-sm ${sent === "up" ? "bg-emerald-100 dark:bg-emerald-900/50" : "hover:bg-neutral-100 dark:hover:bg-neutral-800"}`}>👍</button>
      <button onClick={() => { setSent("down"); setNoting(true); }}
        className={`rounded-lg px-2 py-1 text-sm ${sent === "down" ? "bg-rose-100 dark:bg-rose-900/50" : "hover:bg-neutral-100 dark:hover:bg-neutral-800"}`}>👎</button>
      {noting && (
        <form onSubmit={(e) => { e.preventDefault(); setNoting(false); void submit("down", note); }}
          className="flex flex-1 items-center gap-2">
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="what went wrong? (optional)"
            className="flex-1 rounded-lg border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700" />
          <button type="submit" className="text-sm text-sky-600 hover:underline">save</button>
        </form>
      )}
    </div>
  );
}

function Pill({ children, tone }: { children: React.ReactNode; tone?: "fuchsia" | "rose" }) {
  const c = tone === "fuchsia"
    ? "bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-900/40 dark:text-fuchsia-300"
    : tone === "rose"
    ? "bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300"
    : "bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300";
  return <span className={`rounded-md px-1.5 py-0.5 text-sm font-medium ${c}`}>{children}</span>;
}

function purposeBadge(p: string): string {
  if (p === "context_intent") return "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300";
  if (p.startsWith("response")) return "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300";
  if (p === "search_summarize" || p === "tool_react") return "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300";
  if (p === "judge") return "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300";
  if (p.includes("reflect") || p === "style_rewrite" || p === "disclosure_polish") return "bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-900/40 dark:text-fuchsia-300";
  if (p.includes("memory") || p === "compaction") return "bg-teal-100 text-teal-700 dark:bg-teal-900/40 dark:text-teal-300";
  return "bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300";
}
function prettyPurpose(p: string): string {
  return ({
    context_intent: "intent & context", response: "response", response_repair: "repair reply",
    response_plain: "plain reply", search_summarize: "summarize search", tool_react: "tool step",
    judge: "judge", memory_extraction: "extract memory", psych_consolidation: "psych", self_model: "self-model",
    compaction: "compaction", delivery_relevance: "delivery", project_insight: "project insight",
    style_rewrite: "style rewrite", disclosure_polish: "disclosure",
  } as Record<string, string>)[p] || p || "call";
}
function shortModel(m: string): string { return m.split("/").pop() ?? m; }
function fmtMs(v: unknown): string { const n = num(v); return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`; }
function fmtOffset(ms: number): string { return ms >= 1000 ? `+${(ms / 1000).toFixed(1)}s` : `+${Math.round(ms)}ms`; }
function fmtNum(n: number): string { return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n); }
function fmtCost(v: unknown): string { const n = num(v); return n > 0 ? n.toFixed(4) : "0"; }
function clean(s: string): string { return s.replace(/^- /, "").trim(); }
function num(v: unknown): number { return typeof v === "number" ? v : typeof v === "string" ? parseFloat(v) || 0 : 0; }
function find(spans: TraceEvent[], stage: string): TraceEvent | undefined { return spans.find((e) => e.stage === stage); }
function str(v: unknown): string { return typeof v === "string" ? v : v === undefined || v === null ? "" : String(v); }
