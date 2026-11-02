import { useId, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { ChevronDown } from "lucide-react";
import { unlockAudio } from "../lib/audio";
import { EASE_DRAWER } from "../lib/motion";

/** Flips closer together than this are ignored, so a double tap can't leave sound out of step. */
const SOUND_DEBOUNCE_MS = 300;
let lastSoundFlip = -Infinity; // shared by every Sound switch: they all set the same state

export const DISCLAIMER =
  "Quick heads-up: this is a live demo. To respect real clinics' time, I won't call real offices during judging. I'll call demo clinics instead. Each one is its own AI receptionist with hidden prices and limits, so I really do have to negotiate. Every call starts with me saying I'm an AI, and your voice is only used for this session.";

interface Props {
  voice: boolean;
  setVoice: (v: boolean) => void;
  clinics: 2 | 3;
  setClinics: (n: 2 | 3) => void;
  /** Opens the welcome card again. */
  onHowItWorks: () => void;
}

export function DemoFooter({ voice, setVoice, clinics, setClinics, onHowItWorks }: Props) {
  const [open, setOpen] = useState(false);

  return (
    <footer className="border-t border-hairline px-4 py-4 sm:px-8">
      <div className="mx-auto flex max-w-[1120px] flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <div className="flex items-center gap-5">
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            aria-controls="about-demo"
            className="pressable inline-flex items-center gap-1.5 rounded-md text-[13px] text-muted hover:text-ink"
          >
            About this demo
            <ChevronDown
              size={14}
              aria-hidden
              className="transition-transform duration-200 ease-[var(--ease-out)]"
              style={{ transform: open ? "rotate(180deg)" : "none" }}
            />
          </button>
          <button
            type="button"
            onClick={onHowItWorks}
            aria-haspopup="dialog"
            className="pressable rounded-md text-[13px] text-muted hover:text-ink"
          >
            How it works
          </button>
        </div>

        <div className="flex items-center gap-5 text-[13px] text-muted">
          <SoundSwitch voice={voice} setVoice={setVoice} />
          <ClinicsPicker clinics={clinics} setClinics={setClinics} />
        </div>
      </div>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            id="about-demo"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.26, ease: EASE_DRAWER }}
            className="overflow-hidden"
          >
            <p className="mx-auto max-w-[1120px] pt-3 text-[14px] leading-[1.6] text-muted">
              <span className="block max-w-[64ch]">{DISCLAIMER}</span>
              <span className="mt-2 block max-w-[64ch]">
                Tip: use headphones. You can talk over Lana at any time to interrupt her, and with headphones
                she never mistakes her own voice for yours.
              </span>
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </footer>
  );
}

/**
 * Lana speaks out loud, or text only. Shared by the footer, the welcome card and the top bar,
 * all on App's one voice state. compact: no on/off word, for the top bar.
 */
export function SoundSwitch({
  voice,
  setVoice,
  compact = false,
}: {
  voice: boolean;
  setVoice: (v: boolean) => void;
  compact?: boolean;
}) {
  function toggle() {
    const now = performance.now();
    if (now - lastSoundFlip < SOUND_DEBOUNCE_MS) return;
    lastSoundFlip = now;
    if (!voice) unlockAudio(); // turning sound on is the gesture that lets the browser play again
    setVoice(!voice);
  }

  return (
    <label className="inline-flex cursor-pointer items-center gap-2">
      Sound
      <button
        type="button"
        role="switch"
        aria-checked={voice}
        onClick={toggle}
        className={`relative h-5 w-9 rounded-full transition-colors duration-200 ${voice ? "bg-primary" : "bg-hairline"}`}
      >
        <span
          className="absolute left-0.5 top-0.5 size-4 rounded-full bg-surface shadow-sm transition-transform duration-200 ease-[var(--ease-out)]"
          style={{ transform: voice ? "translateX(16px)" : "none" }}
        />
      </button>
      {!compact && <span className="w-6 text-ink">{voice ? "on" : "off"}</span>}
    </label>
  );
}

/** How many demo clinics Lana calls. Shared by the footer and the welcome card. */
export function ClinicsPicker({ clinics, setClinics }: { clinics: 2 | 3; setClinics: (n: 2 | 3) => void }) {
  const labelId = useId();
  return (
    <div className="inline-flex items-center gap-2">
      <span id={labelId}>Clinics</span>
      <div role="radiogroup" aria-labelledby={labelId} className="inline-flex rounded-full border border-hairline p-0.5">
        {([2, 3] as const).map((n) => (
          <button
            key={n}
            type="button"
            role="radio"
            aria-checked={clinics === n}
            onClick={() => setClinics(n)}
            className={`h-6 w-8 rounded-full font-mono text-[12px] transition-colors duration-150 ${
              clinics === n ? "bg-primary-soft text-primary" : "text-muted hover:text-ink"
            }`}
          >
            {n}
          </button>
        ))}
      </div>
    </div>
  );
}
