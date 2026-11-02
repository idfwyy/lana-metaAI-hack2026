// Playback for Lana's and the clinics' lines. One AudioContext, unlocked on the first click.
// Each line's mp3 chunks are joined and decoded whole: decoding partial mp3 frames is
// unreliable in Safari, and lines arrive faster than they play, so only the first one waits.

let ctx: AudioContext | null = null;
let master: GainNode | null = null; // everything plays through it, so Sound off is one mute
let soundOff = false;
const playing = new Set<AudioBufferSourceNode>();

type AudioContextCtor = typeof AudioContext;

function context(): AudioContext | null {
  if (ctx) return ctx;
  const Ctor: AudioContextCtor | undefined =
    window.AudioContext ?? (window as unknown as { webkitAudioContext?: AudioContextCtor }).webkitAudioContext;
  if (!Ctor) return null;
  ctx = new Ctor();
  master = ctx.createGain();
  master.gain.value = soundOff ? 0 : 1;
  master.connect(ctx.destination);
  return ctx;
}

/**
 * The Sound toggle. Lines keep playing silently while it's off, so turning it back on is
 * heard at once, mid-line. A 15 ms ramp avoids a click.
 */
export function setSoundOff(off: boolean): void {
  soundOff = off;
  if (ctx && master) master.gain.setTargetAtTime(off ? 0 : 1, ctx.currentTime, 0.015);
}

/** Call inside a click handler. Browsers block sound until a gesture resumes the context. */
export function unlockAudio(): void {
  const c = context();
  if (!c) return;
  if (c.state === "suspended") void c.resume().catch(() => {});
  // A one-sample silent buffer started inside the gesture fully unlocks Safari.
  const silent = c.createBuffer(1, 1, 22050);
  const src = c.createBufferSource();
  src.buffer = silent;
  src.connect(c.destination);
  src.start(0);
}

export interface PlayingLine {
  duration: number;
  /** Size of the mp3 that was decoded, for the dev log. */
  bytes: number;
  /** Seconds played so far, clamped to the duration. */
  elapsed: () => number;
  ended: Promise<void>;
  stop: () => void;
}

function decodeBase64(chunks: string[]): ArrayBuffer {
  const parts = chunks.map((b64) => {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  });
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out.buffer;
}

function decode(c: AudioContext, data: ArrayBuffer): Promise<AudioBuffer> {
  // The callback form works on older Safari, where decodeAudioData doesn't return a promise.
  return new Promise((resolve, reject) => {
    const maybe = c.decodeAudioData(data, resolve, reject) as Promise<AudioBuffer> | undefined;
    maybe?.catch(reject);
  });
}

/**
 * Decodes and plays one line, `from` a fraction of the way in (late audio picking up a line
 * already on screen). Rejects if audio can't play, so the caller falls back to text.
 */
export async function playLine(chunks: string[], from = 0): Promise<PlayingLine> {
  const c = context();
  if (!c) throw new Error("Web Audio is not available");
  if (c.state !== "running") await c.resume().catch(() => {});
  if (c.state !== "running") throw new Error("Audio is locked");
  if (chunks.length === 0) throw new Error("No audio");

  const data = decodeBase64(chunks);
  const bytes = data.byteLength; // decoding detaches the buffer
  const buffer = await decode(c, data);
  const src = c.createBufferSource();
  src.buffer = buffer;
  src.connect(master ?? c.destination);
  const offset = Math.min(Math.max(from, 0), 1) * buffer.duration;

  let done = false;
  const ended = new Promise<void>((resolve) => {
    src.onended = () => {
      done = true;
      playing.delete(src);
      resolve();
    };
  });
  const startedAt = c.currentTime - offset; // elapsed() counts from the line's start
  src.start(0, offset);
  playing.add(src);

  return {
    duration: buffer.duration,
    bytes,
    elapsed: () => (done ? buffer.duration : Math.min(buffer.duration, c.currentTime - startedAt)),
    ended,
    stop: () => {
      if (done) return;
      try {
        src.stop();
      } catch {
        // already stopped
      }
    },
  };
}

/** Silences everything (Start over, Stop, reconnect). */
export function stopAllAudio(): void {
  for (const src of playing) {
    try {
      src.stop();
    } catch {
      // already stopped
    }
  }
  playing.clear();
}

const RING_EVERY_MS = 1600;

/** A soft two-note chime for Lana's incoming call, repeated until the returned stop() runs. */
export function startRing(): () => void {
  const c = context();
  if (!c) return () => {};
  if (c.state === "suspended") void c.resume().catch(() => {});
  const gain = c.createGain();
  gain.gain.value = 0.12;
  gain.connect(master ?? c.destination);

  const chime = () => {
    if (c.state !== "running") return;
    [660, 880].forEach((freq, i) => {
      const at = c.currentTime + i * 0.18;
      const osc = c.createOscillator();
      const env = c.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      env.gain.setValueAtTime(0, at);
      env.gain.linearRampToValueAtTime(1, at + 0.02);
      env.gain.exponentialRampToValueAtTime(0.001, at + 0.8);
      osc.connect(env).connect(gain);
      osc.start(at);
      osc.stop(at + 0.85);
    });
  };
  chime();
  const id = setInterval(chime, RING_EVERY_MS);
  return () => {
    clearInterval(id);
    gain.gain.setTargetAtTime(0, c.currentTime, 0.05);
    setTimeout(() => gain.disconnect(), 300);
  };
}
