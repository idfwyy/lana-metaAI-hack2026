// AudioWorklet for mic.ts: mono, resampled to 24 kHz, 16-bit PCM, posted in 100 ms frames.
// Plain JS on purpose: it runs in the AudioWorkletGlobalScope and is loaded as a URL, unbundled.

const TARGET_RATE = 24000;
const FRAME_SAMPLES = TARGET_RATE / 10; // 100 ms

class Pcm24k extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE; // input samples per output sample
    this.pos = 0; // read position into the current input block, carried across blocks
    this.prev = 0; // last input sample of the previous block, for interpolation across the edge
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.filled = 0;
    this.paused = false;
    this.port.onmessage = (e) => {
      this.paused = e.data === "pause";
      this.filled = 0; // never send half a frame from before a pause
    };
  }

  process(inputs) {
    const channels = inputs[0];
    if (!channels || channels.length === 0 || this.paused) return true;
    const n = channels[0].length;
    const mono = new Float32Array(n);
    for (const ch of channels) for (let i = 0; i < n; i++) mono[i] += ch[i] / channels.length;

    // Linear interpolation; index -1 is the previous block's last sample.
    while (this.pos < n) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      const a = i === 0 ? this.prev : mono[i - 1];
      const b = mono[i];
      const s = Math.max(-1, Math.min(1, a + (b - a) * frac));
      this.frame[this.filled++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.filled === FRAME_SAMPLES) {
        const out = this.frame.buffer;
        this.port.postMessage(out, [out]);
        this.frame = new Int16Array(FRAME_SAMPLES);
        this.filled = 0;
      }
      this.pos += this.step;
    }
    this.pos -= n;
    this.prev = mono[n - 1];
    return true;
  }
}

registerProcessor("pcm-24k", Pcm24k);
