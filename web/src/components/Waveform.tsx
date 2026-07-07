import { useEffect, useRef } from "react";

// Siri-style voice visual: several translucent sine ribbons of different hues,
// phase-scrolling and swelling with the user's real mic amplitude (`level`,
// 0..1) while listening; settling to a thin idle line otherwise. Canvas + rAF
// from refs (60fps, no React re-render); additive blending gives the glow.
type Wave = { color: string; freq: number; speed: number; amp: number; width: number };

const WAVES_DARK: Wave[] = [
  { color: "#60a5fa", freq: 1.0, speed: 0.9, amp: 1.0, width: 2.4 }, // blue
  { color: "#a78bfa", freq: 1.7, speed: -1.3, amp: 0.8, width: 2.0 }, // violet
  { color: "#f472b6", freq: 2.5, speed: 1.7, amp: 0.6, width: 1.6 }, // pink
  { color: "#34d399", freq: 3.3, speed: -2.1, amp: 0.45, width: 1.4 }, // teal
];
const WAVES_LIGHT: Wave[] = [
  { color: "#2563eb", freq: 1.0, speed: 0.9, amp: 1.0, width: 2.4 },
  { color: "#7c3aed", freq: 1.7, speed: -1.3, amp: 0.8, width: 2.0 },
  { color: "#db2777", freq: 2.5, speed: 1.7, amp: 0.6, width: 1.6 },
  { color: "#059669", freq: 3.3, speed: -2.1, amp: 0.45, width: 1.4 },
];

export function Waveform({ level, active }: { level: number; active: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const levelRef = useRef(level);
  levelRef.current = level;
  const activeRef = useRef(active);
  activeRef.current = active;
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

      // Ease the amplitude toward the target: voice level when listening (with a
      // small floor so it always breathes), near-zero otherwise.
      const target = activeRef.current ? 0.12 + Math.min(1, levelRef.current * 1.5) : 0.02;
      ampRef.current += (target - ampRef.current) * Math.min(1, dt * 8);
      const amp = ampRef.current;

      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = "lighter";
      ctx.lineJoin = "round";
      ctx.lineCap = "round";

      const waves = isDark() ? WAVES_DARK : WAVES_LIGHT;
      const step = 2;
      for (const wv of waves) {
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
        ctx.strokeStyle = wv.color;
        ctx.globalAlpha = 0.55;
        ctx.lineWidth = wv.width;
        ctx.stroke();
      }
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
      className={`h-16 w-full max-w-sm transition-opacity duration-300 ${
        active ? "opacity-100" : "opacity-45"
      }`}
    />
  );
}
