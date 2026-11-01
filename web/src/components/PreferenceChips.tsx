import { motion } from "motion/react";
import { money } from "../lib/format";
import { rise } from "../lib/motion";
import type { Preferences, PreferencesDraft } from "../lib/types";

const TREATMENT: Record<Preferences["treatment"], string> = {
  braces: "Braces",
  aligners: "Clear aligners",
  "not sure": "Braces or aligners",
};

/** Short labels for what intake filled in; anything the patient didn't say reads as open. */
export function preferenceChips(prefs: Preferences, draft: PreferencesDraft): string[] {
  const pay = [
    prefs.prefers_monthly && "Monthly",
    prefs.pay_in_full_ok && "In full if cheaper",
    prefs.uses_hsa_fsa && "HSA or FSA",
  ].filter(Boolean);
  return [
    TREATMENT[prefs.treatment],
    `Near ${draft.city ?? prefs.zip_code}`,
    draft.budget_max != null ? `Under ${money(prefs.budget_max)}` : "No budget cap",
    pay.length ? pay.join(", ") : "Payment open",
    ...prefs.must_haves.map((m) => m.charAt(0).toUpperCase() + m.slice(1)),
  ];
}

interface Props {
  preferences: Preferences;
  draft: PreferencesDraft;
}

export function PreferenceChips({ preferences, draft }: Props) {
  return (
    <ul className="flex flex-wrap justify-center gap-2" aria-label="Your preferences">
      {preferenceChips(preferences, draft).map((chip, i) => (
        <motion.li
          key={chip}
          {...rise(i * 0.03, 4, 0.2)}
          className="rounded-full bg-primary-soft px-3 py-1.5 text-[14px] font-medium text-primary"
        >
          {chip}
        </motion.li>
      ))}
    </ul>
  );
}
