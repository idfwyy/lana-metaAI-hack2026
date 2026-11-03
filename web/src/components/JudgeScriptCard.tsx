// Be the clinic: the judge's script. Parkway's price sheet in plain words, what to say for the
// question Lana just asked, and whose turn it is. The judge can go off script; Lana listens.
//
// The facts come from GET /config/judge-sheet (Parkway's sheet in backend/app/data/demo_clinics.json),
// with a copy here in case the server can't be reached. Only the judge sees them: Lana never does.

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Mic, Pause } from "lucide-react";
import { fetchJudgeSheet, type JudgeSheet } from "../lib/api";
import { clock, money } from "../lib/format";
import { useNow } from "../lib/hooks";
import { EASE_OUT } from "../lib/motion";
import type { JudgeView, Line } from "../lib/state";
import type { JudgePhase } from "../lib/types";
import type { ListenState } from "../lib/useCaseRun";
import { TypeBox } from "../screens/Home";
import { FlashCard } from "./FlashCard";
import { LiveCaptions } from "./LiveCaptions";

/** Used only when the server can't be reached. */
const FALLBACK_SHEET: JudgeSheet = {
  clinic_name: "Parkway Orthodontics",
  treatment: 4800,
  records_fee: 250,
  monthly: 180,
  down_payment: 1000,
  evening_openings: ["Tue 5:30 PM", "Thu 6:00 PM"],
  lowest_price: 3990,
  can_match_price: true,
  can_waive_records: true,
  pay_in_full_discount_pct: 5,
  can_add_retainers: true,
};

const DAYS: Record<string, string> = {
  Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday", Thu: "Thursday", Fri: "Friday", Sat: "Saturday", Sun: "Sunday",
};

/** "Thu 6:00 PM" -> "Thursday at 6 PM" */
function spokenOpening(slot: string): string {
  const [day, ...time] = slot.split(" ");
  return `${DAYS[day] ?? day} at ${time.join(" ").replace(":00", "")}`;
}

function quoteOf(s: JudgeSheet): [string, string][] {
  return [
    ["Treatment", money(s.treatment)],
    ["Records fee", `${money(s.records_fee)}, for x-rays and scans`],
    ["Monthly plan", `${money(s.monthly)} a month, ${money(s.down_payment)} down`],
    ["Evening openings", s.evening_openings.join(", ") || "None this week"],
  ];
}

function roomOf(s: JudgeSheet): string[] {
  return [
    `Lowest you can go: ${money(s.lowest_price)}`,
    ...(s.can_match_price ? ["You can match a competitor's price"] : []),
    ...(s.can_waive_records ? ["You can waive the records fee"] : []),
    ...(s.pay_in_full_discount_pct > 0 ? [`${s.pay_in_full_discount_pct}% off for paying in full`] : []),
    ...(s.can_add_retainers ? ["You can add a second set of retainers"] : []),
  ];
}

function suggestionsOf(s: JudgeSheet): Record<JudgePhase, string[]> {
  const [first, second] = s.evening_openings.map(spokenOpening);
  return {
    greeting: ["Parkway Orthodontics, how can I help?", "Hi, this is Parkway. What can I do for you?"],
    price: [
      `Our total is ${money(s.treatment)}, plus a ${money(s.records_fee)} records fee.`,
      `Braces are ${money(s.treatment)} for the full treatment.`,
    ],
    fees: [
      `Yes, the records fee is ${money(s.records_fee)}, on top of the ${money(s.treatment)}.`,
      `There's a ${money(s.records_fee)} fee for x-rays and scans.`,
    ],
    monthly: [
      `It's ${money(s.monthly)} a month with ${money(s.down_payment)} down.`,
      `We do ${money(s.down_payment)} down, then ${money(s.monthly)} a month.`,
    ],
    opening: first
      ? [second ? `We have ${first} or ${second}.` : `We have ${first}.`, `Our next evening opening is ${first}.`]
      : ["We don't have evening openings this week."],
    hold: ["Sure, I'll hold.", "No problem."],
    negotiation: [
      "Let me check with my manager... yes, we can match that.",
      "Sorry, we can't go that low.",
      "Which clinic was that?",
    ],
    wrap: ["Thanks, bye!", "Have a great day."],
  };
}

/** The server's sheet once it loads; the copy above until then (or if it never does). */
function useJudgeSheet(): JudgeSheet {
  const [sheet, setSheet] = useState(FALLBACK_SHEET);
  useEffect(() => {
    let live = true;
    void fetchJudgeSheet().then((s) => {
      if (live && s) setSheet(s);
    });
    return () => {
      live = false;
    };
  }, []);
  return sheet;
}

interface Props {
  /** The briefing on Home: just the price sheet, before the call. */
  compact?: boolean;
  judge?: JudgeView | null;
  /** Lana's latest line on the judge's call. */
  lana?: Line | null;
  listen?: ListenState;
  micLevel?: () => number;
  onSay?: (text: string) => void;
}

type Turn = "waiting" | "yours" | "lana" | "thinking" | "hold" | "ended";

function turnOf(judge: JudgeView | null, listen: ListenState | undefined): Turn {
  if (!judge) return "waiting";
  if (judge.onHold) return "hold";
  if (judge.phase === "wrap") return "ended";
  if (listen?.lanaSpeaking) return "lana";
  return judge.yourTurn ? "yours" : "thinking";
}

export function JudgeScriptCard({ compact = false, judge = null, lana = null, listen, micLevel, onSay }: Props) {
  const sheet = useJudgeSheet();
  const turn = turnOf(judge, listen);
  const phase: JudgePhase = judge?.phase ?? "greeting";
  const fallback = !!listen?.fallback;
  // Taps only make sense while Lana can still hear the judge.
  const canAnswer = !compact && turn !== "hold" && turn !== "ended";

  return (
    <section
      aria-label="Your script"
      className="w-full max-w-[460px] rounded-[20px] border border-hairline bg-surface p-5 text-left shadow-soft"
    >
      <h2 className="font-serif text-[26px] font-normal leading-[1.1] tracking-[-0.01em] text-ink">
        You're {sheet.clinic_name}
      </h2>
      {!compact && <TurnState turn={turn} judge={judge} micLevel={micLevel} />}

      <dl className="mt-4 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-[14px]">
        {quoteOf(sheet).map(([term, value]) => (
          <div key={term} className="contents">
            <dt className="text-muted">{term}</dt>
            <dd className="text-ink tnum">{value}</dd>
          </div>
        ))}
      </dl>
      <ul className="mt-3 flex flex-col gap-1 border-t border-hairline pt-3 text-[14px] text-ink">
        {roomOf(sheet).map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>

      {!compact && (
        <div className="mt-4 border-t border-hairline pt-3">
          <FlashCard
            bare
            label={turn === "ended" ? "TO SIGN OFF" : "YOU COULD SAY"}
            phrases={suggestionsOf(sheet)[phase]}
            onPick={fallback && canAnswer && onSay ? onSay : undefined}
          />
        </div>
      )}
      <p className="mt-3 text-[13px] text-muted">Say anything you like. Lana listens.</p>

      {!compact && listen && (
        <div className="mt-4 border-t border-hairline pt-4">
          <LiveCaptions
            lana={lana}
            partial={listen.partial}
            heard={listen.heard}
            thinking={turn === "thinking"}
            youLabel="YOU (Parkway)"
          />
        </div>
      )}
      {!compact && fallback && canAnswer && onSay && (
        <div className="mt-3">
          <TypeBox onSay={onSay} placeholder="Type your reply" />
        </div>
      )}
    </section>
  );
}

/** Whose turn it is, in one line. "Your turn" carries a ring that follows the judge's voice. */
function TurnState({ turn, judge, micLevel }: { turn: Turn; judge: JudgeView | null; micLevel?: () => number }) {
  const now = useNow(1000, turn === "hold");
  const text: Record<Turn, string> = {
    waiting: "Lana is about to call you",
    yours: "Your turn",
    lana: "Lana is talking. You can cut in.",
    thinking: "Lana is thinking",
    hold: "You're on hold. Listen to Lana call Sunrise.",
    ended: "Call ended",
  };

  return (
    <div className="mt-3 flex min-h-9 items-center gap-3" aria-live="polite">
      {turn === "yours" ? (
        <LevelDot level={micLevel} />
      ) : turn === "hold" ? (
        <span className="grid size-9 shrink-0 place-items-center rounded-full bg-paper text-muted" aria-hidden>
          <Pause size={15} strokeWidth={1.75} />
        </span>
      ) : (
        <span className="grid size-9 shrink-0 place-items-center rounded-full bg-paper text-muted" aria-hidden>
          <Mic size={15} strokeWidth={1.75} />
        </span>
      )}
      <AnimatePresence mode="wait" initial={false}>
        <motion.p
          key={turn}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14, ease: EASE_OUT }}
          className={`text-[15px] ${turn === "yours" ? "font-medium text-primary" : "text-muted"}`}
        >
          {text[turn]}
          {turn === "hold" && judge?.holdSince != null && (
            <span className="ml-2 font-mono text-[13px] tnum">{clock((now - judge.holdSince) / 1000)}</span>
          )}
        </motion.p>
      </AnimatePresence>
    </div>
  );
}

/** The mic disc with a soft ring that grows with the judge's voice; written per frame, no re-render. */
function LevelDot({ level }: { level?: () => number }) {
  const ring = useRef<HTMLSpanElement>(null);
  const reduceMotion = useReducedMotion();

  useEffect(() => {
    const el = ring.current;
    if (!el || !level) return;
    let frame = 0;
    const tick = () => {
      const v = level();
      if (reduceMotion) el.style.opacity = String(0.35 + v * 0.65);
      else el.style.transform = `scale(${1 + v * 0.5})`;
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [level, reduceMotion]);

  return (
    <span className="relative grid size-9 shrink-0 place-items-center" aria-hidden>
      <span ref={ring} className="absolute inset-0 rounded-full bg-primary-soft will-change-transform" />
      <span className="relative grid size-7 place-items-center rounded-full bg-primary text-white">
        <Mic size={14} strokeWidth={1.75} />
      </span>
    </span>
  );
}
