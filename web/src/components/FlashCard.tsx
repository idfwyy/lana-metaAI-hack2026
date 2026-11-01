import { AnimatePresence, motion } from "motion/react";
import { EASE_OUT, rise } from "../lib/motion";
import type { IntakeTopic } from "../lib/types";
import type { ListenState } from "../lib/useCaseRun";

const HOME = ["Find me braces near Dallas under $5,000", "Just show me", "Let me be the clinic"];
const READY = ["Just show me", "Start over"];
const BY_TOPIC: Record<IntakeTopic, string[]> = {
  treatment: ["Braces", "Clear aligners", "Not sure yet"],
  location: ["Near Dallas", "76010", "Skip"],
  budget: ["Under $5,000", "Skip"],
  pay: ["Monthly", "In full if it's cheaper", "With my HSA"],
  records: ["No x-rays yet", "I have x-rays"],
  must_haves: ["Evening hours", "Nothing else"],
};

/** What the patient can say right now: 2 or 3 short phrases. */
export function phrasesFor(listen: ListenState): string[] {
  if (listen.done) return READY;
  if (listen.topic) return BY_TOPIC[listen.topic];
  return HOME;
}

interface Props {
  phrases: string[];
  /** Fallback (no mic, or no speech to text): each phrase is a button that sends it. */
  onPick?: (phrase: string) => void;
}

export function FlashCard({ phrases, onPick }: Props) {
  return (
    <div className="w-full max-w-[460px] rounded-[16px] border border-hairline bg-surface px-4 py-3 text-left shadow-soft">
      <p className="speaker-label text-muted">{onPick ? "TAP ONE" : "YOU CAN SAY"}</p>
      <AnimatePresence mode="wait" initial={false}>
        <motion.ul
          key={phrases.join("|")}
          {...rise(0, 4, 0.18)}
          exit={{ opacity: 0, transition: { duration: 0.1, ease: EASE_OUT } }}
          className="mt-2 flex flex-wrap gap-2"
        >
          {phrases.map((phrase) => (
            <li key={phrase}>
              {onPick ? (
                <button
                  type="button"
                  onClick={() => onPick(phrase)}
                  className="pressable rounded-full border border-hairline bg-paper px-3 py-1.5 text-[14px] text-ink hover:border-primary/40 hover:bg-primary-soft"
                >
                  {phrase}
                </button>
              ) : (
                <span className="inline-block rounded-full bg-paper px-3 py-1.5 text-[14px] text-ink">
                  “{phrase}”
                </span>
              )}
            </li>
          ))}
        </motion.ul>
      </AnimatePresence>
    </div>
  );
}
