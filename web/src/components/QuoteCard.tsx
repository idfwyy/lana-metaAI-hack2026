import { useCallback, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { clinicById } from "../lib/clinics";
import { money } from "../lib/format";
import { EASE_OUT, rise } from "../lib/motion";
import type { QuoteView } from "../lib/state";
import { AnimatedNumber } from "./AnimatedNumber";
import { EvidencePopover } from "./EvidencePopover";

interface Props {
  clinicId: string;
  view: QuoteView | undefined;
  best: boolean;
  /** Set when the run is over: the price then travels to the Done screen. */
  shareLayout: boolean;
  index: number;
}

export function QuoteCard({ clinicId, view, best, shareLayout, index }: Props) {
  const clinic = clinicById(clinicId);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  const price = view?.price ?? null;
  const lastDrop = view?.drops[view.drops.length - 1];
  const details = detailLine(view);

  return (
    <motion.li {...rise(index * 0.04)} className="rounded-[16px] border border-hairline bg-surface p-4">
      <div className="flex items-start justify-between gap-3">
        <motion.p layoutId={shareLayout ? `name-${clinicId}` : undefined} className="min-w-0 truncate font-medium">
          {clinic.name}
        </motion.p>
        {best && price !== null && (
          <span className="shrink-0 rounded-full bg-primary-soft px-2 py-[3px] text-[11px] font-semibold tracking-[0.06em] text-primary">
            BEST
          </span>
        )}
      </div>

      <div className="relative mt-3">
        {price === null ? (
          <p className="text-[15px] text-muted">Not confirmed</p>
        ) : (
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            aria-haspopup="dialog"
            title="See what the clinic said"
            className="pressable -mx-1 flex flex-wrap items-baseline gap-x-2.5 gap-y-1 rounded-lg px-1 text-left"
          >
            <motion.span
              layoutId={shareLayout ? `price-${clinicId}` : undefined}
              className="font-mono text-[24px] font-medium leading-none text-ink"
            >
              <AnimatedNumber value={price} duration={0.5} />
            </motion.span>
            {lastDrop && <Struck key={view!.drops.length} amount={lastDrop.from} />}
            <span className="text-[12px] text-muted underline decoration-hairline underline-offset-4">all in</span>
          </button>
        )}
        <AnimatePresence>
          {open && view && (
            <EvidencePopover
              clinicName={clinic.name}
              quoted={view.quote?.checklist.total_price ?? null}
              drops={view.drops}
              onClose={close}
            />
          )}
        </AnimatePresence>
      </div>

      {details && <p className="mt-2 text-[13px] text-muted">{details}</p>}
    </motion.li>
  );
}

/** The old price, with a line drawn across it from the left. */
function Struck({ amount }: { amount: number }) {
  return (
    <span className="relative font-mono text-[14px] text-muted tnum">
      {money(amount)}
      <motion.span
        aria-hidden
        className="absolute inset-x-0 top-1/2 h-[1.5px] origin-left bg-muted"
        initial={{ transform: "scaleX(0)" }}
        animate={{ transform: "scaleX(1)" }}
        transition={{ duration: 0.22, ease: EASE_OUT }}
      />
      <span className="sr-only"> before</span>
    </span>
  );
}

function detailLine(view: QuoteView | undefined): string | null {
  const c = view?.quote?.checklist;
  if (!c) return null;
  const parts: string[] = [];
  if (c.monthly_payment) parts.push(`${money(c.monthly_payment.value)}/mo`);
  if (c.records_fee) parts.push(c.records_fee.value === 0 ? "No records fee" : `Records ${money(c.records_fee.value)}`);
  if (c.evening_hours) parts.push(c.evening_hours.value ? "Evening hours" : "No evenings");
  return parts.length ? parts.join(" · ") : null;
}
