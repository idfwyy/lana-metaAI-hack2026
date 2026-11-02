// Sound on and off mid-run. The player runs on real timers with a fake PlayFn (short lines),
// and audio.ts runs against a fake AudioContext to check the mute and the mid-line start.

import assert from "node:assert/strict";
import { playLine, setSoundOff, unlockAudio, type PlayingLine } from "../src/lib/audio";
import { Player } from "../src/lib/player";
import type { ServerMessage } from "../src/lib/types";

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

async function until(check: () => boolean, ms = 5_000): Promise<void> {
  const deadline = performance.now() + ms;
  while (!check()) {
    assert.ok(performance.now() < deadline, "timed out");
    await sleep(10);
  }
}

async function test(name: string, fn: () => Promise<void>): Promise<void> {
  await fn();
  console.log(`ok ${name}`);
}

// ---------- a fake Web Audio, enough for audio.ts ----------

const gains: { value: number; targets: number[] }[] = [];
const sources: { offset: number | null; stopped: boolean }[] = [];

class FakeContext {
  state = "running";
  currentTime = 0;
  destination = {};
  resume = async () => {};
  createGain() {
    const g = { value: 1, targets: [] as number[] };
    gains.push(g);
    return {
      gain: {
        get value() {
          return g.value;
        },
        set value(v: number) {
          g.value = v;
        },
        setTargetAtTime: (v: number) => {
          g.targets.push(v);
          g.value = v;
        },
      },
      connect: () => {},
      disconnect: () => {},
    };
  }
  createBuffer() {
    return { duration: 0 };
  }
  createBufferSource() {
    const s = { offset: null as number | null, stopped: false };
    sources.push(s);
    return {
      buffer: null,
      onended: null as (() => void) | null,
      connect: () => {},
      start(_when: number, offset = 0) {
        s.offset = offset;
      },
      stop() {
        s.stopped = true;
        this.onended?.();
      },
    };
  }
  decodeAudioData(_data: ArrayBuffer, resolve: (b: { duration: number }) => void) {
    resolve({ duration: 4 });
  }
}
(globalThis as unknown as { window: unknown }).window = { AudioContext: FakeContext };

await test("sound off and on is a mute: the line keeps playing and picks up where it is", async () => {
  unlockAudio();
  const master = gains[0];
  const line = await playLine([btoa("mp3")]);
  setSoundOff(true);
  assert.equal(master.value, 0);
  setSoundOff(false);
  assert.equal(master.value, 1);
  assert.deepEqual(master.targets, [0, 1]);
  const src = sources[sources.length - 1];
  assert.equal(src.stopped, false, "muting must not stop the line");
  assert.equal(src.offset, 0);
  line.stop();
});

await test("late audio starts mid-line, from where the words are", async () => {
  const line = await playLine([btoa("mp3")], 0.5);
  assert.equal(sources[sources.length - 1].offset, 2); // half of the 4 s line
  line.stop();
});

// ---------- the player ----------

interface Played {
  line: string;
  from: number;
  stopped: boolean;
}

/** A fake PlayFn: each line's chunks name it; it "plays" for durationS from `from`. */
function fakePlay(played: Played[], durationS = 0.3) {
  return async (chunks: string[], from = 0): Promise<PlayingLine> => {
    const entry = { line: chunks[0], from, stopped: false };
    played.push(entry);
    const startedAt = performance.now() - from * durationS * 1000;
    let stop = () => {};
    const ended = new Promise<void>((resolve) => {
      const t = setTimeout(resolve, (1 - from) * durationS * 1000);
      stop = () => {
        entry.stopped = true;
        clearTimeout(t);
        resolve();
      };
    });
    return {
      duration: durationS,
      bytes: 0,
      elapsed: () => Math.min(durationS, (performance.now() - startedAt) / 1000),
      ended,
      stop: () => stop(),
    };
  };
}

function harness(durationS?: number) {
  const played: Played[] = [];
  const revealed = new Map<number, number>();
  const player = new Player(
    { apply: () => {}, reveal: (seq, count) => revealed.set(seq, count) },
    fakePlay(played, durationS),
  );
  let seq = 0;
  const say = (text: string) => {
    const s = ++seq;
    player.push({ type: "agent_said", seq: s, case_id: "c", call_id: null, text } as ServerMessage);
    return s;
  };
  const frame = (lineSeq: number, extra: Record<string, unknown>) =>
    player.push({ type: "audio", seq: ++seq, case_id: "c", line_seq: lineSeq, speaker: "agent", ...extra } as ServerMessage);
  return { player, played, revealed, say, frame, last: () => seq };
}

await test("a voiced line plays once, start to end, while sound flips off and on", async () => {
  const h = harness(0.5);
  const one = h.say("Hi there, I am Lana.");
  h.frame(one, { chunk_b64: "one", last: true });
  await until(() => h.played.length === 1);
  setSoundOff(true);
  await sleep(100);
  setSoundOff(false);
  await until(() => h.revealed.get(one) === 5);
  assert.deepEqual(h.played.map((p) => [p.line, p.from, p.stopped]), [["one", 0, false]]);
});

await test("need_audio lines play in order, the one on screen from where its words are", async () => {
  const h = harness();
  // A sound-off case: every line came with a failed marker. Line one is long, so it is on screen a while.
  const one = h.say("This first line is long enough to still be on screen when sound comes on again today.");
  h.frame(one, { last: true, failed: true });
  const two = h.say("Second line.");
  h.frame(two, { last: true, failed: true });
  const three = h.say("Third line.");
  h.frame(three, { last: true, failed: true });

  await sleep(600); // line one is revealing as text
  const ask = h.player.needAudio();
  assert.deepEqual(ask.lineSeqs, [one, two, three]);
  assert.equal(ask.after, h.last());

  // The server voices them in seq order; three arrives first here to show order comes from the timeline.
  h.frame(three, { chunk_b64: "three", last: true, late: true });
  h.frame(one, { chunk_b64: "one", last: true, late: true });
  h.frame(two, { chunk_b64: "two", last: true, late: true });

  await until(() => h.played.length === 3);
  assert.deepEqual(h.played.map((p) => p.line), ["one", "two", "three"]);
  assert.ok(h.played[0].from > 0 && h.played[0].from < 1, `line one picks up mid-line (from ${h.played[0].from})`);
  assert.equal(h.played[1].from, 0);
  assert.equal(h.played[2].from, 0);
});

await test("late audio that isn't ready within 2 s: the line shows as text and the run moves on", async () => {
  const h = harness();
  const one = h.say("Short.");
  h.frame(one, { last: true, failed: true });
  const two = h.say("Waiting on audio.");
  h.frame(two, { last: true, failed: true });
  h.player.needAudio();
  const started = performance.now();
  await until(() => h.revealed.get(two) === 3, 8_000);
  const waited = performance.now() - started;
  assert.ok(waited >= 2_000, `waited ${waited.toFixed(0)} ms for late audio`);
  assert.deepEqual(h.played, []);
});

await test("lines sent after the ask (in flight) wait for their late audio too", async () => {
  const h = harness();
  const one = h.say("Short.");
  h.frame(one, { last: true, failed: true });
  await until(() => h.revealed.get(one) === 1);
  h.player.needAudio();
  const two = h.say("In flight when sound came on.");
  h.frame(two, { last: true, failed: true });
  await sleep(300);
  h.frame(two, { chunk_b64: "two", last: true, late: true });
  await until(() => h.played.length === 1);
  assert.equal(h.played[0].line, "two");
});
