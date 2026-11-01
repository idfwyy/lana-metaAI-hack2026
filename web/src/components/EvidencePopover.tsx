import { useEffect, useRef } from "react";
import { motion } from "motion/react";
import { clock, money } from "../lib/format";
import { EASE_OUT } from "../lib/motion";
import type { PriceDrop } from "../lib/state";
import type { SourcedAnswer } from "../lib/types";

interface Props {
  clinicName: string;
  quoted: SourcedAnswer<unknown> | null;
  drops: PriceDrop[];
  onClose: () => void;
}

/** The proof behind a price: the clinic's exact words and when in the call they said them. */
export function EvidencePopover({ clinicName, quoted, drops, onClose }: Props) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // Next tick, so the click that opened the popover doesn't close it.
    const id = setTimeout(() => document.addEventListener("pointerdown", onDown));
    document.addEventListener("keydown", onKey);
    return () => {
      clearTimeout(id);
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const concessions = drops.flatMap((d) => (d.concession ? [d.concession] : []));

  return (
    <motion.div
      ref={box}
      role="dialog"
      aria-label={`Where ${clinicName}'s price comes from`}
      initial={{ opacity: 0, scale: 0.96 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.98, transition: { duration: 0.12, ease: EASE_OUT } }}
      transition={{ duration: 0.16, ease: EASE_OUT }}
      style={{ transformOrigin: "top left" }}
      className="absolute left-0 top-full z-20 mt-2 w-[min(300px,calc(100vw-48px))] rounded-[16px] border border-hairline bg-surface p-4 text-left shadow-soft"
    >
      {quoted ? (
        <Said who={clinicName} words={quoted.evidence} at={quoted.t_seconds} />
      ) : (
        <p className="text-[13px] text-muted">{clinicName} hasn't said a price yet.</p>
      )}
      {concessions.map((c, i) => (
        <div key={i} className="mt-4 border-t border-hairline pt-4">
          <p className="flex items-baseline justify-between gap-3 text-[13px]">
            <span className="font-medium text-ink">{c.label}</span>
            {c.amount > 0 && <span className="font-mono text-primary tnum">−{money(c.amount)}</span>}
          </p>
          <Said who={clinicName} words={c.evidence.evidence} at={c.evidence.t_seconds} />
        </div>
      ))}
    </motion.div>
  );
}

function Said({ who, words, at }: { who: string; words: string; at: number }) {
  return (
    <figure className="mt-1">
      <blockquote className="text-[15px] leading-[1.5] text-ink">“{words}”</blockquote>
      <figcaption className="mt-1.5 text-[12px] text-muted">
        {who}, <span className="font-mono tnum">at {clock(at)}</span> in the call
      </figcaption>
    </figure>
  );
}
