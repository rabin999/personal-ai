import { ThemeToggle } from "./ThemeToggle";
import type { ThemePref } from "../lib/theme";

interface Props {
  themePref: ThemePref;
  onThemeChange: (pref: ThemePref) => void;
  onGoogle: () => void;
}

// Real sign-in / sign-up screen. Google SSO is the single flow (the callback
// creates the account on first login and signs in on return). Mobile-first:
// full-height, generous tap targets, safe-area padding, no horizontal overflow.
export function AuthPage({ themePref, onThemeChange, onGoogle }: Props) {
  const failed = new URLSearchParams(window.location.search).get("error");

  return (
    <div className="relative flex min-h-[100dvh] items-center justify-center overflow-hidden bg-slate-50 px-5 py-10 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      {/* Ambient wash */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_45%_at_50%_12%,rgba(14,165,233,0.14),transparent_70%)]" />

      <div className="absolute right-4 top-4">
        <ThemeToggle pref={themePref} onChange={onThemeChange} />
      </div>

      <div className="relative w-full max-w-sm">
        {/* Brand */}
        <div className="mb-8 flex flex-col items-center text-center">
          <div className="mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-gradient-to-br from-sky-500 to-cyan-500 text-white shadow-lg shadow-sky-600/20">
            <svg viewBox="0 0 24 24" className="h-7 w-7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
              <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4M8 22h8" />
            </svg>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">Meet Asaathi</h1>
          <p className="mt-2 text-sm leading-relaxed text-slate-500 dark:text-slate-400">
            A voice-first friend that remembers you, adapts to how you feel, and
            keeps everything private to you.
          </p>
        </div>

        {failed && (
          <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-center text-xs font-medium text-red-600 dark:bg-red-950/40 dark:text-red-400">
            Sign-in didn't complete. Please try again.
          </p>
        )}

        <button
          type="button"
          onClick={onGoogle}
          className="flex w-full items-center justify-center gap-3 rounded-xl border border-slate-300 bg-white px-4 py-3.5 text-sm font-semibold text-slate-700 shadow-sm transition-all hover:bg-slate-50 active:scale-[0.99] dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700/70"
        >
          <GoogleIcon />
          Continue with Google
        </button>
      </div>
    </div>
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
