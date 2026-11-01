import { motion } from "motion/react";
import { DemoFooter } from "../components/DemoFooter";
import { MicButton } from "../components/MicButton";
import { EASE_OUT } from "../lib/motion";

interface Props {
  busy: boolean;
  onStart: () => void;
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

export function Home({ busy, onStart, voice, setVoice, clinics, setClinics }: Props) {
  // TODO(Part 12a): make the mic listen (intake over the socket) instead of starting a Watch case.
  const onMic = onStart;

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
          <MicButton busy={busy} onStart={onMic} />
        </div>

        <motion.div {...leave} className="mt-4 flex flex-col items-center gap-3">
          <p className="max-w-[44ch] text-[15px] text-muted">
            Try: “Find me braces near Dallas under $5,000, evenings work best.”
          </p>
          <button
            type="button"
            onClick={onStart}
            disabled={busy}
            className="pressable rounded-md text-[14px] text-muted underline decoration-hairline underline-offset-4 hover:text-ink hover:decoration-muted disabled:opacity-60"
          >
            or just show me
          </button>
        </motion.div>
      </main>

      <motion.div {...leave}>
        <DemoFooter voice={voice} setVoice={setVoice} clinics={clinics} setClinics={setClinics} />
      </motion.div>
    </motion.div>
  );
}
