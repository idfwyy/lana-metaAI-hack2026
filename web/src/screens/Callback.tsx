import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Phone } from "lucide-react";
import { LiveCaptions } from "../components/LiveCaptions";
import { startRing } from "../lib/audio";
import { EASE_OUT, rise } from "../lib/motion";
import type { Line } from "../lib/state";
import type { ListenState } from "../lib/useCaseRun";

const AUTO_ANSWER_MS = 3000;

interface Props {
  /** Every line so far; only Lana's lines to the patient after the pick-up are shown. */
  lines: Line[];
  listen: ListenState;
  onAnswer: () => void;
  onSay: (text: string) => void;
}

/** Lana calls the patient back: it rings, picks up by itself after 3 s, then asks to book. */
export function Callback({ lines, listen, onAnswer, onSay }: Props) {
  const [answeredAt, setAnsweredAt] = useState<number | null>(null);
  const answered = answeredAt !== null;
  const reduceMotion = useReducedMotion();
  const answerRef = useRef(onAnswer);
  answerRef.current = onAnswer;
  const lineCount = useRef(lines.length);
  lineCount.current = lines.length;

  // Picking up remembers where the call starts, so older lines to the patient stay hidden.
  const answer = () => setAnsweredAt((at) => at ?? lineCount.current);

  // Ring until picked up; pick up by itself after AUTO_ANSWER_MS.
  useEffect(() => {
    if (answered) return;
    const stopRing = startRing();
    const id = setTimeout(answer, AUTO_ANSWER_MS);
    return () => {
      stopRing();
      clearTimeout(id);
    };
  }, [answered]);

  // Tell the server once, whichever way the call was picked up.
  useEffect(() => {
    if (answered) answerRef.current();
  }, [answered]);

  const lana = answered ? ([...lines.slice(answeredAt)].reverse().find((l) => l.callId === null) ?? null) : null;
  const asked = lana !== null && lana.revealed >= lana.words.length && !listen.lanaSpeaking;
  const canReply = asked && !listen.thinking;

  return (
    <motion.main
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0, transition: { duration: 0.16, ease: EASE_OUT } }}
      transition={{ duration: 0.24, ease: EASE_OUT }}
      className="mx-auto flex w-full max-w-[560px] flex-col items-center px-4 pb-16 pt-12 text-center sm:pt-16"
    >
      <div className="relative grid size-[176px] place-items-center">
        {!answered && (
          <>
            <span
              aria-hidden
              className="halo-ring absolute size-[144px] rounded-full bg-primary-soft"
              style={{ animationPlayState: reduceMotion ? "paused" : "running" }}
            />
            <span
              aria-hidden
              className="halo-ring delay absolute size-[172px] rounded-full border border-primary/15"
              style={{ animationPlayState: reduceMotion ? "paused" : "running" }}
            />
          </>
        )}
        <span className="relative grid size-[112px] place-items-center rounded-full bg-primary text-white">
          <Phone size={36} strokeWidth={1.5} aria-hidden />
        </span>
      </div>

      <AnimatePresence mode="wait" initial={false}>
        <motion.div key={answered ? "on" : "ringing"} {...rise(0, 4, 0.2)} exit={{ opacity: 0, transition: { duration: 0.1 } }}>
          <p className="mt-6 font-serif text-[clamp(36px,5vw,52px)] leading-[1.05] tracking-[-0.02em] text-ink">
            {answered ? "On a call with Lana" : "Lana is calling..."}
          </p>
          {!answered && (
            <button
              type="button"
              onClick={answer}
              className="pressable mt-6 rounded-full bg-primary px-6 py-3 text-[15px] font-medium text-white hover:bg-primary-press"
            >
              Answer
            </button>
          )}
        </motion.div>
      </AnimatePresence>

      {answered && (
        <motion.div {...rise(0.1, 6, 0.22)} className="mt-8 flex w-full flex-col items-center gap-6">
          <LiveCaptions lana={lana} partial={listen.partial} heard={listen.heard} thinking={listen.thinking} />
          <div className="flex gap-3">
            <button
              type="button"
              onClick={() => onSay("Book it")}
              disabled={!canReply}
              className="pressable rounded-full bg-primary px-6 py-3 text-[15px] font-medium text-white hover:bg-primary-press disabled:opacity-40"
            >
              Book it
            </button>
            <button
              type="button"
              onClick={() => onSay("Not yet")}
              disabled={!canReply}
              className="pressable rounded-full border border-hairline bg-surface px-6 py-3 text-[15px] font-medium text-ink hover:border-primary/40 disabled:opacity-40"
            >
              Not yet
            </button>
          </div>
          {listen.mic === "on" && canReply && (
            <p className="text-[13px] text-muted">Or just say it.</p>
          )}
        </motion.div>
      )}
    </motion.main>
  );
}
