// The first thing a new visitor sees: what Lana is, and the three ways to try her.
// Each start button is the first user gesture, so it starts its mode in the same click
// (audio unlock and the mic prompt happen there) before the card closes.

import { useEffect, useId, useRef, type KeyboardEvent } from "react";
import { motion } from "motion/react";
import { X } from "lucide-react";
import { ClinicsPicker, SoundSwitch } from "./DemoFooter";
import { EASE_OUT, rise } from "../lib/motion";

interface Props {
  busy: boolean;
  onTalk: () => void;
  onWatch: () => void;
  onClinic: () => void;
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
  start: "talk" | "watch" | "clinic";
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

export function WelcomeCard({ busy, onTalk, onWatch, onClinic, onClose, voice, setVoice, clinics, setClinics }: Props) {
  const titleId = useId();
  const introId = useId();
  const card = useRef<HTMLDivElement>(null);
  const first = useRef<HTMLButtonElement>(null);
  const starts = { talk: onTalk, watch: onWatch, clinic: onClinic };

  // Focus moves in on open and back to where it was on close; the page behind stops scrolling.
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    first.current?.focus({ preventScroll: true });
    return () => {
      document.body.style.overflow = overflow;
      if (before?.isConnected) before.focus({ preventScroll: true });
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

  function begin(start: Option["start"]) {
    starts[start](); // first, so audio unlock and the mic prompt stay inside the click
    onClose();
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
                ref={o.recommended ? first : undefined}
                type="button"
                onClick={() => begin(o.start)}
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
          <button
            type="button"
            onClick={onClose}
            className="pressable rounded-md text-[14px] text-muted underline decoration-hairline underline-offset-4 hover:text-ink hover:decoration-muted"
          >
            Just explore
          </button>
        </div>
      </motion.div>
    </div>
  );
}
