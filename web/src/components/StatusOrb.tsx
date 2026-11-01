import { motion } from "motion/react";
import { Mic } from "lucide-react";
import { ORB_MORPH } from "../lib/motion";

/**
 * Lana's small orb in the top bar. Same layoutId as the big mic, so the mic flies here.
 * The mic glyph is sized so that, scaled up at the start of the morph, it matches the big
 * mic's glyph; it fades out as the orb lands.
 */
export function StatusOrb({ live }: { live: boolean }) {
  return (
    <span className="relative grid size-7 shrink-0 place-items-center">
      <motion.span
        layoutId="lana-orb"
        transition={{ layout: ORB_MORPH }}
        style={{ borderRadius: 999 }}
        className="absolute inset-0 grid place-items-center bg-primary text-white"
      >
        <motion.span
          initial={{ opacity: 1, filter: "blur(0px)" }}
          animate={{ opacity: 0, filter: "blur(2px)" }}
          transition={{ duration: 0.22, delay: 0.08 }}
          className="grid place-items-center"
        >
          <Mic size={8} strokeWidth={1.5} aria-hidden />
        </motion.span>
      </motion.span>
      {live && <span className="live-dot absolute -right-0.5 -top-0.5 ring-2 ring-paper" aria-label="On a call" />}
    </span>
  );
}
