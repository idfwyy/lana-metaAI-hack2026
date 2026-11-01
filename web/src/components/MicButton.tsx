import { motion } from "motion/react";
import { Mic } from "lucide-react";
import { EASE_OUT, ORB_MORPH } from "../lib/motion";

interface Props {
  busy: boolean;
  onStart: () => void;
}

/** The big mic. Its disc shares layoutId "lana-orb" with StatusOrb, so it morphs into the top bar. */
export function MicButton({ busy, onStart }: Props) {
  return (
    <div className="relative grid size-[216px] place-items-center sm:size-[240px]">
      {/* Halo: breathes while idle, collapses into the disc when the page unfolds. */}
      <motion.span
        aria-hidden
        className="pointer-events-none absolute inset-0 grid place-items-center"
        exit={{ opacity: 0, scale: 0.72 }}
        transition={{ duration: 0.2, ease: EASE_OUT }}
      >
        <span
          className="halo-ring absolute size-[172px] rounded-full bg-primary-soft sm:size-[192px]"
          style={{ animationPlayState: busy ? "paused" : "running" }}
        />
        <span
          className="halo-ring delay absolute size-[206px] rounded-full border border-primary/15 sm:size-[230px]"
          style={{ animationPlayState: busy ? "paused" : "running" }}
        />
      </motion.span>

      <button
        type="button"
        onClick={onStart}
        disabled={busy}
        aria-label="Talk to Lana"
        aria-busy={busy}
        className="pressable relative rounded-full disabled:cursor-progress"
      >
        <motion.span
          layoutId="lana-orb"
          transition={{ layout: ORB_MORPH }}
          style={{ borderRadius: 999 }}
          className="grid size-[136px] place-items-center bg-primary text-white sm:size-[152px]"
        >
          <Mic size={45} strokeWidth={1.5} aria-hidden />
        </motion.span>
        {busy && (
          <motion.svg
            aria-hidden
            viewBox="0 0 100 100"
            className="trace-ring pointer-events-none absolute -inset-2"
            exit={{ opacity: 0 }}
            transition={{ duration: 0.12 }}
          >
            <circle
              cx="50"
              cy="50"
              r="48.5"
              fill="none"
              stroke="var(--color-primary)"
              strokeOpacity="0.55"
              strokeWidth="1"
              strokeLinecap="round"
              strokeDasharray="40 265"
            />
          </motion.svg>
        )}
      </button>
    </div>
  );
}
