import { useEffect, useRef } from "react";
import type { Line } from "../lib/state";

interface Props {
  lines: Line[];
  empty: string;
  /** Be the clinic: the label on the judge's own lines. */
  youLabel?: string;
}

/** The current call. Each word fades in as the audio reaches it; unrevealed words hold their space. */
export function Transcript({ lines, empty, youLabel = "YOU (PARKWAY)" }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const last = lines[lines.length - 1];
  // The judge's longer label needs a wider column, the same on every row so the text lines up.
  const columns = lines.some((l) => l.judge)
    ? "grid-cols-[88px_minmax(0,1fr)] sm:grid-cols-[96px_minmax(0,1fr)]"
    : "grid-cols-[56px_minmax(0,1fr)] sm:grid-cols-[64px_minmax(0,1fr)]";

  useEffect(() => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, last?.revealed]);

  return (
    <div ref={box} className="scroll-quiet min-h-0 flex-1 overflow-y-auto" aria-live="polite">
      {lines.length === 0 ? (
        <p className="pt-2 text-muted">{empty}</p>
      ) : (
        <ol className="flex flex-col gap-5 pb-2">
          {lines.map((line) => (
            <li key={line.seq} className={`grid gap-3 sm:gap-4 ${columns}`}>
              <span
                className={`speaker-label pt-[6px] ${line.speaker === "agent" ? "text-primary" : line.judge ? "text-ink" : "text-muted"}`}
                style={{ opacity: line.revealed > 0 ? 1 : 0.45 }}
              >
                {line.speaker === "agent" ? "LANA" : line.judge ? youLabel : "CLINIC"}
              </span>
              <p className="max-w-[68ch] text-[16px] leading-[1.6] text-ink">
                {line.words.map((w, i) => (
                  <span key={i} className="word" style={{ opacity: i < line.revealed ? 1 : 0 }}>
                    {w}{" "}
                  </span>
                ))}
              </p>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
