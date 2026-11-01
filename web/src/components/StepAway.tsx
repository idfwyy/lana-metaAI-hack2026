import { motion } from "motion/react";
import { EASE_OUT } from "../lib/motion";
import { statusLine, type RunState } from "../lib/state";

/**
 * The calm screen while the patient steps away. The calls keep running underneath with
 * their audio off; one quiet line keeps up with what Lana is doing.
 */
export function StepAway({ state, onBack }: { state: RunState; onBack: () => void }) {
  const latest = [...state.lines].reverse().find((l) => l.callId !== null && l.revealed > 0);
  const caption = latest ? latest.words.slice(0, latest.revealed).join(" ") : null;

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0, transition: { duration: 0.16, ease: EASE_OUT } }}
      transition={{ duration: 0.24, ease: EASE_OUT }}
      className="absolute inset-0 z-10 grid place-items-center bg-paper/95 px-4"
    >
      <div className="flex max-w-[520px] flex-col items-center text-center">
        <p className="font-serif text-[clamp(40px,6vw,64px)] leading-[1.05] tracking-[-0.02em] text-ink">You're free.</p>
        <p className="mt-3 text-[18px] leading-[1.5] text-muted">I'll call you when it's done.</p>

        <div aria-live="polite" className="mt-10 w-full max-w-[420px] text-[13px] leading-[1.6] text-muted">
          <p className="flex items-center justify-center gap-2">
            <span className="live-dot" aria-hidden />
            {statusLine(state)}
          </p>
          {caption && <p className="mt-1 truncate opacity-70">{caption}</p>}
        </div>

        <button
          type="button"
          onClick={onBack}
          className="pressable mt-8 rounded-md text-[14px] text-muted underline decoration-hairline underline-offset-4 hover:text-ink hover:decoration-muted"
        >
          Back to the calls
        </button>
      </div>
    </motion.div>
  );
}
