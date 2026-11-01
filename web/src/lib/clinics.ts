// Public info for the demo clinics (same fields the agent may see). No prices here.
// The server sends only clinic ids during calls; this gives the cards names early.

import type { Clinic } from "./types";

export const DEMO_CLINICS: Clinic[] = [
  { id: "parkway", name: "Parkway Orthodontics", distance_mi: 3.1, rating: 4.9, review_count: 412, hours: "Mon-Thu 8am-7pm, Fri 8am-3pm" },
  { id: "sunrise", name: "Sunrise Orthodontic Studio", distance_mi: 5.4, rating: 4.6, review_count: 238, hours: "Mon-Fri 7am-4pm" },
  { id: "lakeside", name: "Lakeside Family Orthodontics", distance_mi: 8.0, rating: 4.4, review_count: 156, hours: "Mon-Fri 9am-5pm, Sat 9am-1pm" },
];

export function clinicById(id: string): Clinic {
  return (
    DEMO_CLINICS.find((c) => c.id === id) ?? {
      id,
      name: id.charAt(0).toUpperCase() + id.slice(1),
      distance_mi: NaN,
      rating: NaN,
      review_count: 0,
      hours: "",
    }
  );
}

export function shortName(name: string): string {
  return name.split(" ")[0] ?? name;
}

/**
 * Reads call order from the researcher's log lines:
 * "Calling Parkway first: ..." and "Then Sunrise, then Lakeside".
 * Returns clinic ids in the order found, or [] if the line isn't one of those.
 */
export function parseCallOrder(text: string): string[] {
  let names: string[] = [];
  const first = /^Calling (\S+) first/.exec(text);
  if (first) names = [first[1]];
  else if (text.startsWith("Then ")) names = text.slice(5).split(/,\s*then\s+/i);
  return names
    .map((n) => DEMO_CLINICS.find((c) => shortName(c.name).toLowerCase() === n.trim().toLowerCase())?.id)
    .filter((id): id is string => id !== undefined);
}
