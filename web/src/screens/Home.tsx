import { useState, type FormEvent } from "react";
import { AnimatePresence, motion } from "motion/react";
import { ArrowUp } from "lucide-react";
import { DemoFooter } from "../components/DemoFooter";
import { FlashCard, phrasesFor } from "../components/FlashCard";
import { LiveCaptions } from "../components/LiveCaptions";
import { MicButton } from "../components/MicButton";
import { PreferenceChips } from "../components/PreferenceChips";
import { EASE_OUT, rise } from "../lib/motion";
import type { Line } from "../lib/state";
import type { ListenState } from "../lib/useCaseRun";

interface Props {
  busy: boolean;
  /** The mic tap: starts the patient's voice intake. */
  onMic: () => void;
  /** "or just show me": a Watch case, no mic needed. */
  onStart: () => void;
  listen: ListenState;
  /** Lana's latest line to the patient. */
  lana: Line | null;
  onSay: (text: string) => void;
  micLevel: () => number;
  voice: boolean;
  setVoice: (v: boolean) => void;
  clinics: 2 | 3;
  setClinics: (n: 2 | 3) => void;
}

/** Everything but the mic leaves quickly, so the mic is alone on stage as it flies up. */
const leave = {
  exit: { opacity: 0, filter: "blur(2px)" },
  transition: { duration: 0.16, ease: EASE_OUT },
};

export function Home({ busy, onMic, onStart, listen, lana, onSay, micLevel, voice, setVoice, clinics, setClinics }: Props) {
  const active = listen.active;
  const listening = active && listen.mic === "on" && !listen.lanaSpeaking && !listen.thinking;

  return (
    <motion.div className="flex min-h-dvh flex-col" exit={{ opacity: 1 }} transition={{ duration: 0.46 }}>
      <main className="flex flex-1 flex-col items-center px-4 pb-10 pt-8 text-center sm:pt-10">
        <motion.div {...leave} className="flex flex-col items-center">
          <span className="inline-flex items-center gap-2 rounded-full border border-hairline bg-surface px-3 py-1.5 text-[13px] text-muted">
            <span className="size-1.5 rounded-full bg-primary" aria-hidden />
            Your personal health companion
          </span>
          <h1 className="mt-6 font-serif font-normal tracking-[-0.02em]">
            <span className="block text-[clamp(56px,8vw,104px)] leading-[0.95] text-ink">Hi, I'm Lana.</span>
            <span className="mt-2 block text-[clamp(36px,5.4vw,70px)] italic leading-[1.05] text-primary">
              I'll handle your care, start to finish.
            </span>
          </h1>
          <p className="mt-5 max-w-[46ch] text-[18px] leading-[1.5] text-muted sm:text-[20px]">
            No phone calls, no waiting on hold. Just tell me what you need, and I'll take care of the rest.
          </p>
        </motion.div>

        <div className="mt-3 sm:mt-4">
          {/* Once the session is open, tapping again does nothing: Lana is already listening. */}
          <MicButton busy={busy} onStart={active ? () => {} : onMic} listening={listening} level={micLevel} />
        </div>

        <motion.div {...leave} className="mt-4 flex w-full flex-col items-center gap-4">
          {active && (
            <LiveCaptions lana={lana} partial={listen.partial} heard={listen.heard} thinking={listen.thinking} />
          )}

          <AnimatePresence initial={false}>
            {listen.done && listen.preferences && (
              <motion.div key="ready" {...rise(0, 6, 0.2)} className="flex flex-col items-center gap-3">
                <p className="inline-flex items-center gap-2 speaker-label text-primary">
                  <span className="size-1.5 rounded-full bg-primary" aria-hidden />
                  READY
                </p>
                {/* TODO(Part 12b): start the calls with these preferences. */}
                <PreferenceChips preferences={listen.preferences} draft={listen.draft} />
              </motion.div>
            )}
          </AnimatePresence>

          <FlashCard phrases={phrasesFor(listen)} onPick={active && listen.fallback ? onSay : undefined} />

          {active && listen.fallback && <TypeBox onSay={onSay} />}

          {!active && (
            <button
              type="button"
              onClick={onStart}
              disabled={busy}
              className="pressable rounded-md text-[14px] text-muted underline decoration-hairline underline-offset-4 hover:text-ink hover:decoration-muted disabled:opacity-60"
            >
              or just show me
            </button>
          )}
        </motion.div>
      </main>

      <motion.div {...leave}>
        <DemoFooter voice={voice} setVoice={setVoice} clinics={clinics} setClinics={setClinics} />
      </motion.div>
    </motion.div>
  );
}

/** The no-mic fallback: anything typed here takes the same path as speech. */
function TypeBox({ onSay }: { onSay: (text: string) => void }) {
  const [text, setText] = useState("");

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    onSay(text);
    setText("");
  }

  return (
    <motion.form
      {...rise(0, 4, 0.18)}
      onSubmit={submit}
      className="flex w-full max-w-[460px] items-center gap-2 rounded-full border border-hairline bg-surface py-1.5 pl-4 pr-1.5 shadow-soft focus-within:border-primary/40"
    >
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Type what you need"
        aria-label="Type what you need"
        className="min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none placeholder:text-muted"
      />
      <button
        type="submit"
        disabled={!text.trim()}
        aria-label="Send"
        className="pressable grid size-8 shrink-0 place-items-center rounded-full bg-primary text-white hover:bg-primary-press disabled:opacity-40"
      >
        <ArrowUp size={16} aria-hidden />
      </button>
    </motion.form>
  );
}
