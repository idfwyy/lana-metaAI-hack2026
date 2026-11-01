// Playback for Lana's and the clinics' lines. One AudioContext, unlocked on the first click.
// Each line's mp3 chunks are joined and decoded whole: decoding partial mp3 frames is
// unreliable in Safari, and lines arrive faster than they play, so only the first one waits.

let ctx: AudioContext | null = null;
const playing = new Set<AudioBufferSourceNode>();

type AudioContextCtor = typeof AudioContext;

function context(): AudioContext | null {
  if (ctx) return ctx;
  const Ctor: AudioContextCtor | undefined =
    window.AudioContext ?? (window as unknown as { webkitAudioContext?: AudioContextCtor }).webkitAudioContext;
  if (!Ctor) return null;
  ctx = new Ctor();
  return ctx;
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

/** Decodes and plays one line. Rejects if audio can't play, so the caller falls back to text. */
export async function playLine(chunks: string[]): Promise<PlayingLine> {
  const c = context();
  if (!c) throw new Error("Web Audio is not available");
  if (c.state !== "running") await c.resume().catch(() => {});
  if (c.state !== "running") throw new Error("Audio is locked");
  if (chunks.length === 0) throw new Error("No audio");

  const buffer = await decode(c, decodeBase64(chunks));
  const src = c.createBufferSource();
  src.buffer = buffer;
  src.connect(c.destination);

  let done = false;
  const ended = new Promise<void>((resolve) => {
    src.onended = () => {
      done = true;
      playing.delete(src);
      resolve();
    };
  });
  const startedAt = c.currentTime;
  src.start(0);
  playing.add(src);

  return {
    duration: buffer.duration,
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
