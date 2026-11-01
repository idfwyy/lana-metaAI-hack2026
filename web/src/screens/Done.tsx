import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { ChevronDown, Star } from "lucide-react";
import { AnimatedNumber } from "../components/AnimatedNumber";
import { SavingsBreakdown } from "../components/SavingsBreakdown";
import { clock, humanize, money } from "../lib/format";
import { EASE_DRAWER, EASE_IN_OUT, EASE_OUT } from "../lib/motion";
import type { Outcome } from "../lib/state";
import type { Offer } from "../lib/types";

/** "Tue, Nov 3 · 5:30 PM" in the demo clinics' time zone. */
function slotLabel(iso: string): string {
  const d = new Date(iso);
  const timeZone = "America/Chicago";
  const day = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone });
  const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone });
  return `${day} · ${time}`;
}

const settle = { duration: 0.36, ease: EASE_IN_OUT };

function enter(delay: number) {
  return {
    initial: { opacity: 0, transform: "translateY(8px)" },
    animate: { opacity: 1, transform: "translateY(0px)" },
    transition: { duration: 0.26, ease: EASE_OUT, delay },
  };
}

interface Props {
  offers: Offer[];
  /** How Lana's callback ended. */
  outcome: Outcome | null;
  onStartOver: () => void;
}

export function Done({ offers, outcome, onStartOver }: Props) {
  const [top, ...others] = offers;

  if (!top) {
    return (
      <motion.main {...enter(0.12)} className="mx-auto w-full max-w-[560px] px-4 py-16 text-center">
        <p className="text-[20px] font-medium">Lana didn't find an offer that fits.</p>
        <p className="mt-2 text-muted">None of the clinics confirmed a price within your limits. Start over to try again.</p>
        <StartOver onClick={onStartOver} />
      </motion.main>
    );
  }

  const { clinic, quote, negotiation } = top;
  const final = negotiation?.final_all_in ?? quote.all_in_price;
  const saved = negotiation?.total_saved ?? 0;
  const quoted = quote.checklist.total_price;

  return (
    <main className="mx-auto w-full max-w-[640px] px-4 pb-20 pt-10 text-center sm:pt-14">
      {outcome && <OutcomeLine outcome={outcome} />}
      <motion.h2 layoutId={`name-${clinic.id}`} transition={settle} className="text-[24px] font-semibold leading-[1.2] sm:text-[28px]">
        {clinic.name}
      </motion.h2>
      <motion.p {...enter(0.1)} className="mt-2 flex flex-wrap items-center justify-center gap-x-1.5 text-[14px] text-muted">
        {clinic.distance_mi} mi
        <span aria-hidden>·</span>
        <Star size={12} strokeWidth={1.75} aria-hidden />
        {clinic.rating.toFixed(1)} ({clinic.review_count} reviews)
        {clinic.hours && (
          <>
            <span aria-hidden>·</span>
            {clinic.hours}
          </>
        )}
      </motion.p>

      <motion.p {...enter(0.18)} className="mt-8 font-serif text-[clamp(64px,10vw,128px)] font-normal leading-none tracking-[-0.02em] text-primary">
        {saved > 0 ? (
          <>
            Saved <AnimatedNumber value={saved} from={0} duration={0.9} />
          </>
        ) : final !== null ? (
          <>Best offer {money(final)}</>
        ) : (
          <>Best fit</>
        )}
      </motion.p>

      {final !== null && (
        <p className="mt-5 flex items-baseline justify-center gap-3 text-[15px] text-muted">
          Final all-in
          <motion.span layoutId={`price-${clinic.id}`} transition={settle} className="font-mono text-[32px] font-medium leading-none text-ink tnum">
            {money(final)}
          </motion.span>
        </p>
      )}
      {top.over_budget_by > 0 && (
        <motion.p
          {...enter(0.22)}
          className="mt-3 inline-flex items-center gap-2 rounded-full bg-accent-soft px-3 py-1.5 text-[14px] font-medium text-ink"
        >
          <span className="size-1.5 rounded-full bg-accent" aria-hidden />
          Over your budget by <span className="font-mono tnum">{money(top.over_budget_by)}</span>
        </motion.p>
      )}

      <motion.div {...enter(0.28)} className="mt-10">
        {negotiation ? (
          <SavingsBreakdown negotiation={negotiation} clinicName={clinic.name} />
        ) : (
          quoted && (
            <section className="rounded-[20px] border border-hairline bg-surface p-5 text-left sm:p-6">
              <h3 className="text-[14px] font-medium text-muted">What {clinic.name} said</h3>
              <p className="mt-3 text-[15px] text-ink">“{quoted.evidence}”</p>
              <p className="mt-1.5 font-mono text-[12px] text-muted tnum">at {clock(quoted.t_seconds)} in the call</p>
            </section>
          )
        )}
        {top.unknowns.length > 0 && (
          <p className="mt-3 text-left text-[13px] text-muted">Not confirmed: {top.unknowns.map(humanize).join(", ")}.</p>
        )}
      </motion.div>

      {others.length > 0 && (
        <motion.div {...enter(0.34)} className="mt-6">
          <OtherOffers offers={others} />
        </motion.div>
      )}

      <motion.div {...enter(0.4)}>
        <StartOver onClick={onStartOver} />
      </motion.div>
    </main>
  );
}

function OutcomeLine({ outcome }: { outcome: Outcome }) {
  return (
    <motion.div {...enter(0)} className="mb-6 flex flex-col items-center gap-1">
      {outcome.kind === "booked" ? (
        <>
          <p className="inline-flex items-center gap-2 rounded-full bg-primary-soft px-3 py-1.5 text-[14px] font-medium text-primary">
            <span className="size-1.5 rounded-full bg-primary" aria-hidden />
            Booked (demo clinic)
          </p>
          <p className="mt-1 font-mono text-[15px] text-ink tnum">{slotLabel(outcome.slot)}</p>
        </>
      ) : (
        <p className="max-w-[40ch] text-[15px] text-muted">Saved, not booked. I'll hold this quote for you.</p>
      )}
    </motion.div>
  );
}

function OtherOffers({ offers }: { offers: Offer[] }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="text-left">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls="other-offers"
        className="pressable flex w-full items-center justify-between rounded-[16px] border border-hairline px-5 py-3.5 text-[15px] font-medium text-ink hover:bg-surface"
      >
        Other offers ({offers.length})
        <ChevronDown
          size={16}
          aria-hidden
          className="text-muted transition-transform duration-200"
          style={{ transform: open ? "rotate(180deg)" : "none" }}
        />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.ol
            id="other-offers"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.26, ease: EASE_DRAWER }}
            className="overflow-hidden"
          >
            {offers.map((o) => {
              const price = o.negotiation?.final_all_in ?? o.quote.all_in_price;
              return (
                <li key={o.clinic.id} className="grid grid-cols-[28px_minmax(0,1fr)_auto] gap-x-3 border-b border-hairline px-5 py-4 last:border-b-0">
                  <span className="font-mono text-[13px] text-muted tnum">{o.rank}</span>
                  <div className="min-w-0">
                    <p className="font-medium text-ink">{o.clinic.name}</p>
                    <p className="text-[13px] text-muted">
                      {o.clinic.distance_mi} mi · {o.clinic.rating.toFixed(1)}
                    </p>
                    {o.tradeoffs.length > 0 && (
                      <ul className="mt-2 flex flex-col gap-1 text-[14px] text-ink/80">
                        {o.tradeoffs.map((t, i) => (
                          <li key={i}>{t}</li>
                        ))}
                      </ul>
                    )}
                    {o.unknowns.length > 0 && (
                      <p className="mt-1.5 text-[13px] text-muted">Not confirmed: {o.unknowns.map(humanize).join(", ")}.</p>
                    )}
                    {o.over_budget_by > 0 && (
                      <p className="mt-1.5 text-[13px] text-ink">Over your budget by {money(o.over_budget_by)}</p>
                    )}
                  </div>
                  <span className="font-mono text-[15px] text-ink tnum">{price !== null ? money(price) : "Not confirmed"}</span>
                </li>
              );
            })}
          </motion.ol>
        )}
      </AnimatePresence>
    </section>
  );
}

function StartOver({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="pressable mt-10 rounded-full bg-primary px-6 py-3 text-[15px] font-medium text-white hover:bg-primary-press"
    >
      Start over
    </button>
  );
}
