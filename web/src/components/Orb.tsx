import type { TurnState } from "../lib/types";

const RING: Record<TurnState, string> = {
  idle: "from-slate-600 to-slate-700",
  listening: "from-emerald-400 to-teal-500",
  thinking: "from-amber-400 to-orange-500",
  speaking: "from-indigo-400 to-fuchsia-500",
};

// Amplitude-reactive talking orb: the inner core scales with the audio level,
// the halo pulses. State drives the colour. (Status lives in the top-right chip;
// the live caption sits under this, so the orb itself is just the visual.)
export function Orb({ state, level }: { state: TurnState; level: number }) {
  const scale = 1 + level * 0.35;
  const active = state !== "idle";
  return (
    <div className="flex select-none flex-col items-center gap-6">
      <div className="relative h-64 w-64" style={{ animation: "float 6s ease-in-out infinite" }}>
        <div
          className={`absolute inset-0 rounded-full bg-gradient-to-br ${RING[state]} blur-2xl transition-opacity duration-500`}
          style={{ opacity: active ? 0.55 + level * 0.4 : 0.25 }}
        />
        <div
          className={`absolute inset-6 rounded-full bg-gradient-to-br ${RING[state]} shadow-2xl transition-transform duration-75`}
          style={{ transform: `scale(${scale})` }}
        />
        <div className="absolute inset-16 rounded-full bg-slate-950/50 backdrop-blur-sm" />
        <div className="absolute inset-0 grid place-items-center">
          <div
            className="h-3 w-3 rounded-full bg-white/90 transition-transform duration-75"
            style={{ transform: `scale(${1 + level * 3})` }}
          />
        </div>
      </div>
    </div>
  );
}
