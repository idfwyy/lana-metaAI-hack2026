// The patient's mic: 16-bit mono PCM at 24 kHz (the server's AUDIO_FORMAT) in 100 ms binary
// frames, plus a level for the mic halo. The player pauses it while Lana speaks, so she never
// hears herself. Its own AudioContext, separate from playback, so stopping it never cuts Lana off.

// The worklet's source, loaded from a Blob URL: Vite would inline a small ?url import as a
// data: URL, which audioWorklet.addModule doesn't accept everywhere.
import workletSource from "./mic-worklet.js?raw";

let workletUrl: string | null = null;
function worklet(): string {
  workletUrl ??= URL.createObjectURL(new Blob([workletSource], { type: "text/javascript" }));
  return workletUrl;
}

export type MicFailure = "denied" | "none" | "unsupported";

export class MicError extends Error {
  constructor(readonly reason: MicFailure) {
    super(
      reason === "denied"
        ? "The mic is blocked for this site."
        : reason === "none"
          ? "No mic was found."
          : "This browser can't record audio here.",
    );
  }
}

const LEVEL_ATTACK = 0.5; // how fast the level rises toward a louder reading
const LEVEL_RELEASE = 0.08; // and how slowly it falls back

export interface Mic {
  /** 0 to 1, smoothed; 0 while paused. Cheap enough to read every frame. */
  level: () => number;
  pause: () => void;
  resume: () => void;
  stop: () => void;
}

/** Asks for the mic. Rejects with MicError if it's blocked, missing or unsupported. */
export async function startMic(onFrame: (pcm: ArrayBuffer) => void): Promise<Mic> {
  if (!navigator.mediaDevices?.getUserMedia || typeof AudioWorkletNode === "undefined") {
    throw new MicError("unsupported");
  }
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch (err) {
    const name = err instanceof DOMException ? err.name : "";
    throw new MicError(name === "NotFoundError" || name === "OverconstrainedError" ? "none" : "denied");
  }

  const ctx = new AudioContext({ latencyHint: "interactive" });
  try {
    if (ctx.state === "suspended") await ctx.resume();
    await ctx.audioWorklet.addModule(worklet());
  } catch {
    stream.getTracks().forEach((t) => t.stop());
    void ctx.close();
    throw new MicError("unsupported");
  }

  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  const samples = new Float32Array(analyser.fftSize);
  source.connect(analyser);

  const node = new AudioWorkletNode(ctx, "pcm-24k", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
  // The worklet only runs while connected to the output; a muted gain keeps it silent.
  const sink = ctx.createGain();
  sink.gain.value = 0;
  source.connect(node).connect(sink).connect(ctx.destination);

  let paused = false;
  let stopped = false;
  let smoothed = 0;
  node.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
    if (!paused && !stopped) onFrame(e.data);
  };

  return {
    level: () => {
      if (paused || stopped) {
        smoothed = 0;
        return 0;
      }
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const s of samples) sum += s * s;
      // Speech RMS sits around 0.02 to 0.2; map that onto 0 to 1.
      const target = Math.min(1, Math.sqrt(sum / samples.length) * 6);
      smoothed += (target - smoothed) * (target > smoothed ? LEVEL_ATTACK : LEVEL_RELEASE);
      return smoothed;
    },
    pause: () => {
      if (paused) return;
      paused = true;
      node.port.postMessage("pause");
    },
    resume: () => {
      if (!paused) return;
      paused = false;
      node.port.postMessage("resume");
    },
    stop: () => {
      if (stopped) return;
      stopped = true;
      node.port.onmessage = null;
      stream.getTracks().forEach((t) => t.stop());
      void ctx.close().catch(() => {});
    },
  };
}
