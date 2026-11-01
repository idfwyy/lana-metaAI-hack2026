import { useEffect, useRef, useState, type ReactNode } from "react";
import { motion } from "motion/react";
import { CallCard } from "../components/CallCard";
import { QuoteCard } from "../components/QuoteCard";
import { SavedCounter } from "../components/SavedCounter";
import { Transcript } from "../components/Transcript";
import { clinicById, shortName } from "../lib/clinics";
import { clock } from "../lib/format";
import { useMediaQuery, useNow } from "../lib/hooks";
import { EASE_OUT } from "../lib/motion";
import { isLive, leaderId, type RunState } from "../lib/state";

/** The page unfolds after the orb lands: Calls, Live call, Quotes, 60ms apart. */
function unfold(i: number) {
  return {
    initial: { opacity: 0, transform: "translateY(12px)" },
    animate: { opacity: 1, transform: "translateY(0px)" },
    transition: { duration: 0.24, ease: EASE_OUT, delay: 0.38 + i * 0.06 },
  };
}

type Tab = "call" | "quotes" | "log";

export function Working({ state }: { state: RunState }) {
  const wide = useMediaQuery("(min-width: 1024px)");
  const [tab, setTab] = useState<Tab>("call");
  const now = useNow(1000, isLive(state) || Object.values(state.calls).some((c) => c.phase === "on_hold"));

  const calls = <CallsList state={state} now={now} />;
  const live = <LiveCall state={state} now={now} />;
  const quotes = <QuotesList state={state} />;

  if (wide) {
    return (
      <motion.div
        exit={{ opacity: 0, transition: { duration: 0.18, ease: EASE_OUT } }}
        className="mx-auto grid h-[calc(100dvh-64px)] w-full max-w-[1440px] grid-cols-[280px_minmax(0,1fr)_320px] gap-6 px-8 py-6"
      >
        <motion.section {...unfold(0)} aria-label="Calls" className="flex min-h-0 flex-col">
          <ColumnTitle>Calls</ColumnTitle>
          <div className="scroll-quiet min-h-0 flex-1 overflow-y-auto">{calls}</div>
        </motion.section>
        <motion.section {...unfold(1)} aria-label="Live call" className="flex min-h-0 flex-col">
          {live}
        </motion.section>
        <motion.section {...unfold(2)} aria-label="Quotes" className="flex min-h-0 flex-col">
          <ColumnTitle>Quotes</ColumnTitle>
          <div className="scroll-quiet min-h-0 flex-1 overflow-y-auto pb-24">{quotes}</div>
        </motion.section>
      </motion.div>
    );
  }

  return (
    <motion.div
      exit={{ opacity: 0, transition: { duration: 0.18, ease: EASE_OUT } }}
      className="flex h-[calc(100dvh-64px)] w-full flex-col"
    >
      <motion.div {...unfold(0)} role="tablist" aria-label="Sections" className="flex gap-1 px-4 pt-3">
        {(
          [
            ["call", "Call"],
            ["quotes", "Quotes"],
            ["log", "Log"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`flex-1 rounded-full py-2 text-[14px] font-medium transition-colors duration-150 ${
              tab === id ? "bg-primary-soft text-primary" : "text-muted"
            }`}
          >
            {label}
          </button>
        ))}
      </motion.div>
      <motion.div {...unfold(1)} role="tabpanel" className="flex min-h-0 flex-1 flex-col px-4 pb-4 pt-4">
        {tab === "call" && live}
        {tab === "quotes" && <div className="scroll-quiet min-h-0 flex-1 overflow-y-auto pb-24">{quotes}</div>}
        {tab === "log" && (
          <div className="scroll-quiet min-h-0 flex-1 overflow-y-auto">
            {calls}
            <div className="mt-6">
              <ColumnTitle>Lana's log</ColumnTitle>
              <AgentLog state={state} />
            </div>
          </div>
        )}
      </motion.div>
    </motion.div>
  );
}

function ColumnTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-3 flex min-h-6 items-center gap-3">
      <h2 className="text-[14px] font-medium text-muted">{children}</h2>
      {aside}
    </div>
  );
}

function CallsList({ state, now }: { state: RunState; now: number }) {
  if (state.order.length === 0) return <p className="text-[14px] text-muted">Lana is picking clinics.</p>;
  return (
    <ul className="flex flex-col gap-3">
      {state.order.map((id, i) => (
        <CallCard key={id} call={state.calls[id]} now={now} index={i} />
      ))}
    </ul>
  );
}

function LiveCall({ state, now }: { state: RunState; now: number }) {
  const clinicId = state.currentCallId ? state.callClinic[state.currentCallId] : null;
  const call = clinicId ? state.calls[clinicId] : null;
  const onAir = call?.phase === "live" || call?.phase === "negotiating";
  const lines = state.lines.filter((l) => l.callId !== null && l.callId === state.currentCallId);
  const wide = useMediaQuery("(min-width: 1024px)");

  return (
    <div className="flex min-h-0 flex-1 flex-col rounded-[20px] border border-hairline bg-surface">
      <div className="flex items-center gap-3 border-b border-hairline px-5 py-4 sm:px-6">
        <h2 className="text-[14px] font-medium text-muted">Live call</h2>
        {clinicId && <span className="truncate text-[15px] font-medium text-ink">{shortName(clinicById(clinicId).name)}</span>}
        {call?.phase === "on_hold" && <span className="text-[13px] text-muted">on hold</span>}
        {call && call.startedAt !== null && call.phase !== "waiting" && (
          <span className="ml-auto flex items-center gap-2">
            {onAir && <span className="live-dot" aria-label="Recording" />}
            <span className="font-mono text-[13px] text-muted tnum">
              {clock(((call.endedAt ?? now) - call.startedAt) / 1000)}
            </span>
          </span>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col px-5 pt-5 sm:px-6">
        <Transcript lines={lines} empty="Lana will dial the first clinic in a moment." />
      </div>
      {wide && (
        <div className="border-t border-hairline px-5 py-3 sm:px-6">
          <AgentLog state={state} compact />
        </div>
      )}
    </div>
  );
}

function AgentLog({ state, compact }: { state: RunState; compact?: boolean }) {
  const box = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = box.current;
    if (el && compact) el.scrollTop = el.scrollHeight;
  }, [state.log.length, compact]);

  if (state.log.length === 0) return <p className="font-mono text-[12px] text-muted">Starting...</p>;
  return (
    <ol ref={box} aria-label="Lana's log" className={`scroll-quiet flex flex-col gap-1 ${compact ? "max-h-[96px] overflow-y-auto" : ""}`}>
      {state.log.map((entry) => (
        <motion.li
          key={entry.seq}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.2 }}
          className="grid grid-cols-[40px_minmax(0,1fr)] gap-2 font-mono text-[12px] leading-[1.6] text-muted"
        >
          <span className="tnum">{clock(entry.offset)}</span>
          <span>{entry.text}</span>
        </motion.li>
      ))}
    </ol>
  );
}

function QuotesList({ state }: { state: RunState }) {
  const best = leaderId(state);
  const done = state.offers !== null;
  const ids = state.order.filter((id) => state.quotes[id] || state.calls[id]?.phase !== "waiting");
  return (
    <div className="flex flex-col gap-3">
      <SavedCounter amount={state.saved} />
      {ids.length === 0 ? (
        <p className="pt-1 text-[14px] text-muted">Quotes show up here as clinics answer.</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {ids.map((id, i) => (
            <QuoteCard key={id} clinicId={id} view={state.quotes[id]} best={id === best} shareLayout={done && id === best} index={i} />
          ))}
        </ul>
      )}
    </div>
  );
}
