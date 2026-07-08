import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { fetchProfile, updatePrefs, type LocaleProfile, type UserProfile } from "../lib/profile";
import { deleteAccount } from "../lib/session";

interface Props {
  open: boolean;
  onClose: () => void;
  onSignOut: () => void;
}

// Slide-over profile panel. Fetches the resolved user's record from `/api/me`
// (session cookie / Google SSO) each time it opens and renders it read-only.
export function ProfilePanel({ open, onClose, onSignOut }: Props) {
  const navigate = useNavigate();
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setStatus("loading");
    setError("");
    fetchProfile()
      .then((p) => !cancelled && (setProfile(p), setStatus("idle")))
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Something went wrong.");
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  // Portal to <body>: the panel is rendered from inside the app header, which has
  // backdrop-blur (a filter) — a filtered ancestor makes `position: fixed` resolve
  // to the header's box, not the viewport, so the panel only covered the header and
  // the page bled through (the "glitching / not properly opened" bug). Rendering
  // into <body> escapes that containing block.
  return createPortal(
    <div className="fixed inset-0 z-50">
      {/* Scrim — solid translucent (no backdrop-filter: it caused a mobile
          compositing glitch where the panel body rendered transparent). */}
      <div
        className="absolute inset-0 bg-slate-900/40 dark:bg-slate-950/60"
        onClick={onClose}
        aria-hidden
      />

      {/* Panel — opaque, above the scrim, slides in from the right. Wider than
          before (room to grow) and mobile-first: full width up to a sane max. */}
      <aside
        role="dialog"
        aria-label="Your profile"
        className="absolute right-0 top-0 z-10 flex h-full w-full max-w-md flex-col border-l border-slate-200 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-950"
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
          {/* Mobile-only navigation — the header nav is hidden on small screens. */}
          <nav className="mb-5 grid grid-cols-1 gap-1.5 sm:hidden">
            {(
              [
                ["/conversations", "Conversations"],
                ["/memories", "Memories"],
              ] as const
            ).map(([to, label]) => (
              <button
                key={to}
                onClick={() => {
                  onClose();
                  navigate(to);
                }}
                className="flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2.5 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-50 dark:border-slate-800 dark:text-slate-200 dark:hover:bg-slate-800/60"
              >
                {label}
                <svg viewBox="0 0 24 24" className="h-4 w-4 text-slate-400" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M9 6l6 6-6 6" />
                </svg>
              </button>
            ))}
          </nav>

          {status === "loading" && <Skeleton />}

          {status === "error" && (
            <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300">
              {error}
            </div>
          )}

          {status === "idle" && profile && <ProfileBody p={profile} />}
        </div>

        <footer className="space-y-2 border-t border-slate-200 px-5 py-4 dark:border-slate-800">
          <button
            onClick={onSignOut}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-50 hover:text-slate-900 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />
            </svg>
            Sign out
          </button>
          <DeleteAccount />
        </footer>
      </aside>
    </div>,
    document.body,
  );
}

// Danger action: permanently delete the account + ALL data. Two-step so it can't
// be triggered by accident; on confirm it wipes everything and returns to login.
function DeleteAccount() {
  const [arming, setArming] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!arming) {
    return (
      <button
        onClick={() => setArming(true)}
        className="w-full rounded-lg px-3 py-2 text-sm font-medium text-rose-600 transition-colors hover:bg-rose-50 dark:text-rose-400 dark:hover:bg-rose-950/40"
      >
        Delete my profile
      </button>
    );
  }
  return (
    <div className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm dark:border-rose-900/50 dark:bg-rose-950/30">
      <p className="text-rose-700 dark:text-rose-300">
        Delete your profile and <b>all</b> your data (conversations, memories, traces)?
        This can’t be undone.
      </p>
      <div className="mt-2.5 flex gap-2">
        <button
          disabled={busy}
          onClick={() => { setBusy(true); void deleteAccount(); }}
          className="flex-1 rounded-lg bg-rose-600 px-3 py-2 text-sm font-semibold text-white hover:bg-rose-700 disabled:opacity-60"
        >
          {busy ? "Deleting…" : "Yes, delete everything"}
        </button>
        <button
          disabled={busy}
          onClick={() => setArming(false)}
          className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-600 hover:bg-white dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function ProfileBody({ p }: { p: UserProfile }) {
  const name = p.companion_name || "Asaathi";
  return (
    <div className="flex flex-col gap-6">
      {/* Identity */}
      <div className="flex items-center gap-3">
        <div className="grid h-12 w-12 shrink-0 place-items-center rounded-full bg-gradient-to-br from-sky-500 to-cyan-500 text-base font-semibold text-white shadow-sm">
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

      {/* Voice speed (C7) + locale (C5) — editable */}
      <VoiceAndLocale p={p} />
    </div>
  );
}

// Editable voice-speed slider (C7) + locale fields (C5): the companion frames
// times/units/currency for the user, and speaks at their chosen pace. Saves to
// /api/prefs; the speed applies on the next reply, locale on the next turn.
function VoiceAndLocale({ p }: { p: UserProfile }) {
  const [speed, setSpeed] = useState<number>(numOr(p.audio_prefs.voice_speed, 1.0));
  const [loc, setLoc] = useState<LocaleProfile>({ ...p.locale });
  const [saved, setSaved] = useState<"" | "saving" | "ok" | "err">("");

  // Reflect the new speed on the LIVE conversation immediately (the AudioPlayer in
  // CompanionPage listens for this) — not just on the next connect.
  function applyLive(v: number) {
    window.dispatchEvent(new CustomEvent("asaathi:voice-speed", { detail: v }));
  }

  async function save(patch: { voice_speed?: number; locale?: LocaleProfile }) {
    setSaved("saving");
    try {
      await updatePrefs(patch);
      setSaved("ok");
      setTimeout(() => setSaved(""), 1500);
    } catch {
      setSaved("err");
    }
  }

  const field = (key: keyof LocaleProfile, label: string, placeholder: string) => (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] text-slate-400 dark:text-slate-500">{label}</span>
      <input
        value={loc[key] ?? ""}
        onChange={(e) => setLoc({ ...loc, [key]: e.target.value })}
        onBlur={() => save({ locale: loc })}
        placeholder={placeholder}
        className="rounded-md border border-slate-200 bg-transparent px-2 py-1 text-sm text-slate-800 focus:border-sky-400 focus:outline-none dark:border-slate-700 dark:text-slate-100"
      />
    </label>
  );

  return (
    <Section title="Voice & you">
      <div>
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
            Speaking speed
          </span>
          <span className="font-mono text-xs text-slate-500">{speed.toFixed(2)}×</span>
        </div>
        <input
          type="range"
          min={0.8}
          max={1.5}
          step={0.05}
          value={speed}
          onChange={(e) => {
            const v = parseFloat(e.target.value);
            setSpeed(v);
            applyLive(v); // hear it change immediately, mid-conversation
          }}
          onMouseUp={() => save({ voice_speed: speed })}
          onTouchEnd={() => save({ voice_speed: speed })}
          className="mt-1.5 w-full accent-sky-500"
        />
        <div className="flex justify-between text-[10px] text-slate-400">
          <span>slower</span><span>1.0× default</span><span>faster</span>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2.5">
        {field("city", "City", "Kathmandu")}
        {field("country", "Country", "Nepal")}
        {field("timezone", "Timezone", "Asia/Kathmandu")}
        {field("currency", "Currency", "NPR")}
        <label className="flex flex-col gap-1">
          <span className="text-[11px] text-slate-400 dark:text-slate-500">Units</span>
          <select
            value={loc.units ?? ""}
            onChange={(e) => {
              const next = { ...loc, units: e.target.value as LocaleProfile["units"] };
              setLoc(next);
              void save({ locale: next });
            }}
            className="rounded-md border border-slate-200 bg-transparent px-2 py-1 text-sm text-slate-800 focus:border-sky-400 focus:outline-none dark:border-slate-700 dark:text-slate-100"
          >
            <option value="">—</option>
            <option value="metric">Metric</option>
            <option value="imperial">Imperial</option>
          </select>
        </label>
        {field("language", "Language", "en")}
      </div>
      {saved === "saving" && <p className="text-[11px] text-slate-400">Saving…</p>}
      {saved === "ok" && <p className="text-[11px] text-emerald-500">Saved</p>}
      {saved === "err" && <p className="text-[11px] text-red-500">Couldn't save</p>}
    </Section>
  );
}

function numOr(v: unknown, d: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : d;
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
          className="h-full rounded-full bg-gradient-to-r from-sky-500 to-cyan-500 transition-[width]"
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
          ? "bg-sky-100 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300"
          : "bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${on ? "bg-sky-500" : "bg-slate-400 dark:bg-slate-600"}`} />
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
