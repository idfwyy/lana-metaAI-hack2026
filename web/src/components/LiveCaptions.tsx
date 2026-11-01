import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { rise } from "../lib/motion";
import type { Line } from "../lib/state";

interface Props {
  /** Lana's latest line to the patient; its words fade in as her audio reaches them. */
  lana: Line | null;
  /** What the patient is saying now (muted), then what they said (ink). */
  partial: string;
  heard: string | null;
  thinking: boolean;
  /** Who the second row is: "YOU", or "PATIENT" for Watch mode's demo patient. */
  youLabel?: string;
}

/** Live captions under the mic. Rows keep their height so nothing below jumps. */
export function LiveCaptions({ lana, partial, heard, thinking, youLabel = "YOU" }: Props) {
  const you = partial || heard;
  const reduceMotion = useReducedMotion();

  return (
    <div className="flex w-full max-w-[560px] flex-col gap-3 text-left" aria-live="polite">
      <div className="grid min-h-[52px] grid-cols-[64px_minmax(0,1fr)] gap-3">
        <span className="speaker-label pt-[6px] text-primary">LANA</span>
        <AnimatePresence mode="wait" initial={false}>
          {thinking ? (
            <motion.p
              key="thinking"
              initial={{ opacity: 0 }}
              animate={
                reduceMotion
                  ? { opacity: 0.8, transition: { duration: 0.2 } }
                  : { opacity: [0.45, 0.9, 0.45], transition: { duration: 1.8, repeat: Infinity, ease: "easeInOut" } }
              }
              exit={{ opacity: 0, transition: { duration: 0.1 } }}
              className="text-[15px] leading-[1.6] text-muted"
            >
              Lana is thinking
            </motion.p>
          ) : lana ? (
            <motion.p
              key={lana.seq}
              {...rise(0, 4, 0.18)}
              exit={{ opacity: 0, transition: { duration: 0.1 } }}
              className="text-[16px] leading-[1.6] text-ink"
            >
              {lana.words.map((w, i) => (
                <span key={i} className="word" style={{ opacity: i < lana.revealed ? 1 : 0 }}>
                  {w}{" "}
                </span>
              ))}
            </motion.p>
          ) : (
            <span key="none" />
          )}
        </AnimatePresence>
      </div>

      <div className="grid min-h-[28px] grid-cols-[64px_minmax(0,1fr)] gap-3">
        <span className="speaker-label pt-[6px] text-muted" style={{ opacity: you ? 1 : 0.45 }}>
          {youLabel}
        </span>
        {/* Partials update in place: they change several times a second, so no motion. */}
        <p className={`text-[16px] leading-[1.6] ${partial ? "text-muted" : "text-ink"}`}>{you}</p>
      </div>
    </div>
  );
}
