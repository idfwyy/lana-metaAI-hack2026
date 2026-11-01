import { useEffect, useRef } from "react";
import type { Line } from "../lib/state";

interface Props {
  lines: Line[];
  empty: string;
}

/** The current call. Each word fades in as the audio reaches it; unrevealed words hold their space. */
export function Transcript({ lines, empty }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const last = lines[lines.length - 1];

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
            <li key={line.seq} className="grid grid-cols-[56px_minmax(0,1fr)] gap-3 sm:grid-cols-[64px_minmax(0,1fr)] sm:gap-4">
              <span
                className={`speaker-label pt-[6px] ${line.speaker === "agent" ? "text-primary" : "text-muted"}`}
                style={{ opacity: line.revealed > 0 ? 1 : 0.45 }}
              >
                {line.speaker === "agent" ? "LANA" : "CLINIC"}
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
