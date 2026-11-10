// The first thing a new visitor sees: what Lana is, and the three ways to try her.
// Each start button is the first user gesture, so it starts its mode in the same click
// (audio unlock and the mic prompt happen there) before the card closes.
// Without a valid access pass, picking a mode swaps the options for an access code step inside
// the same card; a right code continues straight into the mode that was picked.

import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { AnimatePresence, motion } from "motion/react";
import { X } from "lucide-react";
import { ClinicsPicker, SoundSwitch } from "./DemoFooter";
import { EASE_OUT, rise } from "../lib/motion";

export type StartKind = "talk" | "watch" | "clinic";

/** A start that is waiting on an access code. `notice` explains why the code is asked for again. */
export interface Gate {
  start: StartKind;
  notice?: string;
}

interface Props {
  busy: boolean;
  /** An option was picked. The app starts it, or sets `gate` if it needs a code first. */
  onPick: (start: StartKind) => void;
  /** Set while the card shows the access code step. */
  gate: Gate | null;
  /** Trade the code for a pass and continue into the picked mode. Throws with the server's message. */
  onCode: (code: string) => Promise<void>;
  /** Back from the code step to the options. */
  onBack: () => void;
  onClose: () => void;
  voice: boolean;
  setVoice: (v: boolean) => void;
  clinics: 2 | 3;
  setClinics: (n: 2 | 3) => void;
}

interface Option {
  title: string;
  body: string;
  action: string;
  start: StartKind;
  recommended?: boolean;
}

const OPTIONS: Option[] = [
  {
    title: "Talk to Lana",
    body: "You're the patient. Tell her what you need out loud, and she calls the clinics for you, then calls you back.",
    action: "Start talking",
    start: "talk",
    recommended: true,
  },
  {
    title: "Just watch",
    body: "An AI patient and AI clinics. Sit back and watch Lana work from start to finish.",
    action: "Watch the demo",
    start: "watch",
  },
  {
    title: "Be the clinic",
    body: "You play the front desk at a dental clinic. A card shows your prices, and Lana calls you to negotiate.",
    action: "Be the clinic",
    start: "clinic",
  },
];

const FOCUSABLE = 'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

// "Need a code? Ask Rahul" goes here (set at build time). Only web and mail links are used.
const CONTACT_URL = ((import.meta.env.VITE_CONTACT_URL as string | undefined) ?? "").trim();
const CONTACT_LINK = /^(https?:|mailto:)/i.test(CONTACT_URL) ? CONTACT_URL : "";

// The two views swap quickly: the old one leaves before the new one arrives.
const viewMotion = {
  initial: { opacity: 0, transform: "translateY(6px)" },
  animate: { opacity: 1, transform: "translateY(0px)", transition: { duration: 0.2, ease: EASE_OUT } },
  exit: { opacity: 0, transform: "translateY(-4px)", transition: { duration: 0.12, ease: EASE_OUT } },
};

const quietLink =
  "pressable rounded-md text-[14px] text-muted underline decoration-hairline underline-offset-4 hover:text-ink hover:decoration-muted";

export function WelcomeCard({
  busy,
  onPick,
  gate,
  onCode,
  onBack,
  onClose,
  voice,
  setVoice,
  clinics,
  setClinics,
}: Props) {
  const titleId = useId();
  const introId = useId();
  const card = useRef<HTMLDivElement>(null);
  // Where focus was when the card opened, so it can go back there on close.
  const before = useRef(document.activeElement as HTMLElement | null);

  // The page behind stops scrolling while the card is open; focus goes back on close.
  useEffect(() => {
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const opener = before.current;
    return () => {
      document.body.style.overflow = overflow;
      if (opener?.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== "Tab" || !card.current) return;
    const items = [...card.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (items.length === 0) return;
    const head = items[0];
    const tail = items[items.length - 1];
    if (e.shiftKey && document.activeElement === head) {
      e.preventDefault();
      tail.focus();
    } else if (!e.shiftKey && document.activeElement === tail) {
      e.preventDefault();
      head.focus();
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto px-4 py-4" onKeyDown={onKeyDown}>
      <motion.div
        aria-hidden
        className="fixed inset-0 bg-ink/30 backdrop-blur-[2px]"
        onClick={onClose}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1, transition: { duration: 0.2, ease: EASE_OUT } }}
        exit={{ opacity: 0, transition: { duration: 0.16, ease: EASE_OUT } }}
      />
      <motion.div
        ref={card}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={introId}
        initial={{ opacity: 0, transform: "translateY(8px)" }}
        animate={{ opacity: 1, transform: "translateY(0px)", transition: { duration: 0.25, ease: EASE_OUT } }}
        exit={{ opacity: 0, transform: "translateY(4px)", transition: { duration: 0.16, ease: EASE_OUT } }}
        className="relative w-full max-w-[880px] rounded-[24px] border border-hairline bg-surface p-5 text-left shadow-soft sm:p-8"
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="pressable absolute right-3 top-3 grid size-9 place-items-center rounded-full text-muted hover:bg-paper hover:text-ink sm:right-4 sm:top-4"
        >
          <X size={18} aria-hidden />
        </button>

        <span className="inline-flex items-center gap-2 rounded-full border border-hairline bg-paper px-3 py-1 text-[13px] text-muted">
          <span className="size-1.5 rounded-full bg-primary" aria-hidden />
          Live demo
        </span>
        <h2 id={titleId} className="mt-4 font-serif text-[44px] font-normal leading-[1] tracking-[-0.02em] text-ink sm:text-[56px]">
          Meet Lana.
        </h2>

        <AnimatePresence mode="wait" initial={false}>
          {gate ? (
            <motion.div key="code" {...viewMotion}>
              <CodeStep introId={introId} notice={gate.notice} onCode={onCode} onBack={onBack} />
            </motion.div>
          ) : (
            <motion.div key="options" {...viewMotion}>
              <p id={introId} className="mt-3 max-w-[62ch] text-[16px] leading-[1.55] text-muted">
                Lana is an AI health companion that calls clinics, negotiates, and books for you. In this demo she calls demo
                clinics, never real offices. Pick how you want to try it.
              </p>

              <ul className="mt-6 grid gap-3 sm:grid-cols-3">
                {OPTIONS.map((o, i) => (
                  <motion.li
                    key={o.start}
                    {...rise(0.06 + i * 0.05, 6, 0.22)}
                    className={`flex flex-col rounded-[16px] border bg-paper p-4 ${
                      o.recommended ? "border-primary/30" : "border-hairline"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <h3 className="text-[16px] font-medium text-ink">{o.title}</h3>
                      {o.recommended && (
                        <span className="rounded-full bg-primary-soft px-2 py-0.5 text-[12px] text-primary">Recommended</span>
                      )}
                    </div>
                    <p className="mt-1.5 text-[14px] leading-[1.5] text-muted">{o.body}</p>
                    <p className="mt-2 text-[12px] text-muted">About 2 min</p>
                    <button
                      // Focus lands on the recommended option, on open and when coming back from the code step.
                      autoFocus={o.recommended}
                      type="button"
                      onClick={() => onPick(o.start)} // first, so audio unlock and the mic prompt stay inside the click
                      disabled={busy}
                      className={`pressable mt-4 w-full rounded-full px-4 py-2.5 text-[14px] font-medium disabled:opacity-60 sm:mt-auto ${
                        o.recommended
                          ? "bg-primary text-white hover:bg-primary-press"
                          : "border border-hairline bg-surface text-ink hover:border-primary/40 hover:bg-primary-soft"
                      }`}
                    >
                      {o.action}
                    </button>
                  </motion.li>
                ))}
              </ul>

              <div className="mt-6 grid gap-3 border-t border-hairline pt-5 text-[13px] text-muted sm:grid-cols-2 sm:gap-x-8">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <ClinicsPicker clinics={clinics} setClinics={setClinics} />
                  <span>2 (about 2 min) or 3 (about 3 min)</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <SoundSwitch voice={voice} setVoice={setVoice} />
                  <span>Off shows text only, useful if you can't play audio.</span>
                </div>
                <p className="sm:col-span-2">Use headphones if you can, and a quiet room. Lana hears you through your mic.</p>
              </div>

              <div className="mt-5 flex justify-center">
                <button type="button" onClick={onClose} className={quietLink}>
                  Just explore
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}

/** The access code step. Submitting is a click or Enter, so audio can unlock before the request goes out. */
function CodeStep({
  introId,
  notice,
  onCode,
  onBack,
}: {
  introId: string;
  notice?: string;
  onCode: Props["onCode"];
  onBack: () => void;
}) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const messageId = useId();
  const message = error ?? notice ?? null;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!code.trim() || checking) return;
    setChecking(true);
    setError(null);
    try {
      await onCode(code); // on success the app continues into the mode and the card goes away
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong. Try again.");
      setChecking(false);
    }
  }

  return (
    <div className="max-w-[420px]">
      <p id={introId} className="mt-3 text-[16px] leading-[1.55] text-muted">
        This demo is invite-only to keep usage costs down.
      </p>

      <form onSubmit={submit} className="mt-6">
        <div className="flex items-center gap-2">
          <input
            autoFocus
            value={code}
            onChange={(e) => {
              setCode(e.target.value);
              setError(null);
            }}
            type="text"
            autoComplete="off"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            placeholder="Access code"
            aria-label="Access code"
            aria-invalid={error !== null}
            aria-describedby={message ? messageId : undefined}
            className="min-w-0 flex-1 rounded-full border border-hairline bg-paper px-4 py-2.5 text-[16px] text-ink outline-none placeholder:text-muted focus:border-primary/50"
          />
          <button
            type="submit"
            disabled={!code.trim() || checking}
            className="pressable shrink-0 rounded-full bg-primary px-5 py-2.5 text-[15px] font-medium text-white hover:bg-primary-press disabled:opacity-60"
          >
            Continue
          </button>
        </div>
        {/* Always in the layout, so the card doesn't jump when an error arrives. */}
        <div className="min-h-[2.75rem] pt-2">
          <AnimatePresence initial={false} mode="wait">
            {message && (
              <motion.p
                key={message}
                id={messageId}
                role={error ? "alert" : undefined}
                {...rise(0, 3, 0.16)}
                exit={{ opacity: 0, transition: { duration: 0.1 } }}
                className={`pl-4 text-[14px] leading-[1.45] ${error ? "text-danger" : "text-muted"}`}
              >
                {message}
              </motion.p>
            )}
          </AnimatePresence>
        </div>
      </form>

      <div className="mt-1 flex flex-wrap items-center gap-x-5 gap-y-2">
        {CONTACT_LINK && (
          <a href={CONTACT_LINK} target="_blank" rel="noopener noreferrer" className={quietLink}>
            Need a code? Ask Rahul
          </a>
        )}
        <button type="button" onClick={onBack} className={quietLink}>
          Back
        </button>
      </div>
    </div>
  );
}
