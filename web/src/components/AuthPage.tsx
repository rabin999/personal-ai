import { ThemeToggle } from "./ThemeToggle";
import type { ThemePref } from "../lib/theme";
import type { ReactNode } from "react";

interface Props {
  themePref: ThemePref;
  onThemeChange: (pref: ThemePref) => void;
  onGoogle: () => void;
}

// What the companion can DO — concrete capabilities, each with a small illustration,
// so a first-time visitor knows what they're signing into.
const FEATURES: { art: ReactNode; title: string; body: string }[] = [
  {
    art: <ArtTalk />,
    title: "Just talk, naturally",
    body: "Voice-first — speak like you would to a friend, interrupt anytime, and hear a warm human reply.",
  },
  {
    art: <ArtRemember />,
    title: "Remembers you",
    body: "Recalls your past chats, preferences, and the people and details that matter — across sessions.",
  },
  {
    art: <ArtThink />,
    title: "Thinks before it replies",
    body: "Reasons about what you said and checks its own answer — not a reflexive chatbot one-liner.",
  },
  {
    art: <ArtSearch />,
    title: "Looks things up live",
    body: "Searches the web for current news and facts when you ask, and tells you honestly when it doesn't know.",
  },
];

// How it HELPS — benefit-oriented, the "why bother" for the user.
const BENEFITS = [
  "A judgment-free companion to talk through your day, ideas, or how you feel",
  "Remembers what matters, so you never have to repeat yourself",
  "Warm and human, but professional — good company for students and professionals alike",
  "Private to you — your data is yours alone, and you can wipe it anytime",
];

// Real sign-in / sign-up screen. Split-screen: the story scrolls on the LEFT, the sign-in
// panel stays FIXED on the right (only the left side scrolls). Theme-aware; on mobile it
// cleanly stacks into one scrolling column.
export function AuthPage({ themePref, onThemeChange, onGoogle }: Props) {
  const failed = new URLSearchParams(window.location.search).get("error");

  return (
    <div className="relative min-h-[100dvh] overflow-x-hidden bg-slate-50 text-slate-900 lg:flex lg:h-screen lg:overflow-hidden dark:bg-slate-950 dark:text-slate-100">
      <div className="absolute right-4 top-4 z-20">
        <ThemeToggle pref={themePref} onChange={onThemeChange} />
      </div>

      {/* ── LEFT: the story — what it is / can do / how it helps (scrolls) ─── */}
      <section className="relative order-2 flex flex-1 items-start overflow-x-hidden px-6 py-16 sm:px-10 lg:order-1 lg:h-screen lg:overflow-y-auto lg:px-16 lg:py-20 xl:px-24">
        {/* Soft decorative glow for depth (Dribbble-ish), kept subtle */}
        <div className="pointer-events-none absolute -left-24 -top-24 h-96 w-96 rounded-full bg-sky-400/20 blur-3xl dark:bg-sky-500/10" />
        <div className="pointer-events-none absolute bottom-0 left-1/3 h-80 w-80 rounded-full bg-cyan-300/15 blur-3xl dark:bg-cyan-500/10" />

        <div className="relative mx-auto w-full max-w-2xl lg:mx-0 lg:my-auto">
          {/* What it can do */}
          <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
            What{" "}
            <span className="bg-gradient-to-r from-sky-500 to-cyan-500 bg-clip-text text-transparent">
              Asaathi
            </span>{" "}
            can do
          </h2>
          <p className="mt-3 max-w-xl text-lg leading-relaxed text-slate-600 dark:text-slate-300">
            More than a chatbot — it listens, thinks, remembers, and talks like a friend.
          </p>
          <div className="mt-8 grid gap-4 sm:grid-cols-2">
            {FEATURES.map((f) => (
              <div
                key={f.title}
                className="group rounded-2xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur-sm transition-all hover:-translate-y-0.5 hover:border-sky-300/70 hover:shadow-lg hover:shadow-sky-600/5 dark:border-slate-800 dark:bg-slate-900/50 dark:hover:border-sky-500/40"
              >
                <div className="mb-3 h-14 w-14">{f.art}</div>
                <h3 className="text-base font-semibold">{f.title}</h3>
                <p className="mt-1.5 text-[15px] leading-relaxed text-slate-500 dark:text-slate-400">
                  {f.body}
                </p>
              </div>
            ))}
          </div>

          {/* How it helps */}
          <div className="mt-10">
            <h2 className="text-sm font-semibold uppercase tracking-[0.14em] text-slate-400 dark:text-slate-500">
              How it helps you
            </h2>
            <ul className="mt-4 space-y-3">
              {BENEFITS.map((b) => (
                <li key={b} className="flex items-start gap-3 text-base text-slate-600 dark:text-slate-300">
                  <CheckIcon />
                  <span>{b}</span>
                </li>
              ))}
            </ul>
          </div>

          {/* Connector to the architecture explainer */}
          <a
            href="/how-it-works"
            className="group mt-8 inline-flex items-center gap-2 rounded-full border border-slate-200/80 bg-white/70 px-4 py-2.5 text-sm font-semibold text-slate-600 backdrop-blur-sm transition-all hover:border-sky-300 hover:text-sky-600 dark:border-slate-800 dark:bg-slate-900/50 dark:text-slate-300 dark:hover:border-sky-500/50 dark:hover:text-sky-400"
          >
            Curious how it works?
            <span className="text-sky-500 dark:text-sky-400">See the architecture</span>
            <span aria-hidden className="transition-transform group-hover:translate-x-0.5">→</span>
          </a>
        </div>
      </section>

      {/* ── RIGHT: isolated sign-in panel (fixed, does not scroll) ─────────── */}
      <section className="relative order-1 flex items-center justify-center border-slate-200 bg-white px-5 py-12 lg:order-2 lg:h-screen lg:w-[26rem] lg:shrink-0 lg:border-l xl:w-[28rem] dark:border-slate-800 dark:bg-slate-900/60">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_50%_at_50%_0%,rgba(14,165,233,0.10),transparent_70%)]" />
        <div className="relative w-full max-w-sm">
          {/* Brand + intro live WITH the sign-in (this side), so the auth panel carries the
              logo, name and value prop — and the left side is purely what-it-can-do. */}
          <div className="mb-8 flex flex-col items-center text-center">
            <div className="mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-gradient-to-br from-sky-500 to-cyan-500 text-white shadow-lg shadow-sky-600/25">
              <AsaathiMark className="h-8 w-8" />
            </div>
            <span className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-400 dark:text-slate-500">
              Asaathi
            </span>
            <h1 className="mt-2 text-2xl font-bold leading-tight tracking-tight sm:text-3xl">
              A friend you can just{" "}
              <span className="bg-gradient-to-r from-sky-500 to-cyan-500 bg-clip-text text-transparent">
                talk
              </span>{" "}
              to.
            </h1>
            <p className="mt-3 text-[15px] leading-relaxed text-slate-500 dark:text-slate-400">
              You speak, it listens — thinks before it answers, remembers you between
              conversations, and replies warmly in a real human voice. A companion, not an
              assistant.
            </p>
          </div>

          {failed && (
            <p className="mb-4 rounded-lg bg-red-50 px-3 py-2.5 text-center text-sm font-medium text-red-600 dark:bg-red-950/40 dark:text-red-400">
              Sign-in didn't complete. Please try again.
            </p>
          )}

          <button
            type="button"
            onClick={onGoogle}
            className="flex w-full items-center justify-center gap-3 rounded-2xl border border-slate-300 bg-white px-5 py-4 text-base font-semibold text-slate-700 shadow-sm transition-all hover:bg-slate-50 hover:shadow-md active:scale-[0.99] dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700/70"
          >
            <GoogleIcon />
            Continue with Google
          </button>

          <p className="mt-5 flex items-center justify-center gap-1.5 text-[13px] text-slate-400 dark:text-slate-500">
            <LockIcon />
            Private to you. Delete everything anytime.
          </p>
        </div>
      </section>
    </div>
  );
}

// ── Feature illustrations (inline SVG — self-contained, theme-safe) ──────────
function ArtTalk() {
  return (
    <svg viewBox="0 0 64 64" className="h-full w-full" fill="none">
      <circle cx="32" cy="32" r="30" fill="#0ea5e9" opacity="0.10" />
      <rect x="12" y="15" width="31" height="23" rx="8" fill="#0ea5e9" />
      <path d="M20 37l-3 8 10-6z" fill="#0ea5e9" />
      <circle cx="22" cy="26" r="2.4" fill="#fff" />
      <circle cx="29" cy="26" r="2.4" fill="#fff" />
      <circle cx="36" cy="26" r="2.4" fill="#fff" />
      <rect x="34" y="31" width="20" height="15" rx="6" fill="#22d3ee" />
      <path d="M49 45l2 6-8-4z" fill="#22d3ee" />
    </svg>
  );
}
function ArtRemember() {
  return (
    <svg viewBox="0 0 64 64" className="h-full w-full" fill="none">
      <circle cx="32" cy="32" r="30" fill="#0ea5e9" opacity="0.10" />
      <rect x="17" y="13" width="27" height="38" rx="6" fill="#fff" stroke="#0ea5e9" strokeWidth="2.6" />
      <path d="M33 13h9a2 2 0 0 1 2 2v13l-6.5-4-6.5 4V15a2 2 0 0 1 2-2z" fill="#22d3ee" />
      <line x1="23" y1="36" x2="38" y2="36" stroke="#93c5fd" strokeWidth="2.6" strokeLinecap="round" />
      <line x1="23" y1="43" x2="34" y2="43" stroke="#93c5fd" strokeWidth="2.6" strokeLinecap="round" />
    </svg>
  );
}
function ArtThink() {
  return (
    <svg viewBox="0 0 64 64" className="h-full w-full" fill="none">
      <circle cx="32" cy="32" r="30" fill="#0ea5e9" opacity="0.10" />
      <circle cx="32" cy="27" r="12" fill="#0ea5e9" />
      <path d="M27 36h10v3a2 2 0 0 1-2 2h-6a2 2 0 0 1-2-2v-3z" fill="#0369a1" />
      <line x1="29" y1="44" x2="35" y2="44" stroke="#0369a1" strokeWidth="2.4" strokeLinecap="round" />
      <path d="M32 21l1.6 3.6L37 26l-3.4 1.4L32 31l-1.6-3.6L27 26l3.4-1.4L32 21z" fill="#fff" />
      <g stroke="#22d3ee" strokeWidth="2.4" strokeLinecap="round">
        <line x1="16" y1="27" x2="12" y2="27" />
        <line x1="52" y1="27" x2="48" y2="27" />
        <line x1="20" y1="15" x2="17" y2="12" />
        <line x1="44" y1="15" x2="47" y2="12" />
      </g>
    </svg>
  );
}
function ArtSearch() {
  return (
    <svg viewBox="0 0 64 64" className="h-full w-full" fill="none">
      <circle cx="32" cy="32" r="30" fill="#0ea5e9" opacity="0.10" />
      <circle cx="28" cy="28" r="13" fill="#e0f2fe" stroke="#0ea5e9" strokeWidth="3" />
      <g stroke="#22d3ee" strokeWidth="2" fill="none">
        <ellipse cx="28" cy="28" rx="5.5" ry="13" />
        <line x1="15" y1="28" x2="41" y2="28" />
      </g>
      <line x1="38" y1="38" x2="49" y2="49" stroke="#0ea5e9" strokeWidth="4.5" strokeLinecap="round" />
    </svg>
  );
}

// ── Brand mark ───────────────────────────────────────────────────────────────
// Asaathi's own logo: a speech bubble (a companion you talk WITH) cradling a voice
// waveform (voice-first). Distinct to this app — not a stock mic. Inline SVG.
export function AsaathiMark({ className = "h-7 w-7" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {/* speech bubble with a soft tail */}
      <path d="M20 3.5H4A2.5 2.5 0 0 0 1.5 6v7.5A2.5 2.5 0 0 0 4 16h2.5v3.6L11.4 16H20a2.5 2.5 0 0 0 2.5-2.5V6A2.5 2.5 0 0 0 20 3.5z" />
      {/* voice waveform inside — the conversation */}
      <path d="M6.5 9.7v.6" />
      <path d="M9.5 7.6v4.4" />
      <path d="M12.5 6.4v6.8" />
      <path d="M15.5 8v3.6" />
      <path d="M18.5 9.4v1.2" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" className="mt-0.5 h-4 w-4 shrink-0 text-sky-500 dark:text-sky-400" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}
function LockIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="11" width="16" height="9" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  );
}

// Google "G" mark, official four-color paths (inline so no external asset).
function GoogleIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden style={{ height: "1.15rem", width: "1.15rem" }}>
      <path fill="#4285F4" d="M23.52 12.27c0-.79-.07-1.54-.2-2.27H12v4.51h6.47a5.53 5.53 0 0 1-2.4 3.63v3h3.88c2.27-2.09 3.57-5.17 3.57-8.87z" />
      <path fill="#34A853" d="M12 24c3.24 0 5.96-1.08 7.95-2.91l-3.88-3c-1.08.72-2.45 1.16-4.07 1.16-3.13 0-5.78-2.11-6.73-4.96H1.29v3.09A12 12 0 0 0 12 24z" />
      <path fill="#FBBC05" d="M5.27 14.29a7.2 7.2 0 0 1 0-4.58V6.62H1.29a12 12 0 0 0 0 10.76l3.98-3.09z" />
      <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.44-3.44A11.98 11.98 0 0 0 12 0 12 12 0 0 0 1.29 6.62l3.98 3.09C6.22 6.86 8.87 4.75 12 4.75z" />
    </svg>
  );
}
