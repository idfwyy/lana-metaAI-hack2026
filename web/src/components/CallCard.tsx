import { motion } from "motion/react";
import { Check, Pause, Star } from "lucide-react";
import { clinicById } from "../lib/clinics";
import { clock } from "../lib/format";
import { rise } from "../lib/motion";
import type { CallView } from "../lib/state";

interface Props {
  call: CallView;
  now: number;
  index: number;
}

export function CallCard({ call, now, index }: Props) {
  const clinic = clinicById(call.clinicId);
  const active = call.phase === "live" || call.phase === "negotiating";
  const meta = [
    Number.isFinite(clinic.distance_mi) ? `${clinic.distance_mi} mi` : null,
    Number.isFinite(clinic.rating) ? clinic.rating.toFixed(1) : null,
  ].filter(Boolean);

  return (
    <motion.li
      {...rise(index * 0.04)}
      className={`rounded-[16px] border p-4 transition-[background-color,box-shadow,border-color] duration-200 ${
        active ? "border-hairline bg-surface shadow-soft" : "border-hairline bg-surface/50"
      }`}
    >
      <p className={`truncate font-medium ${call.phase === "waiting" ? "text-muted" : "text-ink"}`}>{clinic.name}</p>
      {meta.length > 0 && (
        <p className="mt-0.5 flex items-center gap-1 text-[13px] text-muted">
          {meta[0]}
          {meta[1] && (
            <>
              <span aria-hidden>·</span>
              <Star size={11} strokeWidth={1.75} aria-hidden className="-mt-px" />
              {meta[1]}
            </>
          )}
        </p>
      )}
      <div className="mt-3 flex items-center gap-2 text-[13px]">
        <Status call={call} now={now} />
      </div>
    </motion.li>
  );
}

function Status({ call, now }: { call: CallView; now: number }) {
  switch (call.phase) {
    case "waiting":
      return <span className="text-muted">Waiting</span>;
    case "dialing":
      return <span className="text-muted">Dialing...</span>;
    case "live":
    case "negotiating":
      return (
        <>
          <span className="live-dot" aria-hidden />
          <span className="font-medium text-ink">{call.phase === "live" ? "Live" : "Negotiating"}</span>
          {call.startedAt !== null && (
            <span className="ml-auto font-mono text-[12px] text-muted tnum">{clock((now - call.startedAt) / 1000)}</span>
          )}
        </>
      );
    case "on_hold":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full bg-primary-soft px-2.5 py-1 text-primary">
          <Pause size={12} strokeWidth={2} aria-hidden />
          <span className="font-medium">On hold</span>
          {call.holdSince !== null && (
            <span className="font-mono text-[12px] tnum">{clock((now - call.holdSince) / 1000)}</span>
          )}
        </span>
      );
    case "done":
      return (
        <span className="inline-flex items-center gap-1.5 text-muted">
          <Check size={13} strokeWidth={2} aria-hidden />
          Done
        </span>
      );
  }
}
