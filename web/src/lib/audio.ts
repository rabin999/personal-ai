// Browser audio: mic capture at 16 kHz (→ server STT) and playback of the
// companion's 24 kHz TTS stream, both exposing an amplitude level so the orb
// can react. Kept framework-free so App.tsx just wires callbacks.

const CAPTURE_RATE = 16_000;

export async function listMicrophones(): Promise<MediaDeviceInfo[]> {
  // Labels are only populated after permission is granted once.
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices.filter((d) => d.kind === "audioinput");
}

/**
 * Proactively obtain microphone permission so device labels populate and the
 * pipeline can capture on first Start. We open then immediately release a
 * stream purely to trigger the browser's permission prompt (or resolve an
 * already-granted permission). Returns whether access was granted.
 */
export async function requestMicAccess(): Promise<boolean> {
  try {
    // If the Permissions API reports a decision already, honour it without
    // re-prompting on every app entry.
    if (navigator.permissions?.query) {
      try {
        const status = await navigator.permissions.query({
          name: "microphone" as PermissionName,
        });
        if (status.state === "denied") return false;
      } catch {
        /* Permissions API or the "microphone" name is unsupported — fall through. */
      }
    }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
    return true;
  } catch {
    return false; // user dismissed/denied — MicPicker still explains how to grant.
  }
}

export class MicCapture {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;

  async start(
    deviceId: string | undefined,
    onFrame: (pcm: ArrayBuffer) => void,
    onLevel: (level: number) => void,
  ): Promise<void> {
    // Declare a media (play-and-record) audio session on the Start gesture,
    // before capture flips the OS into record-only mode → earpiece (mobile fix).
    requestMediaAudioSession();
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    this.ctx = new AudioContext({ sampleRate: CAPTURE_RATE });
    await this.ctx.audioWorklet.addModule("/pcm-worklet.js");
    const source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "pcm-worklet");
    this.node.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
      onFrame(e.data);
      onLevel(rms(new Int16Array(e.data)));
    };
    source.connect(this.node);
  }

  async stop(): Promise<void> {
    this.node?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    await this.ctx?.close();
    this.ctx = null;
    this.stream = null;
    this.node = null;
  }
}

// ── Mobile speaker routing ───────────────────────────────────────────────────
// On phones/tablets a live mic+speaker session is treated as a *call*: the OS
// routes output to the earpiece on the CALL-volume stream (quiet, wrong speaker).
// We force MEDIA/loud-speaker output two ways, both feature-detected so desktop
// and unsupported browsers fall back cleanly:
//   1. iOS 17+ Safari: declare a media audio session (`navigator.audioSession`).
//   2. Everywhere: pipe Web Audio through a hidden <audio> *media element*
//      (media elements play on the MEDIA stream) pinned to the loud-speaker sink
//      via setSinkId where supported (Android Chrome). This is the routing fix
//      that keeps output off the earpiece/call stream.

interface AudioSessionLike {
  type: string;
}

/** Phones/tablets are where the earpiece/call-routing bug lives; desktop keeps
 *  its known-good raw-destination path unchanged. iPadOS reports as desktop
 *  Safari, so also treat a touch-capable Mac-like UA as mobile. */
function isMobile(): boolean {
  const ua = navigator.userAgent;
  if (/Android|iPhone|iPad|iPod/i.test(ua)) return true;
  return /Macintosh/.test(ua) && navigator.maxTouchPoints > 1; // iPadOS
}

/** iOS 17+ Safari: ask for simultaneous record + loud-speaker media playback. */
export function requestMediaAudioSession(): void {
  const nav = navigator as unknown as { audioSession?: AudioSessionLike };
  try {
    if (nav.audioSession && "type" in nav.audioSession) {
      // We capture the mic AND play TTS at once → play-and-record. Declaring it
      // (vs. the default "auto", which becomes record-only → earpiece) lets
      // WebKit keep media playback on the loud speaker.
      nav.audioSession.type = "play-and-record";
    }
  } catch {
    /* older iOS / unsupported — the media-element route below still applies. */
  }
}

/** The explicit loud-speaker output, if the platform exposes one by label. */
async function loudSpeakerSinkId(): Promise<string | undefined> {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    // Mid-capture the *default* output is the earpiece on mobile, so prefer a
    // device that names itself a speaker/speakerphone when one is exposed.
    const speaker = devices.find(
      (d) => d.kind === "audiooutput" && /speaker/i.test(d.label),
    );
    return speaker?.deviceId;
  } catch {
    return undefined;
  }
}

type SinkCapableAudio = HTMLAudioElement & {
  setSinkId?: (id: string) => Promise<void>;
};

/**
 * Routes an AudioContext's output through a hidden <audio> media element so
 * playback lands on the MEDIA/loud-speaker stream instead of the call/earpiece
 * stream. Falls back to the raw context destination when media-element routing
 * isn't available (older browsers / no MediaStreamDestination support).
 */
class SpeakerRoute {
  /** Connect your source nodes here. */
  readonly target: AudioNode;
  private el: SinkCapableAudio | null = null;

  constructor(ctx: AudioContext) {
    // Only reroute on mobile (the bug's home); desktop keeps the raw destination.
    if (!isMobile() || typeof ctx.createMediaStreamDestination !== "function") {
      this.target = ctx.destination; // desktop / unsupported → default path
      return;
    }
    const dest = ctx.createMediaStreamDestination();
    const el = document.createElement("audio") as SinkCapableAudio;
    el.autoplay = true;
    el.setAttribute("playsinline", ""); // keep inline (don't go fullscreen) on iOS
    el.setAttribute("aria-hidden", "true");
    el.style.display = "none";
    el.srcObject = dest.stream;
    document.body.appendChild(el);
    void el.play().catch(() => {
      /* autoplay may need the Start gesture; enqueue() retries via ctx.resume */
    });
    this.el = el;
    this.target = dest;
    void this.forceLoudSpeaker();
  }

  private async forceLoudSpeaker(): Promise<void> {
    if (!this.el?.setSinkId) return; // unsupported (notably iOS Safari)
    const id = await loudSpeakerSinkId();
    if (!id) return;
    try {
      await this.el.setSinkId(id);
    } catch {
      /* device gone / not permitted — leave on default output */
    }
  }

  dispose(): void {
    if (!this.el) return;
    this.el.pause();
    this.el.srcObject = null;
    this.el.remove();
    this.el = null;
  }
}

export class AudioPlayer {
  private ctx: AudioContext;
  private route: SpeakerRoute;
  private cursor = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private rate = 24_000;
  private remainder = new Uint8Array(0); // odd trailing byte carried to next chunk
  // Barge-in gate (§24 / C2): once the user interrupts, DROP every audio chunk
  // still arriving for the interrupted reply — server buffering + the WS/OS send
  // buffer can deliver already-synthesized audio AFTER the barge-in signal, and
  // re-enqueuing it is exactly why "the voice keeps playing". Stays muted until
  // the NEXT reply actually starts synthesizing (unmute on its first TTS event).
  private muted = false;
  // C7: playback rate multiplier (1.0 = normal). Default 1.2 — the voice was a
  // touch slow; per-user configurable from the profile. Applied to every scheduled
  // buffer so BOTH voice engines (they share this sink) speak at the user's pace.
  private speed = 1.2;
  // Playback lead (§2b): schedule the first buffer of a reply — and rebuild the
  // cushion after any underrun — this far in the future. TTS arrives as many
  // small, jittery network chunks; scheduling them with zero lead means a slow
  // chunk lets playback catch up to the write cursor, and the next buffer starts
  // *after* a gap → an audible click/garble between clauses. An ~120ms cushion
  // absorbs the jitter so playback stays gapless.
  private static readonly LEAD_S = 0.12;

  constructor(
    private onLevel: (level: number) => void,
    private onEnded: () => void = () => {},
  ) {
    // Hint a media audio session before the context exists so the very first
    // buffer already plays on the loud speaker (mobile earpiece fix).
    requestMediaAudioSession();
    this.ctx = new AudioContext();
    this.route = new SpeakerRoute(this.ctx);
  }

  configure(sampleRate: number): void {
    this.rate = sampleRate;
    this.cursor = this.ctx.currentTime;
    this.remainder = new Uint8Array(0);
  }

  /** C7: set the playback rate (0.8–1.5×). Clamped so a bad value can't distort. */
  setSpeed(speed: number): void {
    if (Number.isFinite(speed)) this.speed = Math.min(1.5, Math.max(0.8, speed));
  }

  enqueue(pcm: ArrayBuffer): void {
    // C2: after a barge-in, silently discard trailing audio from the interrupted
    // reply until the next reply un-mutes us — otherwise it re-schedules playback.
    if (this.muted) return;
    // Streamed PCM chunks split at arbitrary byte boundaries; keep samples
    // 16-bit aligned by carrying any odd trailing byte into the next chunk.
    const bytes = new Uint8Array(pcm);
    const joined = new Uint8Array(this.remainder.length + bytes.length);
    joined.set(this.remainder);
    joined.set(bytes, this.remainder.length);
    const usable = joined.length - (joined.length % 2);
    this.remainder = joined.slice(usable);
    if (usable === 0) return;

    const int16 = new Int16Array(joined.buffer, 0, usable / 2);
    const buffer = this.ctx.createBuffer(1, int16.length, this.rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < int16.length; i++) channel[i] = int16[i] / 0x8000;

    // Resume a context the browser auto-suspended (mobile), else the queued
    // buffer is silent and the hidden media element never gets audio to route.
    if (this.ctx.state === "suspended") void this.ctx.resume();
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.playbackRate.value = this.speed; // C7: user's speech rate
    src.connect(this.route.target); // → MEDIA/loud-speaker stream on mobile
    // If the write cursor has fallen to/behind playback (session start, or an
    // underrun after a slow network chunk), restart it a lead ahead of "now" so
    // this and following buffers schedule gaplessly; otherwise keep it seamless.
    const now = this.ctx.currentTime;
    const startAt =
      this.cursor <= now ? now + AudioPlayer.LEAD_S : this.cursor;
    src.start(startAt);
    // Faster playback shortens the buffer's wall-clock duration by `speed`, so the
    // schedule cursor must advance by the SPED-UP duration to stay gapless.
    this.cursor = startAt + buffer.duration / this.speed;
    this.sources.add(src);
    this.onLevel(rms(int16));
    src.onended = () => {
      this.sources.delete(src);
      if (this.sources.size === 0) {
        this.onLevel(0);
        this.onEnded(); // playback drained — safe to un-mute the mic (half-duplex)
      }
    };
  }

  /** Barge-in (§24): stop everything currently playing immediately. */
  stop(): void {
    this.sources.forEach((s) => {
      try {
        s.stop();
      } catch {
        /* already stopped */
      }
    });
    this.sources.clear();
    this.cursor = this.ctx.currentTime;
    this.onLevel(0);
  }

  /** Barge-in (§24 / C2): stop what's playing AND drop any further audio for the
   *  interrupted reply until the next reply starts. Called on the server's
   *  barge_in signal — the audio-stop half of a real interruption. */
  interrupt(): void {
    this.muted = true;
    this.stop();
  }

  /** The next reply is starting to synthesize — accept its audio again. */
  resume(): void {
    this.muted = false;
  }

  /** Replay a whole reply from its collected PCM16 chunks. */
  async replay(chunks: ArrayBuffer[], sampleRate: number): Promise<void> {
    const total = chunks.reduce((n, c) => n + c.byteLength, 0);
    if (total < 2) return;
    // Concatenate raw bytes first, then align to 16-bit (chunk boundaries are
    // arbitrary, so a chunk can end mid-sample).
    const bytes = new Uint8Array(total);
    let at = 0;
    for (const c of chunks) {
      bytes.set(new Uint8Array(c), at);
      at += c.byteLength;
    }
    const usable = total - (total % 2);
    const merged = new Int16Array(bytes.buffer, 0, usable / 2);
    const buffer = this.ctx.createBuffer(1, merged.length, sampleRate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < merged.length; i++) channel[i] = merged[i] / 0x8000;
    if (this.ctx.state === "suspended") void this.ctx.resume();
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.playbackRate.value = this.speed; // C7: replay at the user's rate too
    src.connect(this.route.target); // → MEDIA/loud-speaker stream on mobile
    src.start();
  }

  /** Release the media-routing element + audio context (call on disconnect). */
  async close(): Promise<void> {
    this.stop();
    this.route.dispose();
    try {
      await this.ctx.close();
    } catch {
      /* already closed */
    }
  }
}

function rms(samples: Int16Array): number {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) {
    const v = samples[i] / 0x8000;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / samples.length) * 2.5);
}
