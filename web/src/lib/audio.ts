// Browser audio: mic capture at 16 kHz (→ server STT) and playback of the
// companion's 24 kHz TTS stream, both exposing an amplitude level so the orb
// can react. Kept framework-free so App.tsx just wires callbacks.

const CAPTURE_RATE = 16_000;

export async function listMicrophones(): Promise<MediaDeviceInfo[]> {
  // Labels are only populated after permission is granted once.
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices.filter((d) => d.kind === "audioinput");
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

export class AudioPlayer {
  private ctx: AudioContext;
  private cursor = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private rate = 24_000;
  private remainder = new Uint8Array(0); // odd trailing byte carried to next chunk

  constructor(
    private onLevel: (level: number) => void,
    private onEnded: () => void = () => {},
  ) {
    this.ctx = new AudioContext();
  }

  configure(sampleRate: number): void {
    this.rate = sampleRate;
    this.cursor = this.ctx.currentTime;
    this.remainder = new Uint8Array(0);
  }

  enqueue(pcm: ArrayBuffer): void {
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

    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    const startAt = Math.max(this.ctx.currentTime, this.cursor);
    src.start(startAt);
    this.cursor = startAt + buffer.duration;
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
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    src.start();
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
