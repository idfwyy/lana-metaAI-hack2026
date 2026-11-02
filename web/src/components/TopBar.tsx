import { motion } from "motion/react";
import { EASE_OUT } from "../lib/motion";
import { SoundSwitch } from "./DemoFooter";
import { StatusOrb } from "./StatusOrb";

interface Props {
  status: string;
  live: boolean;
  action: { label: string; onClick: () => void; primary?: boolean };
  /** The Sound toggle, while there is something to hear. */
  sound?: { on: boolean; set: (on: boolean) => void };
}

/** Lana's orb and what she's doing. Stays mounted from Working through Done. */
export function TopBar({ status, live, action, sound }: Props) {
  return (
    <header className="relative flex h-16 shrink-0 items-center gap-3 px-4 sm:px-8">
      <StatusOrb live={live} />
      <motion.p
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.26, delay: 0.3 }}
        aria-live="polite"
        className="min-w-0 truncate text-[15px] text-ink"
      >
        {status}
      </motion.p>
      {sound && (
        <div className="ml-auto shrink-0 text-[13px] text-muted">
          <SoundSwitch voice={sound.on} setVoice={sound.set} compact />
        </div>
      )}
      <button
        type="button"
        onClick={action.onClick}
        className={`pressable shrink-0 ${sound ? "" : "ml-auto "} rounded-full px-4 py-2 text-[14px] font-medium ${
          action.primary ? "bg-primary text-white hover:bg-primary-press" : "text-muted hover:bg-hairline/60 hover:text-ink"
        }`}
      >
        {action.label}
      </button>
      <motion.span
        aria-hidden
        className="absolute inset-x-0 bottom-0 h-px origin-left bg-hairline"
        initial={{ transform: "scaleX(0)" }}
        animate={{ transform: "scaleX(1)" }}
        transition={{ duration: 0.26, delay: 0.3, ease: EASE_OUT }}
      />
    </header>
  );
}
