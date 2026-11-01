import { useEffect, useRef } from "react";
import { animate, useReducedMotion } from "motion/react";
import { EASE_OUT } from "../lib/motion";
import { money } from "../lib/format";

interface Props {
  value: number;
  /** Where the first count starts; defaults to value (no count on mount). */
  from?: number;
  duration?: number;
  format?: (n: number) => string;
  className?: string;
}

/** A number that counts to each new value. Writes text directly, so counting never re-renders. */
export function AnimatedNumber({ value, from, duration = 0.6, format = money, className }: Props) {
  const node = useRef<HTMLSpanElement>(null);
  const shown = useRef(from ?? value);
  const reduce = useReducedMotion();

  useEffect(() => {
    const el = node.current;
    if (!el) return;
    if (reduce || shown.current === value) {
      shown.current = value;
      el.textContent = format(value);
      return;
    }
    const controls = animate(shown.current, value, {
      duration,
      ease: EASE_OUT,
      onUpdate: (v) => {
        shown.current = v;
        el.textContent = format(v);
      },
    });
    return () => controls.stop();
  }, [value, duration, format, reduce]);

  return (
    <span ref={node} className={`tnum ${className ?? ""}`}>
      {format(shown.current)}
    </span>
  );
}
