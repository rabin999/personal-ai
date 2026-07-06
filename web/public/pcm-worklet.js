// Capture worklet: converts the mic's Float32 samples to PCM16 and posts them
// to the main thread as transferable ArrayBuffers. The AudioContext is created
// at 16 kHz, so no resampling is needed here — the server (§20 STT) expects
// PCM16 mono 16 kHz.
class PCMWorklet extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      const channel = input[0];
      const pcm = new Int16Array(channel.length);
      for (let i = 0; i < channel.length; i++) {
        const s = Math.max(-1, Math.min(1, channel[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-worklet", PCMWorklet);
