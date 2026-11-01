// REST calls to the backend. The web app holds no keys; the server does all AI work.

export const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";

export type Mode = "watch" | "patient" | "clinic";

/** POST /cases. Throws with the server's own message (429 limit, 501 not built, 422 bad body). */
export async function createCase(mode: Mode, voice: boolean, clinics: 2 | 3): Promise<string> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/cases`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, voice, clinics }),
    });
  } catch {
    throw new Error("Can't reach Lana's server. Check that the backend is running.");
  }
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) throw new Error(serverMessage(body) ?? `The server answered ${res.status}. Try again.`);
  const id = (body as { case_id?: unknown } | null)?.case_id;
  if (typeof id !== "string") throw new Error("The server didn't return a case. Try again.");
  return id;
}

function serverMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { message?: unknown; detail?: unknown };
  if (typeof b.message === "string") return b.message;
  if (typeof b.detail === "string") return b.detail;
  if (Array.isArray(b.detail) && typeof b.detail[0]?.msg === "string") return b.detail[0].msg;
  return null;
}
