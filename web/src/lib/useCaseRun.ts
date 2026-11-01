// One Watch run: socket in, player in the middle, reducer out.

import { useCallback, useEffect, useReducer, useRef } from "react";
import { toast } from "sonner";
import { Player } from "./player";
import { CaseSocket } from "./socket";
import { initialRun, runReducer, type RunState } from "./state";

export function useCaseRun(): {
  state: RunState;
  start: (caseId: string) => void;
  stop: () => void;
} {
  const [state, dispatch] = useReducer(runReducer, initialRun);
  const socket = useRef<CaseSocket | null>(null);
  const player = useRef<Player | null>(null);

  const stop = useCallback(() => {
    socket.current?.stop();
    socket.current = null;
    player.current?.reset();
    player.current = null;
    dispatch({ kind: "reset" });
  }, []);

  const start = useCallback(
    (caseId: string) => {
      stop();
      const p = new Player({
        apply: (event) => dispatch({ kind: "event", event, now: performance.now() }),
        reveal: (seq, count) => dispatch({ kind: "reveal", seq, count }),
      });
      const s = new CaseSocket(caseId, {
        onMessage: (m) => p.push(m),
        onRestart: () => {
          p.reset();
          dispatch({ kind: "reset" });
          toast("Connection dropped. Lana is starting the calls again.");
        },
        onFatal: (message) =>
          dispatch({
            kind: "event",
            event: { type: "error", case_id: caseId, message },
            now: performance.now(),
          }),
      });
      player.current = p;
      socket.current = s;
      s.open();
    },
    [stop],
  );

  const finished = (state.offers !== null && state.caseStatus === "offers_ready") || state.error !== null;
  useEffect(() => {
    if (finished) socket.current?.markFinished();
  }, [finished]);

  useEffect(() => stop, [stop]);

  return { state, start, stop };
}
