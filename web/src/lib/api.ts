// REST calls to the backend. The web app holds no keys; the server does all AI work.

// VITE_API_URL is baked in at build time. Localhost is only a dev fallback: a production build
// without it can't reach the server, and says so.
const DEV_API_URL = import.meta.env.DEV ? "http://localhost:8000" : "";
export const API_URL = ((import.meta.env.VITE_API_URL as string | undefined) || DEV_API_URL).replace(/\/+$/, "");

const UNREACHABLE = "Can't reach Lana's server. Try again in a moment.";
const NOT_CONNECTED = "This site isn't connected to Lana's server yet.";

export type Mode = "watch" | "patient" | "clinic";

// ---------- access pass ----------
// The server won't start a case without a pass (POST /access trades a code for one). It is kept in
// memory and in sessionStorage: a refresh in the same tab keeps it, a closed tab loses it.

const PASS_KEY = "lana.pass";

interface StoredPass {
  pass: string;
  /** Epoch seconds. */
  expiresAt: number;
}

function readStoredPass(): StoredPass | null {
  try {
    const raw = sessionStorage.getItem(PASS_KEY);
    const p = raw ? (JSON.parse(raw) as Partial<StoredPass>) : null;
    return p && typeof p.pass === "string" && typeof p.expiresAt === "number"
      ? { pass: p.pass, expiresAt: p.expiresAt }
      : null;
  } catch {
    return null;
  }
}

let stored: StoredPass | null = readStoredPass();
/** The pass, if there is one that hasn't expired. */
export function getPass(): string | null {
  return stored && stored.expiresAt * 1000 > Date.now() ? stored.pass : null;
}

export function hasValidPass(): boolean {
  return getPass() !== null;
}

export function clearPass(): void {
  stored = null;
  try {
    sessionStorage.removeItem(PASS_KEY);
  } catch {
    // storage blocked: memory only
  }
}

function savePass(next: StoredPass): void {
  stored = next;
  try {
    sessionStorage.setItem(PASS_KEY, JSON.stringify(next));
  } catch {
    // storage blocked: memory only
  }
}

/** The server turned the pass down (401): it has expired or its code was removed. */
export class AccessError extends Error {}

/** POST /access. Keeps the pass on success; throws with the server's message ("That code didn't work.", 429). */
export async function redeemCode(code: string): Promise<void> {
  if (!API_URL) throw new Error(NOT_CONNECTED);
  let res: Response;
  try {
    res = await fetch(`${API_URL}/access`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  } catch {
    throw new Error(UNREACHABLE);
  }
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) throw new Error(serverMessage(body) ?? `The server answered ${res.status}. Try again.`);
  const b = body as { pass?: unknown; expires_at?: unknown } | null;
  if (typeof b?.pass !== "string" || typeof b.expires_at !== "number") {
    throw new Error("The server didn't return a pass. Try again.");
  }
  savePass({ pass: b.pass, expiresAt: b.expires_at });
}

/** Whether the server asks for a code at all. Null if it can't be reached; callers then ask for one. */
export async function fetchAccessRequired(): Promise<boolean | null> {
  if (!API_URL) return null;
  try {
    const res = await fetch(`${API_URL}/config/access`);
    if (!res.ok) return null;
    const body = (await res.json()) as { required?: unknown };
    return typeof body.required === "boolean" ? body.required : null;
  } catch {
    return null;
  }
}

/** POST /cases. Throws with the server's own message (429 limit, 501 not built, 422 bad body), or AccessError on 401. */
export async function createCase(mode: Mode, voice: boolean, clinics: 2 | 3): Promise<string> {
  if (!API_URL) throw new Error(NOT_CONNECTED);
  let res: Response;
  const pass = getPass();
  try {
    res = await fetch(`${API_URL}/cases`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(pass ? { Authorization: `Bearer ${pass}` } : {}) },
      body: JSON.stringify({ mode, voice, clinics }),
    });
  } catch {
    throw new Error(UNREACHABLE);
  }
  const body: unknown = await res.json().catch(() => null);
  if (res.status === 401) {
    clearPass();
    throw new AccessError(serverMessage(body) ?? "Enter an access code to continue.");
  }
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
