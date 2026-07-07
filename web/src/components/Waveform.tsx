import { useEffect, useRef } from "react";

// Live mic-input waveform: a row of bars whose heights scroll with the user's
// voice amplitude (`level`, 0..1) while they're speaking. Driven on rAF from a
// ref so it animates at 60fps without re-rendering React; decays to a flat line
// when not listening. Theme-aware via the indigo accent.
export function Waveform({
  level,
  active,
  bars = 40,
}: {
  level: number;
  active: boolean;
  bars?: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const levelRef = useRef(level);
  levelRef.current = level;
  const activeRef = useRef(active);
  activeRef.current = active;
  const history = useRef<number[]>(new Array(bars).fill(0));

  useEffect(() => {
    let raf = 0;
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

    const draw = () => {
      const rect = canvas.getBoundingClientRect();
      const w = rect.width;
      const h = rect.height;
      const hist = history.current;
      // Scroll: push the current amplitude (a touch of gain + floor so quiet
      // speech still shows), or decay toward flat when not listening.
      const next = activeRef.current
        ? Math.min(1, 0.06 + levelRef.current * 1.4)
        : hist[hist.length - 1] * 0.82;
      hist.push(next);
      hist.shift();

      ctx.clearRect(0, 0, w, h);
      const dark =
        document.documentElement.dataset.theme === "dark" ||
        (!document.documentElement.dataset.theme &&
          window.matchMedia("(prefers-color-scheme: dark)").matches);
      const color = activeRef.current
        ? dark
          ? "#a5b4fc"
          : "#6366f1"
        : dark
          ? "#3f3f5a"
          : "#cbd5e1";
      ctx.fillStyle = color;

      const gap = 2;
      const bw = Math.max(2, w / bars - gap);
      const mid = h / 2;
      for (let i = 0; i < bars; i++) {
        const v = hist[i];
        const bh = Math.max(3, v * (h - 4));
        const x = i * (bw + gap);
        const y = mid - bh / 2;
        const r = Math.min(bw / 2, 3);
        // rounded vertical bar, centered
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.arcTo(x + bw, y, x + bw, y + bh, r);
        ctx.arcTo(x + bw, y + bh, x, y + bh, r);
        ctx.arcTo(x, y + bh, x, y, r);
        ctx.arcTo(x, y, x + bw, y, r);
        ctx.closePath();
        ctx.fill();
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }, [bars]);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`h-10 w-full max-w-xs transition-opacity duration-300 ${
        active ? "opacity-100" : "opacity-40"
      }`}
    />
  );
}
