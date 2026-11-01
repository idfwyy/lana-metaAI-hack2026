import { clock, money } from "../lib/format";
import type { NegotiationResult } from "../lib/types";

interface Props {
  negotiation: NegotiationResult;
  clinicName: string;
}

/** First all-in, each concession with its evidence, final all-in. */
export function SavingsBreakdown({ negotiation, clinicName }: Props) {
  return (
    <section aria-label="How Lana got there" className="rounded-[20px] border border-hairline bg-surface p-5 text-left sm:p-6">
      <h3 className="text-[14px] font-medium text-muted">How Lana got there</h3>
      <dl className="mt-2">
        <Row label="First all-in" amount={money(negotiation.first_all_in)} />
        {negotiation.concessions.map((c, i) => (
          <div key={i} className="border-t border-hairline py-3">
            <div className="flex items-baseline justify-between gap-4">
              <dt className="text-[15px] text-ink">{c.label}</dt>
              <dd className="font-mono text-[15px] text-primary tnum">{c.amount > 0 ? `−${money(c.amount)}` : "Included"}</dd>
            </div>
            <p className="mt-1 text-[13px] leading-[1.5] text-muted">
              {clinicName}: “{c.evidence.evidence}” <span className="whitespace-nowrap font-mono tnum">at {clock(c.evidence.t_seconds)}</span>
            </p>
          </div>
        ))}
        <Row label="Final all-in" amount={money(negotiation.final_all_in)} strong />
      </dl>
      {negotiation.refused.length > 0 && (
        <p className="mt-2 text-[13px] text-muted">Asked, but declined: {negotiation.refused.join(", ")}.</p>
      )}
    </section>
  );
}

function Row({ label, amount, strong }: { label: string; amount: string; strong?: boolean }) {
  return (
    <div className={`flex items-baseline justify-between gap-4 py-3 ${strong ? "border-t border-ink/70" : ""}`}>
      <dt className={`text-[15px] ${strong ? "font-medium text-ink" : "text-muted"}`}>{label}</dt>
      <dd className={`font-mono text-[15px] tnum ${strong ? "font-medium text-ink" : "text-muted"}`}>{amount}</dd>
    </div>
  );
}
