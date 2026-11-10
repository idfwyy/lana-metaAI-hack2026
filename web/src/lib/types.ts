// Mirrors backend/app/schemas (events.py and the shapes it carries), voice/audio_bus.py and
// the patient session's messages in main.py.

export type CaseStatus =
  | "new"
  | "intake"
  | "plan_check"
  | "research"
  | "calling"
  | "holding"
  | "quotes_in"
  | "negotiating"
  | "offers_ready"
  | "callback"
  | "booked"
  | "saved"
  | "error";

export type CallStatus = "dialing" | "live" | "on_hold" | "negotiating" | "ended";
export type Speaker = "agent" | "clinic" | "patient";

export interface SourcedAnswer<T> {
  value: T;
  speaker: Speaker;
  evidence: string;
  call_id: string;
  t_seconds: number;
}

export interface ClinicChecklist {
  total_price: SourcedAnswer<number> | null;
  records_fee: SourcedAnswer<number> | null;
  consult_fee: SourcedAnswer<number> | null;
  includes: SourcedAnswer<string[]> | null;
  monthly_payment: SourcedAnswer<number> | null;
  down_payment: SourcedAnswer<number> | null;
  payment_plan: SourcedAnswer<boolean> | null;
  next_slots: SourcedAnswer<string[]> | null;
  evening_hours: SourcedAnswer<boolean> | null;
  needs_exam_first: SourcedAnswer<boolean> | null;
  /** The one question Lana asked from the patient's notes, and the clinic's answer. */
  note_question?: string | null;
  note_answer?: SourcedAnswer<string> | null;
}

export interface Quote {
  clinic_id: string;
  checklist: ClinicChecklist;
  all_in_price: number | null;
  missing_fields: string[];
  is_solid: boolean;
}

export interface Concession {
  label: string;
  amount: number;
  evidence: SourcedAnswer<number | boolean | string>;
}

export interface NegotiationResult {
  clinic_id: string;
  first_all_in: number;
  final_all_in: number;
  concessions: Concession[];
  refused: string[];
  total_saved: number;
}

export interface Clinic {
  id: string;
  name: string;
  distance_mi: number;
  rating: number;
  review_count: number;
  hours: string;
  review_summary?: string;
  phone?: string;
  is_demo?: boolean;
}

export interface Offer {
  rank: number;
  clinic: Clinic;
  quote: Quote;
  negotiation: NegotiationResult | null;
  score: number;
  tradeoffs: string[];
  unknowns: string[];
  /** Dollars over the patient's budget; 0 when within it. Ranked after every offer within it. */
  over_budget_by: number;
}

interface Base {
  case_id: string;
  seq?: number;
  at?: string;
}

export interface CaseStatusEvent extends Base {
  type: "case_status";
  status: CaseStatus;
  previous: CaseStatus | null;
}
export interface AgentSaid extends Base {
  type: "agent_said";
  text: string;
  call_id: string | null;
}
export interface UserSaid extends Base {
  type: "user_said";
  text: string;
  /** Watch mode's demo patient: spoken aloud, so it plays as a line with its own audio. */
  voiced?: boolean;
}
export interface LogLine extends Base {
  type: "log_line";
  text: string;
}
export interface CallStatusEvent extends Base {
  type: "call_status";
  call_id: string;
  clinic_id: string;
  status: CallStatus;
}
export interface TranscriptPartial extends Base {
  type: "transcript_partial";
  call_id: string | null;
  speaker: Speaker;
  text: string;
}
export interface TranscriptFinal extends Base {
  type: "transcript_final";
  call_id: string | null;
  speaker: Speaker;
  text: string;
  t_seconds: number;
  /** Be the clinic: the judge's own words. Shown at once, never voiced. */
  judge?: boolean;
}
export interface QuoteUpdated extends Base {
  type: "quote_updated";
  quote: Quote;
}
export interface ConcessionWon extends Base {
  type: "concession_won";
  clinic_id: string;
  concession: Concession;
  new_all_in: number;
}
export interface OffersReady extends Base {
  type: "offers_ready";
  offers: Offer[];
}
export interface CallbackStarted extends Base {
  type: "callback_started";
  summary: string;
}
export interface Booked extends Base {
  type: "booked";
  clinic_id: string;
  slot: string;
  total_saved: number;
}
/** Be the clinic: what Lana asked about, so the card can suggest lines. `wrap` is only a hint. */
export type JudgePhase = "greeting" | "price" | "fees" | "monthly" | "opening" | "hold" | "negotiation" | "wrap";
/** Be the clinic: the judge should speak now. */
export interface JudgeTurnStart extends Base {
  type: "judge_turn_start";
  clinic_id: string;
  phase: JudgePhase;
}
/** Be the clinic: the judge's call went on hold, or Lana came back. */
export interface JudgeHold extends Base {
  type: "judge_hold";
  clinic_id: string;
  on_hold: boolean;
}
/** Orchestrator errors carry step/recoverable; socket-level errors (main.py) carry code. */
export interface ErrorEvent extends Base {
  type: "error";
  message: string;
  step?: string | null;
  recoverable?: boolean;
  code?: "limit" | "not_found" | "bad_message" | "running" | "server_error" | string;
}

export type TimePref = "morning" | "afternoon" | "evening" | "weekend" | "any";

/** Mirrors backend/app/schemas/preferences.py. */
export interface Preferences {
  treatment: "braces" | "aligners" | "not sure";
  zip_code: string;
  max_distance_mi: number;
  budget_max: number;
  pay_in_full_ok: boolean;
  uses_hsa_fsa: boolean;
  prefers_monthly: boolean;
  has_xrays: boolean;
  has_plan: boolean;
  must_haves: string[];
  free_times: string[];
  /** When the patient wants appointments; "any" means they didn't say. */
  time_pref: TimePref;
  /** Short plain notes of anything else the patient said that matters. */
  notes: string[];
  /** A place outside DFW as the patient said it; the demo clinics are placed near it. */
  place?: string | null;
}

/** What the patient has said so far (agent/intake.py PreferencesDraft); missing keys are unsaid. */
export type PreferencesDraft = Partial<Preferences> & { city?: string };

/** The question Lana asked last (agent/intake.py TOPIC_FIELDS). */
export type IntakeTopic = "treatment" | "location" | "budget" | "pay" | "records" | "must_haves";

/** main.py PatientSession: what the patient is saying, before the turn ends. */
export interface UserPartial extends Base {
  type: "user_partial";
  text: string;
}
/** Speech to text can't be used; the browser switches to tap and type. */
export interface SttUnavailable extends Base {
  type: "stt_unavailable";
}
/** After each intake answer. `preferences` comes once intake is done. */
export interface IntakeUpdate extends Base {
  type: "intake";
  topic: IntakeTopic | null;
  done: boolean;
  draft: PreferencesDraft;
  preferences?: Preferences;
}
/** A spoken command the browser acts on itself: start over, step away while the calls run, or be the clinic. */
export interface CommandEvent extends Base {
  type: "command";
  name: "start_over" | "step_away" | "clinic" | string;
}

/** audio_bus: frames for one line, pointing back to it with line_seq. */
export interface AudioFrame extends Base {
  type: "audio";
  line_seq: number;
  speaker: Speaker;
  chunk_b64?: string;
  last: boolean;
  failed?: boolean;
  /** The patient talked over this line; the rest of its audio never came. */
  cut?: boolean;
  /** Voiced after sound came on (need_audio): replaces the line's earlier failed marker. */
  late?: boolean;
}

export type ServerEvent =
  | CaseStatusEvent
  | AgentSaid
  | UserSaid
  | LogLine
  | CallStatusEvent
  | TranscriptPartial
  | TranscriptFinal
  | QuoteUpdated
  | ConcessionWon
  | OffersReady
  | CallbackStarted
  | Booked
  | JudgeTurnStart
  | JudgeHold
  | ErrorEvent
  | UserPartial
  | SttUnavailable
  | IntakeUpdate
  | CommandEvent;

export type ServerMessage = ServerEvent | AudioFrame;

export type LineEvent = AgentSaid | TranscriptFinal | UserSaid;

export function isLine(m: ServerMessage): m is LineEvent {
  return (
    m.type === "agent_said" ||
    // The judge's own lines have no audio to wait for.
    (m.type === "transcript_final" && !m.judge && (m.speaker === "agent" || m.speaker === "clinic")) ||
    (m.type === "user_said" && m.voiced === true)
  );
}

export type ClientMessage =
  /** The access pass: the first message on the socket, when there is one. */
  | { type: "auth"; pass: string }
  | { type: "start" }
  | { type: "listen" }
  | { type: "stop" }
  | { type: "user_text"; text: string }
  | { type: "command"; name: "step_away" | "step_back" | string }
  /** Pick up Lana's callback (auto after 3 s of ringing). */
  | { type: "answer" }
  /** The patient started talking over Lana; her audio is already stopped here. */
  | { type: "barge_in" }
  /**
   * Sound came on in a case that started without it: voice these lines (the one on screen and
   * the buffered ones) and every line after `after`, the last seq received; then voice all lines.
   */
  | { type: "need_audio"; line_seqs: number[]; after: number };
