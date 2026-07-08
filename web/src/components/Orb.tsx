import type { TurnState } from "../lib/types";

// Number of flame columns. Odd so there's a single tallest core column.
const N = 13;

// Bell-shaped base height per column (0..1): the centre burns tallest, the edges
// are low embers → the whole thing reads as a flame, not a bar chart.
function shape(i: number): number {
  const t = (i - (N - 1) / 2) / ((N - 1) / 2); // -1 … 1
  return 0.22 + (1 - t * t) * 0.78; // 0.22 … 1.0
}

// A voice-reactive FIRE: vertical flame columns that rise and flicker with the
// audio `level`, over an ember glow that bleeds into the page (no box, no mask,
// no hard edge). Calm and low while idle; taller and faster when active/thinking.
export function Orb({ state, level }: { state: TurnState; level: number }) {
  const idle = state === "idle";
  const thinking = state === "thinking";
  const reactive = state === "listening" || state === "speaking";
  const amp = reactive ? level : 0;

  // Overall energy of the fire: a low calm ember at idle, medium when busy,
  // driven by the voice level when live.
  const energy = idle ? 0.28 : thinking ? 0.82 : 0.58 + amp * 0.95;
  // Flicker speed: slow calm ember at idle, fast when thinking/loud.
  const flickerMul = idle ? 1.9 : thinking ? 0.6 : 1 - amp * 0.35;

  return (
    <div className="pointer-events-none relative flex w-full max-w-2xl select-none items-end justify-center">
      {/* Ember bloom behind the flames — a soft radial that FADES to transparent,
          so the fire dissolves into the page instead of sitting in a box. */}
      <div
        className="absolute inset-x-0 bottom-0 -z-10 h-[150%] transition-opacity duration-700"
        style={{
          background:
            "radial-gradient(58% 62% at 50% 100%, rgba(251,146,60,0.42), rgba(239,68,68,0.16) 42%, transparent 72%)",
          filter: "blur(34px)",
          opacity: (idle ? 0.5 : 0.8) + amp * 0.4,
        }}
      />

      {/* Heat haze — a wider, slower wash that wobbles, adds depth beyond the flames. */}
      <div
        className="absolute inset-x-[-15%] bottom-0 -z-10 h-[120%]"
        style={{
          background:
            "radial-gradient(45% 55% at 50% 100%, rgba(250,204,21,0.22), transparent 70%)",
          filter: "blur(48px)",
          animation: `heat ${(4.5 * flickerMul).toFixed(1)}s ease-in-out infinite`,
        }}
      />

      {/* The flames — columns overlap and blur into one continuous, flickering
          fire body rather than a row of separate equalizer bars. */}
      <div className="relative flex h-72 w-full items-end justify-center sm:h-96 lg:h-[30rem]">
        {/* Hot base: a bright glowing root strip that unifies the flame bottoms. */}
        <div
          className="absolute inset-x-[8%] bottom-0 h-6 rounded-full"
          style={{
            background:
              "linear-gradient(to top, #fff7ed, #fde68a 40%, rgba(251,146,60,0.5) 75%, transparent)",
            filter: "blur(7px)",
            opacity: 0.7 + energy * 0.3,
          }}
        />
        {Array.from({ length: N }).map((_, i) => {
          const base = shape(i);
          // Height as a % of the container so it scales with the (large) space.
          const pct = 12 + base * 80 * energy;
          const dur = (0.55 + ((i * 7) % 5) * 0.14) * flickerMul;
          return (
            <span
              key={i}
              className="block"
              style={{
                width: "10%",
                marginLeft: i === 0 ? 0 : "-2.7%", // overlap → merge into one flame
                height: `${Math.min(100, pct)}%`,
                background:
                  "linear-gradient(to top, #ffffff, #fff7ed 8%, #fde68a 20%, #fbbf24 36%, #fb923c 56%, #ef4444 76%, #b91c1c 90%, transparent)",
                borderRadius: "50% 50% 46% 46% / 70% 70% 30% 30%",
                filter: "blur(5px)",
                transformOrigin: "bottom center",
                transition: "height 110ms ease-out",
                animation: `flame ${dur.toFixed(2)}s ease-in-out ${(i * 0.06).toFixed(2)}s infinite`,
              }}
            />
          );
        })}
      </div>

      {/* Rising sparks/embers — only when there's real energy (not idle). */}
      {!idle && (
        <div className="absolute inset-0 -z-0">
          {[12, 24, 38, 46, 54, 62, 76, 88].map((x, i) => (
            <span
              key={i}
              className="absolute h-1.5 w-1.5 rounded-full"
              style={{
                left: `${x}%`,
                bottom: "8%",
                background: i % 2 ? "#fef3c7" : "#fdba74",
                boxShadow: "0 0 8px 2px rgba(251,146,60,0.9)",
                animation: `ember-rise ${(2.4 + (i % 3) * 0.8).toFixed(1)}s linear ${(i * 0.4).toFixed(1)}s infinite`,
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
