// One case on one socket: socket in, player in the middle, reducers out.
// A Watch case opens with start. A patient case opens with listen: Lana greets, the mic
// streams to the server, and the listen state below drives Home until a Watch run starts.

import { useCallback, useEffect, useReducer, useRef } from "react";
import { toast } from "sonner";
import type { Mic } from "./mic";
import { Player } from "./player";
import { CaseSocket } from "./socket";
import { initialRun, runReducer, type RunState } from "./state";
import type { ClientMessage, IntakeTopic, Preferences, PreferencesDraft, ServerEvent } from "./types";

export interface ListenState {
  /** A patient session is open on the socket. */
  active: boolean;
  mic: "starting" | "on" | "off";
  /** No mic, or no speech to text: phrases become buttons and a text box appears. */
  fallback: boolean;
  /** What the patient is saying right now, before Meta ends the turn. */
  partial: string;
  /** The patient's last finished turn. */
  heard: string | null;
  /** Between the patient's turn and Lana's reply. */
  thinking: boolean;
  lanaSpeaking: boolean;
  /** The question Lana asked last; null before the first one and once intake is done. */
  topic: IntakeTopic | null;
  done: boolean;
  draft: PreferencesDraft;
  preferences: Preferences | null;
}

export const initialListen: ListenState = {
  active: false,
  mic: "off",
  fallback: false,
  partial: "",
  heard: null,
  thinking: false,
  lanaSpeaking: false,
  topic: null,
  done: false,
  draft: {},
  preferences: null,
};

type ListenAction =
  | { kind: "listen" }
  | { kind: "mic"; mic: ListenState["mic"]; fallback?: boolean }
  | { kind: "speaking"; on: boolean }
  | { kind: "event"; event: ServerEvent }
  | { kind: "reset" };

function listenReducer(state: ListenState, action: ListenAction): ListenState {
  switch (action.kind) {
    case "reset":
      return initialListen;
    case "listen":
      return { ...initialListen, active: true, mic: "starting" };
    case "mic":
      return { ...state, mic: action.mic, fallback: state.fallback || !!action.fallback };
    case "speaking":
      return { ...state, lanaSpeaking: action.on };
  }
  if (!state.active) return state;
  const e = action.event;
  switch (e.type) {
    case "user_partial":
      return { ...state, partial: e.text, heard: null };
    case "user_said":
      // Once intake is done Lana answers only commands, so nothing else waits on her.
      return { ...state, partial: "", heard: e.text, thinking: !state.done };
    case "agent_said":
      return e.call_id === null ? { ...state, thinking: false } : state;
    case "intake":
      return { ...state, topic: e.topic, done: e.done, draft: e.draft, preferences: e.preferences ?? state.preferences };
    case "stt_unavailable":
      return { ...state, fallback: true, mic: "off" };
    case "error":
      return { ...state, thinking: false };
    default:
      return state;
  }
}

export function useCaseRun(onStartOver?: () => void): {
  state: RunState;
  listen: ListenState;
  start: (caseId: string) => void;
  listenTo: (caseId: string, mic: Promise<Mic>) => void;
  sendText: (text: string) => void;
  sendAudio: (frame: ArrayBuffer) => void;
  micLevel: () => number;
  stop: () => void;
} {
  const [state, dispatch] = useReducer(runReducer, initialRun);
  const [listen, listenDispatch] = useReducer(listenReducer, initialListen);
  const socket = useRef<CaseSocket | null>(null);
  const player = useRef<Player | null>(null);
  const mic = useRef<Mic | null>(null);
  const lanaSpeaking = useRef(false);
  const startOver = useRef(onStartOver);
  startOver.current = onStartOver;

  const stopMic = useCallback(() => {
    mic.current?.stop();
    mic.current = null;
  }, []);

  const stop = useCallback(() => {
    stopMic();
    socket.current?.stop();
    socket.current = null;
    player.current?.reset();
    player.current = null;
    dispatch({ kind: "reset" });
    listenDispatch({ kind: "reset" });
  }, [stopMic]);

  const open = useCallback(
    (caseId: string, opening: ClientMessage) => {
      stop();
      const p = new Player({
        apply: (event) => {
          dispatch({ kind: "event", event, now: performance.now() });
          listenDispatch({ kind: "event", event });
          if (event.type === "stt_unavailable") stopMic();
          // Let the player finish this step before Start over tears it down.
          if (event.type === "command" && event.name === "start_over") queueMicrotask(() => startOver.current?.());
        },
        reveal: (seq, count) => dispatch({ kind: "reveal", seq, count }),
        speaking: (on) => {
          lanaSpeaking.current = on;
          if (on) mic.current?.pause();
          else mic.current?.resume();
          listenDispatch({ kind: "speaking", on });
        },
      });
      const s = new CaseSocket(
        caseId,
        {
          onMessage: (m) => p.push(m),
          onRestart: () => {
            p.reset();
            dispatch({ kind: "reset" });
            if (opening.type === "listen") {
              listenDispatch({ kind: "listen" });
              listenDispatch({ kind: "mic", mic: mic.current ? "on" : "off" });
              toast("Connection dropped. Lana is starting over.");
            } else {
              toast("Connection dropped. Lana is starting the calls again.");
            }
          },
          onFatal: (message) =>
            dispatch({
              kind: "event",
              event: { type: "error", case_id: caseId, message },
              now: performance.now(),
            }),
        },
        opening,
      );
      player.current = p;
      socket.current = s;
      s.open();
      return s;
    },
    [stop, stopMic],
  );

  const start = useCallback((caseId: string) => void open(caseId, { type: "start" }), [open]);

  const listenTo = useCallback(
    (caseId: string, micPromise: Promise<Mic>) => {
      const s = open(caseId, { type: "listen" });
      listenDispatch({ kind: "listen" });
      micPromise.then(
        (m) => {
          if (socket.current !== s) return m.stop(); // started over while the mic was asked for
          mic.current = m;
          if (lanaSpeaking.current) m.pause();
          listenDispatch({ kind: "mic", mic: "on" });
        },
        () => {
          if (socket.current === s) listenDispatch({ kind: "mic", mic: "off", fallback: true });
        },
      );
    },
    [open],
  );

  const sendText = useCallback((text: string) => {
    const t = text.trim();
    if (t) socket.current?.send({ type: "user_text", text: t });
  }, []);
  const sendAudio = useCallback((frame: ArrayBuffer) => socket.current?.sendAudio(frame), []);
  const micLevel = useCallback(() => mic.current?.level() ?? 0, []);

  // A Watch run started (the patient said "just show me"): the mic's job is done.
  useEffect(() => {
    if (state.caseStatus !== null) stopMic();
  }, [state.caseStatus, stopMic]);

  const finished = (state.offers !== null && state.caseStatus === "offers_ready") || state.error !== null;
  useEffect(() => {
    if (finished) {
      socket.current?.markFinished();
      stopMic();
    }
  }, [finished, stopMic]);

  useEffect(() => stop, [stop]);

  return { state, listen, start, listenTo, sendText, sendAudio, micLevel, stop };
}
