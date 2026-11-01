// Easing and timing shared by every motion decision (mirrors the CSS tokens in styles.css).

export const EASE_OUT = [0.23, 1, 0.32, 1] as const;
export const EASE_IN_OUT = [0.77, 0, 0.175, 1] as const;
export const EASE_DRAWER = [0.32, 0.72, 0, 1] as const;

/** The mic to orb morph: once per session, so it may run past the 300ms UI ceiling. */
export const ORB_MORPH = { duration: 0.46, ease: EASE_IN_OUT };

/** Opacity plus a small rise; for things that arrive. */
export function rise(delay = 0, distance = 6, duration = 0.2) {
  return {
    initial: { opacity: 0, transform: `translateY(${distance}px)` },
    animate: { opacity: 1, transform: "translateY(0px)" },
    transition: { duration, ease: EASE_OUT, delay },
  };
}
