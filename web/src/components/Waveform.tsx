import { useEffect, useRef } from "react";
import type { TurnState } from "../lib/types";

// Siri-style voice visual: several translucent sine ribbons phase-scrolling and
// swelling with real amplitude (`level`, 0..1). It reacts to BOTH sides of the
// conversation — the user's mic while listening AND the companion's TTS while
// speaking (the caller feeds `level` from both sources) — so one continuous bar
// carries the whole turn. `state` drives the hue (green=you, cyan=companion,
// amber=thinking). Canvas + rAF from refs (60fps, no React re-render); additive
// blending gives the glow. This replaces the old orb as the single hero visual.

// Ribbon shape (frequency/scroll speed/amplitude/stroke width) is shared; only
// the colours change per turn state, so the motion reads as one instrument.
type Ribbon = { freq: number; speed: number; amp: number; width: number };
const RIBBONS: Ribbon[] = [
  { freq: 1.0, speed: 0.9, amp: 1.0, width: 2.4 },
  { freq: 1.7, speed: -1.3, amp: 0.8, width: 2.0 },
  { freq: 2.5, speed: 1.7, amp: 0.6, width: 1.6 },
  { freq: 3.3, speed: -2.1, amp: 0.45, width: 1.4 },
];

// Four-stop palettes per state, mirroring the StatusChip dot colours. [dark, light].
const PALETTES: Record<TurnState, [string[], string[]]> = {
  idle: [
    ["#38bdf8", "#22d3ee", "#2dd4bf", "#7dd3fc"],
    ["#0284c7", "#0891b2", "#0d9488", "#0ea5e9"],
  ],
  listening: [
    ["#34d399", "#2dd4bf", "#22d3ee", "#6ee7b7"],
    ["#059669", "#0d9488", "#0891b2", "#10b981"],
  ],
  thinking: [
    ["#fbbf24", "#f59e0b", "#fb923c", "#fcd34d"],
    ["#d97706", "#ea580c", "#f59e0b", "#b45309"],
  ],
  speaking: [
    ["#22d3ee", "#38bdf8", "#818cf8", "#67e8f9"],
    ["#0891b2", "#0284c7", "#6366f1", "#06b6d4"],
  ],
};

export function Waveform({ level, state }: { level: number; state: TurnState }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const levelRef = useRef(level);
  levelRef.current = level;
  const stateRef = useRef(state);
  stateRef.current = state;
  const ampRef = useRef(0); // smoothed amplitude the ribbons actually follow

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    window.addEventListener("resize", resize);

    const isDark = () =>
      document.documentElement.dataset.theme === "dark" ||
      (!document.documentElement.dataset.theme &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);

    let raf = 0;
    let phase = 0;
    let last = performance.now();

    const draw = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      phase += dt;

      const rect = canvas.getBoundingClientRect();
      const w = rect.width;
      const h = rect.height;
      const mid = h / 2;

      // Ease the amplitude toward the target: full voice level whenever the turn
      // is live (you speaking OR the companion speaking) with a small breathing
      // floor, settling near-flat only at idle.
      const active = stateRef.current !== "idle";
      const target = active ? 0.12 + Math.min(1, levelRef.current * 1.5) : 0.02;
      ampRef.current += (target - ampRef.current) * Math.min(1, dt * 8);
      const amp = ampRef.current;

      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = "lighter";
      ctx.lineJoin = "round";
      ctx.lineCap = "round";

      const colors = PALETTES[stateRef.current][isDark() ? 0 : 1];
      const step = 2;
      RIBBONS.forEach((wv, i) => {
        ctx.beginPath();
        for (let x = 0; x <= w; x += step) {
          const t = x / w; // 0..1
          // Edge taper so the ribbon emanates from the centre and fades at ends.
          const att = Math.max(0, 1 - (2 * t - 1) ** 2);
          const y =
            mid +
            Math.sin(t * Math.PI * 2 * wv.freq + phase * wv.speed * Math.PI) *
              att *
              amp *
              (h * 0.42) *
              wv.amp;
          if (x === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = colors[i];
        ctx.globalAlpha = 0.55;
        ctx.lineWidth = wv.width;
        ctx.stroke();
      });
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`h-48 w-full max-w-2xl transition-opacity duration-300 sm:h-56 ${
        state !== "idle" ? "opacity-100" : "opacity-50"
      }`}
    />
  );
}
