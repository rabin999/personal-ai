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

// Accent (label dot + glow tint) per state.
const ACCENT: Record<TurnState, string> = {
  idle: "#38bdf8",
  listening: "#34d399",
  thinking: "#a78bfa",
  speaking: "#22d3ee",
};

// Color-FUSION visualization: heavily-blurred colour blobs drift, scale and blend
// (screen) inside a dark panel into a living, shifting plasma. Reactive — the field
// intensifies with the audio `level` and speeds up while thinking. No circle, no
// bars: a modern gradient-mesh "fusion" that keeps attention during processing.
export function Orb({ state, level }: { state: TurnState; level: number }) {
  const accent = ACCENT[state];
  const reactive = state === "listening" || state === "speaking";
  const amp = reactive ? level : 0;
  const thinking = state === "thinking";
  const idle = state === "idle";
  // Animation pace: calm at idle, quicker when active, quickest while thinking.
  const pace = thinking ? 0.55 : reactive ? 0.78 : 1.2;
  const boost = thinking ? 0.22 : amp * 0.4; // extra blob opacity when busy

  const blob = (
    color: string,
    pos: string,
    keyframe: string,
    base: number,
    dur: number,
  ): React.CSSProperties => ({
    background: `radial-gradient(circle at center, ${color} 0%, ${color}00 68%)`,
    opacity: Math.min(0.95, base + boost),
    animation: `${keyframe} ${(dur * pace).toFixed(1)}s ease-in-out infinite`,
    ...JSON.parse(pos),
  });

  void blob;
  return (
    <div className="relative flex select-none items-center justify-center">
      <div
        className="relative h-80 w-80 sm:h-[28rem] sm:w-[28rem] lg:h-[34rem] lg:w-[34rem]"
        style={{ transform: `scale(${1 + amp * 0.06})`, transition: "transform 120ms ease-out" }}
      >
        {/* Wide outer glow, tinted by the state accent */}
        <div
          className="absolute -inset-10 blur-3xl transition-opacity duration-700"
          style={{
            background: `radial-gradient(circle at center, ${accent} 0%, ${accent}00 60%)`,
            opacity: (idle ? 0.18 : 0.34) + amp * 0.3,
          }}
        />

        {/* FREE-FLOATING plasma — masked so it dissolves into the page (no box). */}
        <div
          className="absolute inset-0"
          style={{
            WebkitMaskImage: "radial-gradient(circle at 50% 50%, #000 60%, transparent 92%)",
            maskImage: "radial-gradient(circle at 50% 50%, #000 60%, transparent 92%)",
          }}
        >
          {/* FULL base fill — overlapping colour fields on a dark core so the plasma
              is COMPLETE (never patchy/empty); hue-cycles so it's always shifting. */}
          <div
            className="absolute inset-0"
            style={{
              background:
                "radial-gradient(circle at 30% 30%, rgba(56,189,248,0.9), transparent 52%), " +
                "radial-gradient(circle at 72% 30%, rgba(167,139,250,0.9), transparent 52%), " +
                "radial-gradient(circle at 32% 72%, rgba(34,211,238,0.85), transparent 52%), " +
                "radial-gradient(circle at 70% 70%, rgba(52,211,153,0.85), transparent 52%), " +
                "radial-gradient(circle at 50% 50%, rgba(2,6,23,0.55), rgba(2,6,23,0.95))",
              animation: `orb-hue ${thinking ? 7 : reactive ? 12 : 22}s linear infinite`,
            }}
          />

          {/* Moving blobs on top for depth/motion (less blur → more defined). */}
          <div className="absolute inset-0 [&>span]:absolute [&>span]:h-[60%] [&>span]:w-[60%] [&>span]:rounded-full [&>span]:blur-xl [&>span]:mix-blend-screen">
            <span style={{ left: "6%", top: "8%", background: "radial-gradient(circle, #38bdf8, transparent 64%)", opacity: 0.7, animation: `fusion-a ${(11 * pace).toFixed(1)}s ease-in-out infinite` }} />
            <span style={{ right: "4%", top: "6%", background: "radial-gradient(circle, #a78bfa, transparent 64%)", opacity: 0.7, animation: `fusion-b ${(14 * pace).toFixed(1)}s ease-in-out infinite` }} />
            <span style={{ left: "8%", bottom: "6%", background: "radial-gradient(circle, #22d3ee, transparent 64%)", opacity: 0.6, animation: `fusion-c ${(16 * pace).toFixed(1)}s ease-in-out infinite` }} />
            <span style={{ right: "8%", bottom: "8%", background: "radial-gradient(circle, #34d399, transparent 64%)", opacity: 0.6, animation: `fusion-d ${(18 * pace).toFixed(1)}s ease-in-out infinite` }} />
          </div>

          {/* FIERY nuclear fire — warm light radiating FROM the core, flickering,
              plus rising embers. Active only; idle stays calm. */}
          {!idle && (
            <>
              <div
                className="absolute left-1/2 top-1/2 h-3/4 w-3/4 -translate-x-1/2 -translate-y-1/2 rounded-full blur-lg mix-blend-screen"
                style={{
                  background: "radial-gradient(circle, rgba(255,237,213,0.9) 0%, #fb923c 26%, #ea580c 48%, transparent 70%)",
                  transform: `translate(-50%,-50%) scale(${0.85 + amp * 0.4})`,
                  animation: `fusion-flicker ${(1.9 * (thinking ? 0.75 : 1)).toFixed(1)}s ease-in-out infinite`,
                }}
              />
              <div
                className="absolute left-1/2 top-1/2 h-1/2 w-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full blur-md mix-blend-screen"
                style={{
                  background: "radial-gradient(circle, #fde68a 0%, #f97316 40%, transparent 72%)",
                  animation: `fusion-flicker ${(1.3 * (thinking ? 0.75 : 1)).toFixed(1)}s ease-in-out infinite`,
                  animationDelay: "0.35s",
                }}
              />
              <div className="absolute inset-0">
                {[14, 28, 42, 50, 58, 72, 86].map((x, i) => (
                  <span
                    key={i}
                    className="absolute h-1.5 w-1.5 rounded-full"
                    style={{
                      left: `${x}%`,
                      top: "52%",
                      background: i % 2 ? "#fef3c7" : "#fdba74",
                      boxShadow: "0 0 8px 2px #fb923c",
                      animation: `ember-rise ${(2.6 + (i % 3) * 0.7).toFixed(1)}s linear infinite`,
                      animationDelay: `${(i * 0.45).toFixed(1)}s`,
                    }}
                  />
                ))}
              </div>
            </>
          )}

          {/* Converging particles — spiral INTO the core (the fusing fuel). */}
          {(reactive || thinking) && (
            <div className="absolute left-1/2 top-1/2 h-0 w-0">
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <div
                  key={i}
                  className="absolute left-0 top-0"
                  style={{
                    width: 200, height: 200, marginLeft: -100, marginTop: -100,
                    animation: `fusion-implode ${thinking ? 1.5 : 2.1}s linear infinite`,
                    animationDelay: `${i * (thinking ? 0.25 : 0.35)}s`,
                    transform: `rotate(${i * 60}deg)`,
                  }}
                >
                  <span className="absolute left-1/2 top-0 h-2 w-2 -translate-x-1/2 rounded-full" style={{ background: "#fff7ed", boxShadow: `0 0 10px 2px ${accent}` }} />
                </div>
              ))}
            </div>
          )}

          {/* Discharging energy rings */}
          {(reactive || thinking) && (
            <>
              <span className="absolute left-1/2 top-1/2 h-32 w-32 rounded-full border-2" style={{ borderColor: `${accent}aa`, animation: `fusion-ring ${thinking ? 1.8 : 2.4}s ease-out infinite` }} />
              <span className="absolute left-1/2 top-1/2 h-32 w-32 rounded-full border-2" style={{ borderColor: `${accent}55`, animation: `fusion-ring ${thinking ? 1.8 : 2.4}s ease-out infinite`, animationDelay: "0.9s" }} />
            </>
          )}

          {/* Hot fusion CORE */}
          <div
            className="absolute left-1/2 top-1/2 h-24 w-24 rounded-full blur-md"
            style={{
              background: `radial-gradient(circle, #ffffff 0%, ${idle ? accent : "#fed7aa"} 32%, transparent 70%)`,
              transform: `translate(-50%, -50%) scale(${0.6 + amp * 1.4})`,
              opacity: 0.75 + amp * 0.25,
              transition: "transform 110ms ease-out",
              animation: thinking ? "fusion-core 1.5s ease-in-out infinite" : undefined,
            }}
          />
        </div>

        {/* Status — a floating glass pill at the bottom, over the plasma. */}
        <div className="absolute bottom-6 left-1/2 flex -translate-x-1/2 items-center gap-2 rounded-full border border-white/15 bg-slate-950/45 px-3.5 py-1.5 backdrop-blur-md">
          <span className="h-2 w-2 rounded-full" style={{ background: accent, boxShadow: `0 0 8px ${accent}` }} />
          <span className="text-sm font-semibold tracking-wide text-white">{LABEL[state]}</span>
          <span className="hidden text-xs text-white/70 sm:inline">· {SUBLABEL[state]}</span>
        </div>
      </div>
    </div>
  );
}
