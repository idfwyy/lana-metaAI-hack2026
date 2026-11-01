export function money(n: number): string {
  return `$${Math.round(n).toLocaleString("en-US")}`;
}

/** 72.4 -> "1:12" */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** "records_fee" -> "Records fee" */
export function humanize(field: string): string {
  const t = field.replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}
