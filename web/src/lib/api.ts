// REST calls to the backend. The web app holds no keys; the server does all AI work.

// VITE_API_URL is baked in at build time. Localhost is only a dev fallback: a production build
// without it can't reach the server, and says so.
const DEV_API_URL = import.meta.env.DEV ? "http://localhost:8000" : "";
export const API_URL = ((import.meta.env.VITE_API_URL as string | undefined) || DEV_API_URL).replace(/\/+$/, "");

const UNREACHABLE = "Can't reach Lana's server. Try again in a moment.";
const NOT_CONNECTED = "This site isn't connected to Lana's server yet.";

export type Mode = "watch" | "patient" | "clinic";

/** POST /cases. Throws with the server's own message (429 limit, 501 not built, 422 bad body). */
export async function createCase(mode: Mode, voice: boolean, clinics: 2 | 3): Promise<string> {
  if (!API_URL) throw new Error(NOT_CONNECTED);
  let res: Response;
  try {
    res = await fetch(`${API_URL}/cases`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, voice, clinics }),
    });
  } catch {
    throw new Error(UNREACHABLE);
  }
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) throw new Error(serverMessage(body) ?? `The server answered ${res.status}. Try again.`);
  const id = (body as { case_id?: unknown } | null)?.case_id;
  if (typeof id !== "string") throw new Error("The server didn't return a case. Try again.");
  return id;
}

/** Parkway's facts for the judge's script card (GET /config/judge-sheet). */
export interface JudgeSheet {
  clinic_name: string;
  treatment: number;
  records_fee: number;
  monthly: number;
  down_payment: number;
  evening_openings: string[];
  lowest_price: number;
  can_match_price: boolean;
  can_waive_records: boolean;
  pay_in_full_discount_pct: number;
  can_add_retainers: boolean;
}

let judgeSheet: Promise<JudgeSheet | null> | null = null;

/** The judge sheet, fetched once. Null if the server can't be reached; the card has its own copy then. */
export function fetchJudgeSheet(): Promise<JudgeSheet | null> {
  judgeSheet ??= (async () => {
    if (!API_URL) return null;
    try {
      const res = await fetch(`${API_URL}/config/judge-sheet`);
      if (!res.ok) return null;
      const body = (await res.json()) as JudgeSheet;
      return typeof body.treatment === "number" ? body : null;
    } catch {
      return null;
    }
  })();
  return judgeSheet;
}

function serverMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { message?: unknown; detail?: unknown };
  if (typeof b.message === "string") return b.message;
  if (typeof b.detail === "string") return b.detail;
  if (Array.isArray(b.detail) && typeof b.detail[0]?.msg === "string") return b.detail[0].msg;
  return null;
}
