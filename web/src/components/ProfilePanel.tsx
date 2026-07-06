import { useEffect, useState } from "react";
import { fetchProfile, type UserProfile } from "../lib/profile";

interface Props {
  open: boolean;
  token: string;
  onClose: () => void;
  onSignOut: () => void;
}

// Slide-over profile panel. Fetches the resolved user's static record from
// `/api/me` each time it opens and renders it read-only. The bearer token is the
// one the app already holds (also used by the WebSocket auth handshake).
export function ProfilePanel({ open, token, onClose, onSignOut }: Props) {
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setStatus("loading");
    setError("");
    fetchProfile(token)
      .then((p) => !cancelled && (setProfile(p), setStatus("idle")))
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Something went wrong.");
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [open, token]);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40">
      {/* Scrim */}
      <div
        className="absolute inset-0 bg-slate-900/30 backdrop-blur-sm dark:bg-slate-950/50"
        onClick={onClose}
        aria-hidden
      />

      {/* Panel */}
      <aside
        role="dialog"
        aria-label="Your profile"
        className="absolute right-0 top-0 flex h-full w-full max-w-sm flex-col border-l border-slate-200 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-950"
      >
        <header className="flex items-center justify-between border-b border-slate-200 px-5 py-4 dark:border-slate-800">
          <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
            Your profile
          </h2>
          <button
            onClick={onClose}
            aria-label="Close profile"
            className="grid h-8 w-8 place-items-center rounded-lg text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </header>

        <div className="thin-scroll flex-1 overflow-y-auto px-5 py-5">
          {status === "loading" && <Skeleton />}

          {status === "error" && (
            <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300">
              {error}
            </div>
          )}

          {status === "idle" && profile && <ProfileBody p={profile} />}
        </div>

        <footer className="border-t border-slate-200 px-5 py-4 dark:border-slate-800">
          <button
            onClick={onSignOut}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-50 hover:text-slate-900 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />
            </svg>
            Sign out
          </button>
        </footer>
      </aside>
    </div>
  );
}

function ProfileBody({ p }: { p: UserProfile }) {
  const name = p.companion_name || "Companion";
  return (
    <div className="flex flex-col gap-6">
      {/* Identity */}
      <div className="flex items-center gap-3">
        <div className="grid h-12 w-12 shrink-0 place-items-center rounded-full bg-gradient-to-br from-indigo-500 to-fuchsia-500 text-base font-semibold text-white shadow-sm">
          {initials(p.user_id)}
        </div>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
            {p.user_id}
          </p>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Companion · <span className="font-medium text-slate-600 dark:text-slate-300">{name}</span>
          </p>
        </div>
      </div>

      {/* Communication prefs */}
      <Section title="Communication">
        <Meter label="Directness" value={num(p.comm_prefs.directness)} />
        <Meter
          label="Emotional scaffolding"
          value={num(p.comm_prefs.emotional_scaffolding)}
        />
      </Section>

      {/* Enabled traits */}
      <Section title="Traits">
        {Object.keys(p.traits_enabled).length === 0 ? (
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Using defaults — no per-user overrides.
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(p.traits_enabled).map(([id, on]) => (
              <span
                key={id}
                className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${
                  on
                    ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300"
                    : "bg-slate-100 text-slate-400 line-through dark:bg-slate-800 dark:text-slate-500"
                }`}
              >
                {prettify(id)}
              </span>
            ))}
          </div>
        )}
      </Section>

      {/* Audio prefs */}
      <Section title="Audio">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5">
          <Stat label="VAD threshold" value={fmt(p.audio_prefs.vad_threshold)} />
          <Stat
            label="VAD range"
            value={`${fmt(p.audio_prefs.vad_min)}–${fmt(p.audio_prefs.vad_max)}`}
          />
          <Stat label="Short pause" value={ms(p.audio_prefs.endpoint_short_pause_ms)} />
          <Stat label="Long pause" value={ms(p.audio_prefs.endpoint_long_pause_ms)} />
        </dl>
        <div className="mt-3 flex flex-wrap gap-1.5">
          <Toggle label="AEC" on={p.audio_prefs.aec} />
          <Toggle label="Noise suppress" on={p.audio_prefs.noise_suppress} />
          <Toggle label="AGC" on={p.audio_prefs.agc} />
        </div>
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
        {title}
      </h3>
      {children}
    </section>
  );
}

function Meter({ label, value }: { label: string; value: number | null }) {
  const pct = value === null ? 0 : Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="text-slate-600 dark:text-slate-300">{label}</span>
        <span className="font-medium tabular-nums text-slate-500 dark:text-slate-400">
          {value === null ? "—" : `${pct}%`}
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
        <div
          className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-fuchsia-500 transition-[width]"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-[11px] text-slate-400 dark:text-slate-500">{label}</dt>
      <dd className="text-sm font-medium tabular-nums text-slate-700 dark:text-slate-200">
        {value}
      </dd>
    </div>
  );
}

function Toggle({ label, on }: { label: string; on?: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ${
        on
          ? "bg-indigo-100 text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300"
          : "bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${on ? "bg-indigo-500" : "bg-slate-400 dark:bg-slate-600"}`} />
      {label}
    </span>
  );
}

function Skeleton() {
  return (
    <div className="flex animate-pulse flex-col gap-6">
      <div className="flex items-center gap-3">
        <div className="h-12 w-12 rounded-full bg-slate-200 dark:bg-slate-800" />
        <div className="flex flex-1 flex-col gap-2">
          <div className="h-3 w-2/3 rounded bg-slate-200 dark:bg-slate-800" />
          <div className="h-2.5 w-1/2 rounded bg-slate-200 dark:bg-slate-800" />
        </div>
      </div>
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex flex-col gap-2">
          <div className="h-2.5 w-24 rounded bg-slate-200 dark:bg-slate-800" />
          <div className="h-8 w-full rounded-lg bg-slate-100 dark:bg-slate-800/60" />
        </div>
      ))}
    </div>
  );
}

// ── formatting helpers ────────────────────────────────────────────────────
function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function fmt(v: unknown): string {
  return typeof v === "number" ? String(v) : "—";
}
function ms(v: unknown): string {
  return typeof v === "number" ? `${v} ms` : "—";
}
function prettify(id: string): string {
  return id.replace(/[_-]+/g, " ");
}
function initials(id: string): string {
  const s = id.replace(/^u[_-]?/i, "");
  const m = s.match(/[a-z]+/i);
  return (m ? m[0].slice(0, 2) : s.slice(0, 2)).toUpperCase() || "U";
}
