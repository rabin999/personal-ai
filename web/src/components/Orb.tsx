import { useEffect, useRef } from "react";
import type { TurnState } from "../lib/types";

// A contained artificial sun — a canvas-rendered nuclear-fusion core. A white-hot
// breathing centre wrapped in swirling liquid plasma, looping magnetic field arcs
// that flicker and snap, solar flares that burst outward and get pulled back, and
// drifting embers — all drawn additively (`lighter`) for real bloom. It reacts to
// the voice `level` and the turn `state`: calm and slow at idle, hotter and more
// turbulent while listening/speaking/thinking. No CSS box — the plasma glows over
// the page and dissolves at the edges (soft radial mask).

const TAU = Math.PI * 2;
type RGB = readonly [number, number, number];

const WHITE: RGB = [255, 248, 238];
const YELLOW: RGB = [252, 211, 77];
const AMBER: RGB = [251, 146, 60];
const ORANGE: RGB = [234, 88, 12];
const RED: RGB = [153, 27, 27];
const BLUE: RGB = [56, 189, 248];
const VIOLET: RGB = [167, 139, 250];

// Cheap layered-sine pseudo-noise in [-1, 1] for organic flicker/turbulence.
function nz(x: number): number {
  return Math.sin(x) * 0.5 + Math.sin(x * 2.17 + 1.3) * 0.3 + Math.sin(x * 4.73 + 2.1) * 0.2;
}

interface Ember {
  a: number; // angle
  r: number; // distance from core
  vr: number; // outward speed
  sz: number;
  life: number;
  max: number;
  col: RGB;
}

export function Orb({ state, level }: { state: TurnState; level: number }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // Live props read by the rAF loop without restarting it.
  const stateRef = useRef(state);
  const levelRef = useRef(level);
  stateRef.current = state;
  levelRef.current = level;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const parent = canvas.parentElement!;
    const ctx = canvas.getContext("2d", { alpha: true })!;
    const DPR = Math.min(1.75, window.devicePixelRatio || 1);
    let W = 0;
    let H = 0;

    const resize = () => {
      const r = parent.getBoundingClientRect();
      W = Math.max(1, Math.floor(r.width));
      H = Math.max(1, Math.floor(r.height));
      canvas.width = Math.floor(W * DPR);
      canvas.height = Math.floor(H * DPR);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(parent);

    // Radial glow — the primitive everything is built from.
    const glow = (x: number, y: number, r: number, [R, G, B]: RGB, a: number) => {
      if (a <= 0.003 || r <= 0) return;
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, `rgba(${R},${G},${B},${a})`);
      g.addColorStop(0.5, `rgba(${R},${G},${B},${a * 0.32})`);
      g.addColorStop(1, `rgba(${R},${G},${B},0)`);
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, TAU);
      ctx.fill();
    };

    // Swirling plasma cells that orbit the core and merge additively.
    const CELLS = 8;
    const cell = Array.from({ length: CELLS }, (_, i) => ({
      orbit: 0.35 + (i / CELLS) * 1.05,
      spd: 0.25 + (i % 4) * 0.12 + (i % 2 ? 0.05 : 0),
      phase: (i / CELLS) * TAU,
      wob: 0.6 + (i % 3) * 0.25,
      col: [AMBER, ORANGE, YELLOW, RED, AMBER, ORANGE, BLUE, YELLOW][i] as RGB,
    }));

    // Magnetic field arcs looping around the core.
    const arcs = [
      { rx: 1.7, ry: 0.72, tilt: 0.3, spin: 0.16, col: AMBER },
      { rx: 2.15, ry: 0.62, tilt: 1.5, spin: -0.11, col: BLUE },
      { rx: 2.55, ry: 0.9, tilt: -0.7, spin: 0.08, col: VIOLET },
    ];

    // Solar flares that rise from the rim and curl back in.
    const flares = Array.from({ length: 5 }, (_, i) => ({
      ang: (i / 5) * TAU,
      spd: 0.9 + (i % 3) * 0.4,
      phase: i * 1.7,
    }));

    const embers: Ember[] = [];
    const spawnEmber = (baseR: number): Ember => ({
      a: Math.random() * TAU,
      r: baseR * (0.5 + Math.random() * 0.5),
      vr: baseR * (0.25 + Math.random() * 0.7),
      sz: baseR * (0.05 + Math.random() * 0.12),
      life: 0,
      max: 1.4 + Math.random() * 1.8,
      col: Math.random() < 0.15 ? BLUE : Math.random() < 0.5 ? YELLOW : AMBER,
    });

    let raf = 0;
    let last = performance.now();
    const start = last;

    const frame = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const t = (now - start) / 1000;

      const st = stateRef.current;
      const idle = st === "idle";
      const thinking = st === "thinking";
      const reactive = st === "listening" || st === "speaking";
      const amp = reactive ? Math.min(1, levelRef.current) : 0;
      // Overall energy + animation speed.
      const energy = idle ? 0.32 : thinking ? 0.85 : 0.55 + amp * 0.95;
      const speed = idle ? 0.5 : thinking ? 1.35 : 0.85 + amp * 0.5;
      const ts = t * speed;

      const cx = W / 2;
      const cy = H / 2;
      const R0 = Math.min(W, H) * 0.15; // base core radius
      // Rhythmic breathing: the core expands, brightens, then compresses.
      const breath = Math.sin(ts * 1.15) * 0.5 + 0.5; // 0..1
      const pulse = breath * breath; // sharper peaks
      const R = R0 * (0.9 + 0.16 * breath + 0.12 * amp) * (0.6 + 0.4 * energy);
      // Fold energy into brightness so idle is a calm, dimmer sun (not a flare).
      // The trailing DIM keeps peak output gentle — a white-hot sun is harsh at
      // night, so overall luminance is capped while the palette/motion stay intact.
      const DIM = 0.66;
      const bright = (0.6 + 0.4 * breath + 0.25 * amp) * (0.72 + 0.28 * energy) * DIM;

      // Fade the previous frame toward transparent → motion trails on a see-through
      // background (no dark rectangle / box).
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      ctx.globalCompositeOperation = "destination-out";
      ctx.fillStyle = `rgba(0,0,0,${0.16 + 0.06 * energy})`;
      ctx.fillRect(0, 0, W, H);

      // Camera: slow orbit + gentle push-in, plus a shake on energy pulses.
      const zoom = 1 + 0.03 * Math.sin(ts * 0.3) + 0.04 * amp;
      const rot = t * 0.05;
      const shake = pulse * (2 + 3 * energy);
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      ctx.translate(cx + nz(t * 7) * shake, cy + nz(t * 6 + 4) * shake);
      ctx.rotate(rot);
      ctx.scale(zoom, zoom);
      ctx.translate(-cx, -cy);

      // Outer plasma halo (deep red → orange), gives volumetric depth.
      glow(cx, cy, R * 4.2, RED, 0.28 * bright);
      glow(cx, cy, R * 3.0, ORANGE, 0.3 * bright);

      // Swirling plasma cells.
      for (const c of cell) {
        const ang = ts * c.spd + c.phase;
        const wob = 1 + 0.35 * nz(ts * c.wob + c.phase);
        const rr = R * c.orbit * wob * (1.1 + 0.3 * energy);
        const x = cx + Math.cos(ang) * rr;
        const y = cy + Math.sin(ang) * rr * 0.92;
        const a = (0.22 + 0.18 * nz(ts * 1.7 + c.phase)) * bright * (0.7 + energy * 0.5);
        glow(x, y, R * 1.5, c.col, Math.max(0, a));
      }

      // Solar flares: tongues that extend from the rim and are pulled back.
      for (const f of flares) {
        const ang = f.ang + rot * 2;
        const burst = Math.max(0, Math.sin(ts * f.spd + f.phase));
        const len = R * (0.4 + 2.2 * burst * burst) * energy;
        const steps = 7;
        for (let s = 1; s <= steps; s++) {
          const k = s / steps;
          const rr = R * 0.85 + len * k;
          const x = cx + Math.cos(ang) * rr;
          const y = cy + Math.sin(ang) * rr;
          const col = k < 0.4 ? WHITE : k < 0.75 ? YELLOW : AMBER;
          glow(x, y, R * (0.4 * (1 - k * 0.6)), col, (1 - k) * 0.5 * burst * bright);
        }
      }

      // Magnetic field arcs — flicker and snap.
      for (const arc of arcs) {
        const flick = 0.25 + 0.75 * Math.max(0, nz(ts * 3 + arc.tilt));
        const snap = Math.max(0, nz(ts * 0.6 + arc.tilt * 2)) ** 3; // occasional bright snap
        ctx.save();
        ctx.translate(cx, cy);
        ctx.rotate(arc.tilt + t * arc.spin);
        ctx.lineWidth = (1.2 + 2.6 * flick + 3 * snap) * (0.7 + energy * 0.6);
        ctx.shadowBlur = 18 + 26 * flick;
        ctx.shadowColor = `rgba(${arc.col[0]},${arc.col[1]},${arc.col[2]},1)`;
        ctx.strokeStyle = `rgba(${arc.col[0]},${arc.col[1]},${arc.col[2]},${(0.18 + 0.5 * flick + 0.4 * snap) * bright})`;
        ctx.beginPath();
        ctx.ellipse(0, 0, R * arc.rx, R * arc.ry, 0, 0, TAU);
        ctx.stroke();
        ctx.restore();
      }
      ctx.shadowBlur = 0;

      // Core layers — yellow surface → white-hot centre → tiny blue-hot pinpoint.
      glow(cx, cy, R * 2.0, AMBER, 0.5 * bright);
      glow(cx, cy, R * 1.35, YELLOW, 0.55 * bright);
      // White core kept deliberately soft (capped) — it's the harshest part on a
      // dark screen. A warm-white, not a pure blast.
      glow(cx, cy, R * 0.8, WHITE, Math.min(0.6, 0.2 + 0.28 * bright + 0.16 * energy));
      glow(cx, cy, R * 0.4, BLUE, 0.14 * pulse); // electric-blue heat at peaks
      glow(cx, cy, R * 0.22, WHITE, 0.5 + 0.18 * energy);

      // Embers / plasma fragments drifting outward with trails.
      const want = idle ? 26 : thinking ? 70 : 40 + Math.floor(amp * 55);
      while (embers.length < want) embers.push(spawnEmber(R));
      if (embers.length > want) embers.length = want;
      const maxR = Math.min(W, H) * 0.55;
      for (const e of embers) {
        e.life += dt * speed;
        e.r += e.vr * dt * speed;
        e.a += dt * 0.3 * nz(e.life + e.a);
        const fade = 1 - e.life / e.max;
        if (fade <= 0 || e.r > maxR) {
          Object.assign(e, spawnEmber(R));
          continue;
        }
        const x = cx + Math.cos(e.a) * e.r;
        const y = cy + Math.sin(e.a) * e.r;
        glow(x, y, e.sz * (1 + e.life), e.col, fade * 0.85 * bright);
      }

      ctx.restore();
      raf = requestAnimationFrame(frame);
    };

    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  return (
    <div className="pointer-events-none relative aspect-square w-full max-w-[34rem] select-none">
      <canvas
        ref={canvasRef}
        className="h-full w-full"
        style={{
          WebkitMaskImage: "radial-gradient(circle at 50% 50%, #000 55%, transparent 100%)",
          maskImage: "radial-gradient(circle at 50% 50%, #000 55%, transparent 100%)",
        }}
      />
    </div>
  );
}
