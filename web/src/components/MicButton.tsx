import { useEffect, useRef } from "react";
import { motion, useReducedMotion } from "motion/react";
import { Mic } from "lucide-react";
import { EASE_OUT, ORB_MORPH } from "../lib/motion";

interface Props {
  busy: boolean;
  onStart: () => void;
  /** The mic is open and Lana is waiting for the patient: the halo follows their voice. */
  listening?: boolean;
  /** 0 to 1, read once per frame while listening. */
  level?: () => number;
}

const LEVEL_SCALE = 0.35; // the halo grows by up to 35% at full voice

/** The big mic. Its disc shares layoutId "lana-orb" with StatusOrb, so it morphs into the top bar. */
export function MicButton({ busy, onStart, listening = false, level }: Props) {
  const halo = useRef<HTMLSpanElement>(null);
  const reduceMotion = useReducedMotion();

  // Written straight to the element every frame, so the voice never re-renders React.
  useEffect(() => {
    const el = halo.current;
    if (!el) return;
    if (!listening || !level) {
      el.style.transform = "";
      el.style.opacity = "";
      return;
    }
    let frame = 0;
    const tick = () => {
      const v = level();
      if (reduceMotion) el.style.opacity = String(0.55 + v * 0.45);
      else el.style.transform = `scale(${1 + v * LEVEL_SCALE})`;
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      el.style.transform = "";
      el.style.opacity = "";
    };
  }, [listening, level, reduceMotion]);

  // While listening the rings hold still, so only the voice moves them.
  const ring = { animation: listening ? "none" : undefined, animationPlayState: busy ? "paused" : "running" } as const;

  return (
    <div className="relative grid size-[216px] place-items-center sm:size-[240px]">
      {/* Halo: breathes while idle, follows the voice while listening, collapses into the disc when the page unfolds. */}
      <motion.span
        aria-hidden
        className="pointer-events-none absolute inset-0 grid place-items-center"
        exit={{ opacity: 0, scale: 0.72 }}
        transition={{ duration: 0.2, ease: EASE_OUT }}
      >
        <span ref={halo} className="absolute inset-0 grid place-items-center will-change-transform">
          <span
            className="halo-ring absolute size-[172px] rounded-full bg-primary-soft sm:size-[192px]"
            style={{ ...ring, opacity: listening ? 0.8 : undefined }}
          />
          <span
            className="halo-ring delay absolute size-[206px] rounded-full border border-primary/15 sm:size-[230px]"
            style={{ ...ring, opacity: listening ? 0.7 : undefined }}
          />
        </span>
      </motion.span>

      <button
        type="button"
        onClick={onStart}
        disabled={busy}
        aria-label={listening ? "Lana is listening" : "Talk to Lana"}
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
