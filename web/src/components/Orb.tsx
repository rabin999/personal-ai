import type { TurnState } from "../lib/types";

const LABEL: Record<TurnState, string> = {
  idle: "Ready",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
};

const SUBLABEL: Record<TurnState, string> = {
  idle: "Waiting for you",
  listening: "I'm hearing you",
  thinking: "Composing a reply",
  speaking: "Talk over me to interrupt",
};

// Per-state visual palette. Gradients are inline so nothing depends on dynamic
// Tailwind class generation. `accent` drives the caption dot, ripples and glow.
interface Palette {
  sphere: string;
  glow: string;
  accent: string;
}

const PALETTE: Record<TurnState, Palette> = {
  idle: {
    // Calm skies — soft sky blue.
    sphere:
      "radial-gradient(circle at 33% 28%, #bae6fd 0%, #38bdf8 42%, #075985 100%)",
    glow: "#0ea5e9",
    accent: "#7dd3fc",
  },
  listening: {
    sphere:
      "radial-gradient(circle at 33% 28%, #a7f3d0 0%, #10b981 44%, #065f46 100%)",
    glow: "#10b981",
    accent: "#6ee7b7",
  },
  thinking: {
    sphere:
      "radial-gradient(circle at 33% 28%, #fde68a 0%, #f59e0b 44%, #b45309 100%)",
    glow: "#f59e0b",
    accent: "#fcd34d",
  },
  speaking: {
    // Fresh water — aqua/cyan.
    sphere:
      "radial-gradient(circle at 33% 28%, #a5f3fc 0%, #22d3ee 44%, #0e7490 100%)",
    glow: "#06b6d4",
    accent: "#67e8f9",
  },
};

// Amplitude-reactive talking orb. The sphere scales with the audio `level`
// (0..1) when it's the user's or companion's turn to make sound; `state` drives
// colour, ripples and the caption. Idle breathes; thinking spins a sheen.
export function Orb({ state, level }: { state: TurnState; level: number }) {
  const p = PALETTE[state];
  const reactive = state === "listening" || state === "speaking";
  const amp = reactive ? level : 0;
  const scale = 1 + amp * 0.28;
  const idle = state === "idle";
  const thinking = state === "thinking";
  const rippling = reactive || thinking;

  return (
    <div className="flex select-none flex-col items-center gap-7">
      <div
        className="relative grid h-60 w-60 place-items-center sm:h-72 sm:w-72"
        style={{ animation: "orb-float 7s ease-in-out infinite" }}
      >
        {/* Ambient glow */}
        <div
          className="absolute inset-2 rounded-full blur-3xl transition-opacity duration-500"
          style={{
            background: p.glow,
            opacity: (idle ? 0.3 : 0.5) + amp * 0.35,
          }}
        />

        {/* Expanding ripples (listening / speaking / thinking) */}
        {rippling && (
          <>
            <Ripple color={p.accent} delay="0s" />
            <Ripple color={p.accent} delay="1.1s" />
          </>
        )}

        {/* Rotating conic sheen — most visible while thinking */}
        <div
          className="absolute inset-6 rounded-full transition-opacity duration-500"
          style={{
            background: `conic-gradient(from 0deg, transparent 0%, ${p.accent}88 25%, transparent 55%, ${p.accent}55 80%, transparent 100%)`,
            opacity: thinking ? 0.9 : 0.18,
            animation: `orb-spin ${thinking ? 2.6 : 9}s linear infinite`,
          }}
        />

        {/* Main sphere */}
        <div
          className="absolute inset-8 rounded-full shadow-2xl"
          style={{
            background: p.sphere,
            transform: `scale(${scale})`,
            transition: "transform 110ms ease-out",
            animation: idle ? "orb-breathe 4.5s ease-in-out infinite" : undefined,
            boxShadow: `0 20px 60px -12px ${p.glow}99`,
          }}
        >
          {/* Glass highlight */}
          <div
            className="absolute inset-0 rounded-full"
            style={{
              background:
                "radial-gradient(circle at 32% 26%, rgba(255,255,255,0.55) 0%, rgba(255,255,255,0) 42%)",
            }}
          />
          {/* Inner well */}
          <div className="absolute inset-[28%] rounded-full bg-black/25 backdrop-blur-sm" />
        </div>

        {/* Reactive core */}
        <div
          className="relative h-3 w-3 rounded-full bg-white"
          style={{
            transform: `scale(${1 + amp * 2.4 + (thinking ? 0.3 : 0)})`,
            transition: "transform 110ms ease-out",
            boxShadow: `0 0 16px 4px ${p.accent}`,
            animation: thinking ? "orb-breathe 1.4s ease-in-out infinite" : undefined,
          }}
        />
      </div>

      <div className="flex flex-col items-center gap-1">
        <div className="flex items-center gap-2">
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: p.accent, boxShadow: `0 0 8px ${p.accent}` }}
          />
          <span className="text-sm font-semibold tracking-wide text-slate-700 dark:text-slate-100">
            {LABEL[state]}
          </span>
        </div>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          {SUBLABEL[state]}
        </span>
      </div>
    </div>
  );
}

function Ripple({ color, delay }: { color: string; delay: string }) {
  return (
    <div
      className="absolute inset-8 rounded-full"
      style={{
        border: `2px solid ${color}`,
        animation: "orb-ripple 2.2s ease-out infinite",
        animationDelay: delay,
        opacity: 0,
      }}
    />
  );
}
