import { ThemeToggle } from "./ThemeToggle";
import type { ThemePref } from "../lib/theme";
import type { ReactNode } from "react";

interface Props {
  themePref: ThemePref;
  onThemeChange: (pref: ThemePref) => void;
  onGoogle: () => void;
}

// What the companion can DO — concrete capabilities, so a first-time visitor knows
// what they're signing into (user request: explain the app on the login page).
const FEATURES: { icon: ReactNode; title: string; body: string }[] = [
  {
    icon: <MicIcon />,
    title: "Just talk, naturally",
    body: "Voice-first — speak like you would to a friend, interrupt anytime, and hear a warm human reply.",
  },
  {
    icon: <MemoryIcon />,
    title: "Remembers you",
    body: "Recalls your past chats, preferences, and the people and details that matter — across sessions.",
  },
  {
    icon: <ThinkIcon />,
    title: "Thinks before it replies",
    body: "Reasons about what you said and checks its own answer — not a reflexive chatbot one-liner.",
  },
  {
    icon: <SearchIcon />,
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

// Real sign-in / sign-up screen. Google SSO is the single flow (the callback creates the
// account on first login and signs in on return). Two-column story + sign-in on desktop,
// cleanly stacked and scrollable on mobile; theme-aware, no horizontal overflow.
export function AuthPage({ themePref, onThemeChange, onGoogle }: Props) {
  const failed = new URLSearchParams(window.location.search).get("error");

  return (
    <div className="relative min-h-[100dvh] overflow-x-hidden bg-slate-50 text-slate-900 lg:flex dark:bg-slate-950 dark:text-slate-100">
      <div className="absolute right-4 top-4 z-20">
        <ThemeToggle pref={themePref} onChange={onThemeChange} />
      </div>

      {/* ── LEFT: the story — what it is / can do / how it helps ───────────── */}
      <section className="relative order-2 flex flex-1 items-start overflow-hidden px-6 py-16 sm:px-10 lg:order-1 lg:items-center lg:px-16 lg:py-20 xl:px-24">
        {/* Soft decorative glow for depth (Dribbble-ish), kept subtle */}
        <div className="pointer-events-none absolute -left-24 -top-24 h-96 w-96 rounded-full bg-sky-400/20 blur-3xl dark:bg-sky-500/10" />
        <div className="pointer-events-none absolute bottom-0 left-1/3 h-80 w-80 rounded-full bg-cyan-300/15 blur-3xl dark:bg-cyan-500/10" />

        <div className="relative mx-auto w-full max-w-2xl lg:mx-0">
          {/* Eyebrow: brand */}
          <div className="flex items-center gap-3">
            <div className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-sky-500 to-cyan-500 text-white shadow-lg shadow-sky-600/25">
              <AsaathiMark className="h-6 w-6" />
            </div>
            <span className="text-sm font-semibold uppercase tracking-[0.18em] text-slate-500 dark:text-slate-400">
              Asaathi
            </span>
          </div>

          {/* Hero */}
          <h1 className="mt-8 text-4xl font-bold leading-[1.08] tracking-tight sm:text-5xl xl:text-6xl">
            A friend you can
            <br className="hidden sm:block" />{" "}
            just{" "}
            <span className="bg-gradient-to-r from-sky-500 to-cyan-500 bg-clip-text text-transparent">
              talk
            </span>{" "}
            to.
          </h1>

          <p className="mt-6 max-w-xl text-lg leading-relaxed text-slate-600 sm:text-xl dark:text-slate-300">
            You speak, it listens — thinks before it answers, remembers you between
            conversations, and replies warmly in a real human voice. A companion, not an
            assistant.
          </p>

          {/* What it can do */}
          <div className="mt-10 grid gap-4 sm:grid-cols-2">
            {FEATURES.map((f) => (
              <div
                key={f.title}
                className="group rounded-2xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur-sm transition-all hover:-translate-y-0.5 hover:border-sky-300/70 hover:shadow-lg hover:shadow-sky-600/5 dark:border-slate-800 dark:bg-slate-900/50 dark:hover:border-sky-500/40"
              >
                <div className="mb-3 grid h-11 w-11 place-items-center rounded-xl bg-sky-500/10 text-sky-600 dark:bg-sky-400/10 dark:text-sky-400">
                  {f.icon}
                </div>
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
        </div>
      </section>

      {/* ── RIGHT: isolated sign-in panel ─────────────────────────────────── */}
      <section className="relative order-1 flex items-center justify-center border-slate-200 bg-white px-5 py-12 lg:sticky lg:top-0 lg:order-2 lg:h-screen lg:w-[26rem] lg:shrink-0 lg:self-start lg:border-l xl:w-[28rem] dark:border-slate-800 dark:bg-slate-900/60">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_50%_at_50%_0%,rgba(14,165,233,0.10),transparent_70%)]" />
        <div className="relative w-full max-w-sm">
          {/* Brand mark lives in the sign-in panel so the mobile-first (top) view is branded */}
          <div className="mb-8 flex flex-col items-center text-center">
            <div className="mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-gradient-to-br from-sky-500 to-cyan-500 text-white shadow-lg shadow-sky-600/25">
              <AsaathiMark className="h-8 w-8" />
            </div>
            <h2 className="text-2xl font-semibold tracking-tight">Start talking</h2>
            <p className="mt-2 text-base leading-relaxed text-slate-500 dark:text-slate-400">
              Sign in to begin — your companion remembers you from here on.
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

// ── Brand mark ───────────────────────────────────────────────────────────────
// Asaathi's own logo: a speech bubble (a companion you talk WITH) cradling a voice
// waveform (voice-first). Distinct to this app — not a stock mic. Inline SVG.
function AsaathiMark({ className = "h-7 w-7" }: { className?: string }) {
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

// ── Icons (inline so there's no external asset / network fetch) ──────────────
function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
      <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4M8 22h8" />
    </svg>
  );
}
function MemoryIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 5a3 3 0 0 0-3 3 3 3 0 0 0-1 5.83V16a3 3 0 0 0 4 2.83A3 3 0 0 0 16 16v-2.17A3 3 0 0 0 15 8a3 3 0 0 0-3-3z" />
      <path d="M9 8h.01M15 8h.01M9 13h6" />
    </svg>
  );
}
function ThinkIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m12 3 1.9 4.3L18 9l-4.1 1.7L12 15l-1.9-4.3L6 9l4.1-1.7L12 3z" />
      <path d="M18 15l.8 1.8 1.9.7-1.9.8-.8 1.7-.8-1.7-1.9-.8 1.9-.7.8-1.8z" />
    </svg>
  );
}
function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
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
