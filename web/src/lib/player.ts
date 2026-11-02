// The playback timeline. Every message is buffered and played strictly in seq order.
// A line's words are revealed while its audio plays, and the next item waits for that
// audio to end. Other events apply when the timeline reaches them. Every wait has a cap,
// so the run can never freeze.
//
// A line is decoded only once its last frame is in, so every chunk is always played, and
// its audio may run for its whole decoded length: the cap is that length plus a margin.
//
// Sound off is a mute in audio.ts: lines keep playing silently, so the words keep their pace
// and sound on picks up mid-word. In a case that started without sound there is no audio to
// unmute: needAudio() lists the lines to ask the server for, and their late audio plays as
// it arrives, taking over a text-only line from where its words are.

import { playLine, stopAllAudio, type PlayingLine } from "./audio";
import { isLine, type AudioFrame, type LineEvent, type ServerEvent, type ServerMessage } from "./types";

const WORDS_PER_SECOND = 2.6; // text-only reveal speed
const MUTED_MS_PER_WORD = 40; // stepped away: lines reveal fast so the timeline catches up
const AUDIO_MARGIN_MS = 3_000; // audio still playing this long past its decoded length is stopped
const FRAME_WAIT_MS = 12_000; // no audio frame for this long: treat the line as failed
const LATE_WAIT_MS = 2_000; // a line's late audio isn't ready this long after its turn: show it as text
const LATE_MIN_LEFT_MS = 1_500; // the line on screen gets late audio only if this much of it is left
const GAP_SKIP_MS = 3_000; // a missing seq is skipped after this long
const TICK_MS = 33;

interface LineAudio {
  chunks: string[];
  done: boolean;
  failed: boolean;
  /** Asked for after sound came on (need_audio): replaces the line's earlier failed marker. */
  late: boolean;
  lastFrameAt: number;
}

/** Decodes and starts one line's audio, `from` a fraction of the way in (audio.ts playLine; a fake in tests). */
export type PlayFn = (chunks: string[], from?: number) => Promise<PlayingLine>;

export interface PlayerHandlers {
  apply: (event: ServerEvent) => void;
  reveal: (seq: number, count: number) => void;
  /**
   * Lana's line starts (true) or ends (false). toPatient: she is talking to the patient, who may
   * talk over her (barge-in); otherwise it is a clinic call and the mic stays muted.
   */
  speaking?: (on: boolean, toPatient: boolean) => void;
  /** Be the clinic: Lana's lines on this call are to the judge, who may talk over her too. */
  bargeable?: (callId: string) => boolean;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Where each word starts, as a fraction of the line, weighted by its length: long words take longer to say. */
export function wordStarts(words: string[]): number[] {
  const total = words.reduce((n, w) => n + w.length + 1, 0);
  let at = 0;
  return words.map((w) => {
    const start = total ? at / total : 0;
    at += w.length + 1;
    return start;
  });
}

/** How many words to show once a fraction p of the line has played: every word that has started. */
export function revealedAt(starts: number[], p: number): number {
  let n = 0;
  while (n < starts.length && starts[n] <= p) n++;
  return Math.max(Math.min(1, starts.length), n);
}

export class Player {
  private buffer = new Map<number, ServerMessage>();
  private audio = new Map<number, LineAudio>();
  private next: number | null = null;
  private pumping = false;
  private gapTimer: ReturnType<typeof setTimeout> | null = null;
  private gen = 0;
  private current: PlayingLine | null = null;
  private muted = false;
  private toPatient: number | null = null; // seq of Lana's line to the patient that is playing
  private barged: number | null = null; // seq of the line the patient talked over
  private lastSeq = 0; // the highest seq received
  /** The line on screen; endsAt is when its text-only reveal ends (null: not started yet). */
  private onScreen: { seq: number; endsAt: number | null } | null = null;
  // need_audio: the lines asked for, the last seq received when asking (later lines were in
  // flight, so the server voices those too), and the first line after it the server voiced as usual.
  private requested = new Set<number>();
  private lateFrom: number | null = null;
  private voicedFrom: number | null = null;

  constructor(
    private readonly handlers: PlayerHandlers,
    private readonly play: PlayFn = playLine,
  ) {}

  /** Stepped away: call audio is skipped and lines reveal quickly. Lana's own lines still play. */
  setMuted(on: boolean): void {
    this.muted = on;
    if (on) this.current?.stop();
  }

  /**
   * Sound came on in a case that started without it: the lines to send in need_audio. The line
   * on screen counts if enough of it is left, then every line still waiting in the buffer.
   */
  needAudio(): { lineSeqs: number[]; after: number } {
    const seqs: number[] = [];
    const now = this.onScreen;
    if (now && (now.endsAt === null || now.endsAt - performance.now() > LATE_MIN_LEFT_MS)) seqs.push(now.seq);
    for (const [seq, m] of this.buffer) if (isLine(m)) seqs.push(seq);
    seqs.sort((a, b) => a - b);
    for (const seq of seqs) this.requested.add(seq);
    this.lateFrom = this.lastSeq;
    this.voicedFrom = null;
    return { lineSeqs: seqs, after: this.lastSeq };
  }

  push(m: ServerMessage): void {
    if (m.type === "audio") this.storeFrame(m);
    if (m.seq == null) {
      // Socket-level messages (e.g. not_found) have no seq: apply at once.
      if (m.type !== "audio") this.handlers.apply(m);
      return;
    }
    this.lastSeq = Math.max(this.lastSeq, m.seq);
    if (this.next === null) this.next = m.seq;
    if (m.seq < this.next) return; // already played (or skipped)
    this.buffer.set(m.seq, m);
    void this.pump();
  }

  /**
   * The patient talked over Lana: her line to them stops now and shows whole. Returns false if
   * no such line is playing (a clinic call, or she already finished).
   */
  bargeIn(): boolean {
    if (this.toPatient === null) return false;
    this.barged = this.toPatient;
    this.current?.stop();
    return true;
  }

  /** Drops everything: Stop, Start over, or a reconnect that restarts the run. */
  reset(): void {
    this.gen++;
    this.buffer.clear();
    this.audio.clear();
    this.next = null;
    this.pumping = false;
    if (this.gapTimer) clearTimeout(this.gapTimer);
    this.gapTimer = null;
    this.current?.stop();
    this.current = null;
    stopAllAudio();
    this.toPatient = null;
    this.barged = null;
    this.lastSeq = 0;
    this.onScreen = null;
    this.requested.clear();
    this.lateFrom = null;
    this.voicedFrom = null;
    this.handlers.speaking?.(false, false);
  }

  private storeFrame(f: AudioFrame): void {
    let a = this.audio.get(f.line_seq);
    if (f.late) {
      // Late audio replaces the failed marker sent while sound was off.
      if (!a?.late) a = { chunks: [], done: false, failed: false, late: true, lastFrameAt: 0 };
    } else if (a?.late) {
      return;
    }
    a ??= { chunks: [], done: false, failed: false, late: false, lastFrameAt: 0 };
    if (f.chunk_b64) a.chunks.push(f.chunk_b64);
    if (f.failed) a.failed = true;
    if (f.last) a.done = true;
    a.lastFrameAt = performance.now();
    this.audio.set(f.line_seq, a);
    // The first line after the ask that came voiced as usual: lines from here on need nothing late.
    if (!f.late && f.chunk_b64 && this.lateFrom !== null && f.line_seq > this.lateFrom) {
      this.voicedFrom = Math.min(this.voicedFrom ?? f.line_seq, f.line_seq);
    }
  }

  /** This line went out as text only and its late audio is on the way. */
  private expectsLate(seq: number): boolean {
    if (this.requested.has(seq)) return true;
    return this.lateFrom !== null && seq > this.lateFrom && (this.voicedFrom === null || seq < this.voicedFrom);
  }

  /** The line's late audio, once all of it is in. */
  private takeLate(seq: number): string[] | null {
    const a = this.audio.get(seq);
    if (!a?.late || !a.done) return null;
    this.audio.delete(seq);
    return a.failed || a.chunks.length === 0 ? null : a.chunks;
  }

  private async pump(): Promise<void> {
    if (this.pumping) return;
    this.pumping = true;
    const gen = this.gen;
    try {
      while (gen === this.gen && this.next !== null) {
        const m = this.buffer.get(this.next);
        if (!m) {
          this.armGapSkip();
          return;
        }
        this.disarmGapSkip();
        this.buffer.delete(this.next);
        this.next++;
        if (m.type === "audio") continue; // stored on arrival; the line consumes it
        if (isLine(m)) await this.playOne(m, gen);
        else this.handlers.apply(m);
      }
    } finally {
      if (gen === this.gen) this.pumping = false;
    }
  }

  private armGapSkip(): void {
    if (this.gapTimer || this.buffer.size === 0) return;
    const gen = this.gen;
    this.gapTimer = setTimeout(() => {
      this.gapTimer = null;
      if (gen !== this.gen || this.buffer.size === 0) return;
      this.next = Math.min(...this.buffer.keys());
      void this.pump();
    }, GAP_SKIP_MS);
  }

  private disarmGapSkip(): void {
    if (this.gapTimer) clearTimeout(this.gapTimer);
    this.gapTimer = null;
  }

  private async playOne(line: LineEvent, gen: number): Promise<void> {
    const lana = line.type === "agent_said" || (line.type === "transcript_final" && line.speaker === "agent");
    const toPatient =
      (line.type === "agent_said" && line.call_id === null) ||
      (lana && line.type === "transcript_final" && line.call_id !== null && !!this.handlers.bargeable?.(line.call_id));
    if (toPatient) this.toPatient = line.seq!;
    if (lana) this.handlers.speaking?.(true, toPatient);
    this.onScreen = { seq: line.seq!, endsAt: null };
    try {
      await this.playLineOnce(line, gen);
    } finally {
      if (gen === this.gen) this.onScreen = null;
      if (toPatient) this.toPatient = null;
      if (lana && gen === this.gen) this.handlers.speaking?.(false, toPatient);
    }
  }

  private async playLineOnce(line: LineEvent, gen: number): Promise<void> {
    const seq = line.seq!;
    const wordList = line.text.split(/\s+/).filter(Boolean);
    const words = wordList.length;
    const starts = wordStarts(wordList);
    this.handlers.apply(line);

    // Muted applies to the calls only; Lana and the patient talking to each other (no call id) always play.
    const callId = line.type === "user_said" ? null : line.call_id;
    const quiet = this.muted && callId !== null;
    const chunks = await this.waitForAudio(seq, gen, !quiet);
    if (gen !== this.gen) return;

    if (quiet) {
      for (let i = 1; i <= words && gen === this.gen; i++) {
        this.handlers.reveal(seq, i);
        await sleep(MUTED_MS_PER_WORD);
      }
      return;
    }

    let played = chunks ? await this.playAudio(seq, chunks, starts, gen, 0) : false;
    if (gen !== this.gen) return;

    if (!played && this.barged !== seq) {
      const total = (words / WORDS_PER_SECOND) * 1000;
      const startedAt = performance.now();
      if (this.onScreen?.seq === seq) this.onScreen.endsAt = startedAt + total;
      while (gen === this.gen) {
        const p = (performance.now() - startedAt) / total;
        if (p >= 1) break;
        // Sound came on and this line's late audio is in: it carries on from where the words are.
        const late = this.takeLate(seq);
        if (late) {
          played = await this.playAudio(seq, late, starts, gen, p);
          if (played) break;
        }
        this.handlers.reveal(seq, revealedAt(starts, p));
        await sleep(TICK_MS);
      }
    }
    // The last word shows the moment the audio ends.
    if (gen === this.gen) this.handlers.reveal(seq, words);
  }

  /** Plays the line's audio from a fraction of the way in, revealing words as it goes. False if it couldn't play. */
  private async playAudio(seq: number, chunks: string[], starts: number[], gen: number, from: number): Promise<boolean> {
    let handle: PlayingLine;
    try {
      handle = await this.play(chunks, from);
    } catch {
      return false;
    }
    if (gen !== this.gen) {
      handle.stop();
      return true;
    }
    this.current = handle;
    const startedAt = performance.now();
    const capMs = handle.duration * (1 - from) * 1000 + AUDIO_MARGIN_MS;
    let ended = false;
    void handle.ended.then(() => (ended = true));
    while (!ended && gen === this.gen && performance.now() - startedAt < capMs) {
      const p = handle.duration > 0 ? handle.elapsed() / handle.duration : 1;
      this.handlers.reveal(seq, revealedAt(starts, p));
      await sleep(TICK_MS);
    }
    if (!ended) handle.stop();
    this.current = null;
    if (import.meta.env?.DEV) {
      const playedS = (performance.now() - startedAt) / 1000;
      console.debug(
        `[line ${seq}] decoded ${handle.duration.toFixed(2)} s, from ${(from * 100).toFixed(0)}%, ` +
          `played ${playedS.toFixed(2)} s${ended ? "" : " (stopped at the cap)"}`,
      );
    }
    return true;
  }

  /**
   * Resolves with the line's chunks, or null if its audio failed or never came. A line whose
   * late audio is on the way waits for it up to LATE_WAIT_MS (lateOk false: no such wait).
   */
  private async waitForAudio(seq: number, gen: number, lateOk = true): Promise<string[] | null> {
    const reachedAt = performance.now();
    while (gen === this.gen) {
      if (this.barged === seq) return null; // talked over before her audio even arrived
      const a = this.audio.get(seq);
      if (lateOk && (a?.late || this.expectsLate(seq))) {
        const late = this.takeLate(seq);
        if (late) return late;
        if (a?.late && a.done) return null; // its late audio failed too
        if (performance.now() - reachedAt > LATE_WAIT_MS) return null;
      } else if (a?.done) {
        this.audio.delete(seq);
        return a.failed || a.chunks.length === 0 ? null : a.chunks;
      } else if (performance.now() - Math.max(reachedAt, a?.lastFrameAt ?? 0) > FRAME_WAIT_MS) {
        return null;
      }
      await sleep(40);
    }
    return null;
  }
}
