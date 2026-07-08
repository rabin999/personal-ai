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

  return (
    <div className="flex select-none flex-col items-center gap-7">
      <div
        className="relative h-72 w-72 sm:h-96 sm:w-96 lg:h-[26rem] lg:w-[26rem]"
        style={{ transform: `scale(${1 + amp * 0.07})`, transition: "transform 120ms ease-out" }}
      >
        {/* Soft outer glow, tinted by the state accent */}
        <div
          className="absolute -inset-8 blur-3xl transition-opacity duration-700"
          style={{
            background: `radial-gradient(circle at center, ${accent} 0%, ${accent}00 62%)`,
            opacity: (idle ? 0.16 : 0.3) + amp * 0.3,
          }}
        />

        {/* FREE-FLOATING plasma — no panel, no border, no hard box. A soft dark
            core base gives the screen-blended colours something to glow against,
            and the whole field is radially MASKED so it dissolves into the page at
            the edges (never looks capped/boxed). */}
        <div
          className="absolute inset-0"
          style={{
            WebkitMaskImage: "radial-gradient(circle at 50% 50%, #000 46%, transparent 72%)",
            maskImage: "radial-gradient(circle at 50% 50%, #000 46%, transparent 72%)",
          }}
        >
          {/* dark base for the screen blend, itself fading out */}
          <div
            className="absolute inset-0"
            style={{
              background:
                "radial-gradient(circle at 50% 50%, rgba(2,6,23,0.92) 0%, rgba(2,6,23,0.55) 42%, transparent 70%)",
            }}
          />
          {/* Drifting, blending colour blobs → the fusion. The whole field also
              cycles HUE continuously (slow at idle, faster when busy) so the colours
              are always shifting — never the same blend twice. */}
          <div
            className="absolute inset-0 [&>span]:absolute [&>span]:h-3/4 [&>span]:w-3/4 [&>span]:rounded-full [&>span]:blur-2xl [&>span]:mix-blend-screen"
            style={{ animation: `orb-hue ${thinking ? 7 : reactive ? 12 : 20}s linear infinite` }}
          >
            <span style={blob("#38bdf8", '{"left":"2%","top":"6%"}', "fusion-a", 0.5, 11)} />
            <span style={blob("#a78bfa", '{"right":"0%","top":"2%"}', "fusion-b", 0.5, 13)} />
            <span style={blob("#22d3ee", '{"left":"6%","bottom":"2%"}', "fusion-c", 0.45, 15)} />
            <span style={blob("#34d399", '{"right":"4%","bottom":"6%"}', "fusion-d", 0.4, 17)} />
            <span style={blob(thinking ? "#f472b6" : accent, '{"left":"22%","top":"26%"}', "fusion-a", 0.35, 19)} />
          </div>

          {/* Slow rotating sheen for extra flow (faster while thinking) */}
          <div
            className="absolute inset-[-25%] mix-blend-screen"
            style={{
              background:
                "conic-gradient(from 0deg, transparent, rgba(255,255,255,0.10), transparent 40%, rgba(255,255,255,0.06), transparent)",
              animation: `fusion-drift ${thinking ? 8 : 22}s linear infinite`,
            }}
          />

          {/* Converging particles — spiral INTO the core (the fusing fuel). */}
          {(reactive || thinking) && (
            <div className="absolute left-1/2 top-1/2 h-0 w-0">
              {[0, 1, 2, 3, 4].map((i) => (
                <div
                  key={i}
                  className="absolute left-0 top-0"
                  style={{
                    width: 120,
                    height: 120,
                    marginLeft: -60,
                    marginTop: -60,
                    transformOrigin: "center",
                    animation: `fusion-implode ${(thinking ? 1.5 : 2.1)}s linear infinite`,
                    animationDelay: `${i * (thinking ? 0.3 : 0.42)}s`,
                    transform: `rotate(${i * 72}deg)`,
                  }}
                >
                  <span
                    className="absolute left-1/2 top-0 h-1.5 w-1.5 -translate-x-1/2 rounded-full"
                    style={{ background: "#e0f2fe", boxShadow: `0 0 8px 2px ${accent}` }}
                  />
                </div>
              ))}
            </div>
          )}

          {/* Discharging energy rings — pulses radiating from the fusion core. */}
          {(reactive || thinking) && (
            <>
              <span className="absolute left-1/2 top-1/2 h-24 w-24 rounded-full border" style={{ borderColor: `${accent}aa`, animation: `fusion-ring ${thinking ? 1.8 : 2.4}s ease-out infinite` }} />
              <span className="absolute left-1/2 top-1/2 h-24 w-24 rounded-full border" style={{ borderColor: `${accent}66`, animation: `fusion-ring ${thinking ? 1.8 : 2.4}s ease-out infinite`, animationDelay: "0.9s" }} />
            </>
          )}

          {/* Hot fusion CORE — a bright plasma point that flares with the voice. */}
          <div
            className="absolute left-1/2 top-1/2 h-20 w-20 rounded-full blur-md"
            style={{
              background: `radial-gradient(circle, #ffffff 0%, ${accent} 34%, ${accent}00 70%)`,
              transform: `translate(-50%, -50%) scale(${0.6 + amp * 1.5})`,
              opacity: 0.7 + amp * 0.3,
              transition: "transform 110ms ease-out",
              animation: thinking ? "fusion-core 1.5s ease-in-out infinite" : undefined,
            }}
          />

          {/* Fine top sheen so the panel reads as glass */}
          <div
            className="absolute inset-0"
            style={{ background: "linear-gradient(160deg, rgba(255,255,255,0.10), transparent 45%)" }}
          />
        </div>
      </div>

      <div className="flex flex-col items-center gap-1">
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: accent, boxShadow: `0 0 8px ${accent}` }} />
          <span className="text-sm font-semibold tracking-wide text-slate-700 dark:text-slate-100">
            {LABEL[state]}
          </span>
        </div>
        <span className="text-xs text-slate-500 dark:text-slate-400">{SUBLABEL[state]}</span>
      </div>
    </div>
  );
}
