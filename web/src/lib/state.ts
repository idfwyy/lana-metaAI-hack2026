// View state for a run. The player applies server events here in seq order.

import { clinicById, parseCallOrder } from "./clinics";
import type { CaseStatus, Concession, Offer, Quote, ServerEvent, SourcedAnswer } from "./types";

export type CallPhase = "waiting" | "dialing" | "live" | "on_hold" | "negotiating" | "done";

export interface CallView {
  clinicId: string;
  callId: string | null;
  phase: CallPhase;
  /** ms timestamps (performance clock of the browser) for the timers. */
  startedAt: number | null;
  endedAt: number | null;
  holdSince: number | null;
}

export interface Line {
  seq: number;
  callId: string | null;
  speaker: "agent" | "clinic";
  words: string[];
  revealed: number;
  tSeconds: number | null;
}

export interface LogEntry {
  seq: number;
  text: string;
  /** Seconds since the run's first event. */
  offset: number;
}

export interface PriceDrop {
  from: number;
  to: number;
  concession: Concession | null;
}

export interface QuoteView {
  clinicId: string;
  quote: Quote | null;
  price: number | null;
  /** Earlier prices, oldest first, each with what brought it down. */
  drops: PriceDrop[];
  /** Where the current price came from: the clinic's words, or the last concession. */
  evidence: SourcedAnswer<unknown> | null;
}

/** How the callback ended: booked at a slot, or saved without booking. */
export type Outcome = { kind: "booked"; clinicId: string; slot: string; totalSaved: number } | { kind: "saved" };

export interface RunState {
  caseStatus: CaseStatus | null;
  order: string[];
  calls: Record<string, CallView>;
  callClinic: Record<string, string>;
  currentCallId: string | null;
  lines: Line[];
  log: LogEntry[];
  quotes: Record<string, QuoteView>;
  saved: number;
  offers: Offer[] | null;
  outcome: Outcome | null;
  error: { message: string; code?: string } | null;
  firstAt: number | null;
}

export const initialRun: RunState = {
  caseStatus: null,
  order: [],
  calls: {},
  callClinic: {},
  currentCallId: null,
  lines: [],
  log: [],
  quotes: {},
  saved: 0,
  offers: null,
  outcome: null,
  error: null,
  firstAt: null,
};

export type RunAction =
  | { kind: "event"; event: ServerEvent; now: number }
  | { kind: "reveal"; seq: number; count: number }
  | { kind: "reset" };

function addToOrder(order: string[], ids: string[]): string[] {
  const next = [...order];
  for (const id of ids) if (!next.includes(id)) next.push(id);
  return next;
}

function waitingCall(clinicId: string): CallView {
  return { clinicId, callId: null, phase: "waiting", startedAt: null, endedAt: null, holdSince: null };
}

export function runReducer(state: RunState, action: RunAction): RunState {
  if (action.kind === "reset") return initialRun;
  if (action.kind === "reveal") {
    const lines = state.lines.map((l) =>
      l.seq === action.seq && l.revealed !== action.count ? { ...l, revealed: action.count } : l,
    );
    return { ...state, lines };
  }

  const e = action.event;
  const at = e.at ? Date.parse(e.at) : NaN;
  const firstAt = state.firstAt ?? (Number.isNaN(at) ? null : at);
  const s: RunState = { ...state, firstAt };

  switch (e.type) {
    case "case_status":
      return {
        ...s,
        caseStatus: e.status,
        outcome: e.status === "saved" && !s.outcome ? { kind: "saved" } : s.outcome,
      };

    case "booked":
      return {
        ...s,
        outcome: { kind: "booked", clinicId: e.clinic_id, slot: e.slot, totalSaved: e.total_saved },
      };

    case "log_line": {
      const offset = firstAt !== null && !Number.isNaN(at) ? Math.max(0, (at - firstAt) / 1000) : 0;
      const log = [...s.log, { seq: e.seq ?? 0, text: e.text, offset }];
      const ids = parseCallOrder(e.text);
      if (ids.length === 0) return { ...s, log };
      const order = addToOrder(s.order, ids);
      const calls = { ...s.calls };
      for (const id of order) calls[id] ??= waitingCall(id);
      return { ...s, log, order, calls };
    }

    case "call_status": {
      const prev = s.calls[e.clinic_id] ?? waitingCall(e.clinic_id);
      const phase: CallPhase = e.status === "ended" ? "done" : e.status;
      const call: CallView = {
        ...prev,
        callId: e.call_id,
        phase,
        startedAt: prev.callId === e.call_id && prev.startedAt !== null ? prev.startedAt : action.now,
        endedAt: e.status === "ended" ? action.now : null,
        holdSince: e.status === "on_hold" ? action.now : null,
      };
      const takesFocus = e.status === "dialing" || e.status === "live" || e.status === "negotiating";
      return {
        ...s,
        order: addToOrder(s.order, [e.clinic_id]),
        calls: { ...s.calls, [e.clinic_id]: call },
        callClinic: { ...s.callClinic, [e.call_id]: e.clinic_id },
        currentCallId: takesFocus ? e.call_id : s.currentCallId,
      };
    }

    case "agent_said":
    case "transcript_final": {
      const speaker = e.type === "agent_said" ? "agent" : e.speaker;
      if (speaker === "patient") return s;
      const line: Line = {
        seq: e.seq ?? s.lines.length,
        callId: e.call_id,
        speaker,
        words: e.text.split(/\s+/).filter(Boolean),
        revealed: 0,
        tSeconds: e.type === "transcript_final" ? e.t_seconds : null,
      };
      return { ...s, lines: [...s.lines, line], currentCallId: e.call_id ?? s.currentCallId };
    }

    case "quote_updated": {
      const q = e.quote;
      const prev = s.quotes[q.clinic_id];
      const drops = [...(prev?.drops ?? [])];
      if (prev?.price != null && q.all_in_price != null && q.all_in_price < prev.price) {
        drops.push({ from: prev.price, to: q.all_in_price, concession: null });
      }
      const view: QuoteView = {
        clinicId: q.clinic_id,
        quote: q,
        price: q.all_in_price,
        drops,
        evidence: q.checklist.total_price ?? prev?.evidence ?? null,
      };
      return { ...s, quotes: { ...s.quotes, [q.clinic_id]: view } };
    }

    case "concession_won": {
      const prev = s.quotes[e.clinic_id] ?? {
        clinicId: e.clinic_id,
        quote: null,
        price: null,
        drops: [],
        evidence: null,
      };
      const drops = [...prev.drops];
      if (prev.price != null && e.new_all_in < prev.price) {
        drops.push({ from: prev.price, to: e.new_all_in, concession: e.concession });
      }
      const view: QuoteView = { ...prev, price: e.new_all_in, drops, evidence: e.concession.evidence };
      return { ...s, quotes: { ...s.quotes, [e.clinic_id]: view }, saved: s.saved + e.concession.amount };
    }

    case "offers_ready":
      return { ...s, offers: e.offers };

    case "error":
      return { ...s, error: { message: e.message, code: e.code } };

    default:
      return s;
  }
}

// ---------- derived ----------

/**
 * Lowest confirmed all-in price; after ranking, the rank 1 offer.
 * A price tie goes to the clinic Lana negotiated with: Python picks it as the best on
 * everything but price, so at the same price the ranker puts it first. Then higher rating.
 */
export function leaderId(state: RunState): string | null {
  if (state.offers?.length) return state.offers[0].clinic.id;
  const priced = Object.values(state.quotes).filter((q) => q.price != null);
  if (priced.length === 0) return null;
  const lowest = Math.min(...priced.map((q) => q.price!));
  const tied = priced.filter((q) => q.price === lowest);
  const negotiated = tied.find((q) => q.drops.some((d) => d.concession !== null));
  if (negotiated) return negotiated.clinicId;
  return tied.reduce((a, b) => (clinicById(b.clinicId).rating > clinicById(a.clinicId).rating ? b : a)).clinicId;
}

function short(clinicId: string): string {
  return clinicById(clinicId).name.split(" ")[0];
}

export function statusLine(state: RunState): string {
  if (state.outcome?.kind === "booked") return "Booked";
  if (state.outcome?.kind === "saved") return "Quote saved";
  if (state.offers && (state.caseStatus === "offers_ready" || state.caseStatus === "callback")) {
    return "Lana found your best offer";
  }
  const calls = Object.values(state.calls);
  const active = calls.find((c) => c.callId === state.currentCallId && c.phase !== "done" && c.phase !== "waiting");
  const held = calls.find((c) => c.phase === "on_hold");
  if (active?.phase === "negotiating" || (state.caseStatus === "negotiating" && active?.phase === "live")) {
    return `Lana is negotiating with ${short(active.clinicId)}...`;
  }
  if (active && held && held !== active) {
    return `${short(held.clinicId)} is on hold. Lana is calling ${short(active.clinicId)}...`;
  }
  if (active) return `Lana is calling ${short(active.clinicId)}...`;
  switch (state.caseStatus) {
    case null:
    case "new":
    case "intake":
      return "Lana is getting ready...";
    case "research":
      return "Lana is finding clinics...";
    case "calling":
    case "holding":
      return "Lana is dialing...";
    case "quotes_in":
    case "negotiating":
    case "offers_ready":
      return "Lana is comparing offers...";
    default:
      return "Lana is working...";
  }
}

export function isLive(state: RunState): boolean {
  return Object.values(state.calls).some((c) => c.phase === "live" || c.phase === "negotiating");
}
