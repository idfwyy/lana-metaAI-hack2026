// The case WebSocket: opens with start (Watch) or listen (the patient's voice intake), sends
// stop, text and binary mic frames, and hands every message to onMessage.
// An unexpected close reconnects once (the server cancels the run when a socket drops,
// so the caller resets and the run starts over). A second close is fatal.

import { API_URL } from "./api";
import type { ClientMessage, ServerMessage } from "./types";

// Blank VITE_WS_URL: the same host as VITE_API_URL (https -> wss, http -> ws).
export const WS_URL = ((import.meta.env.VITE_WS_URL as string | undefined) || API_URL.replace(/^http/, "ws")).replace(
  /\/+$/,
  "",
);

export interface CaseSocketHandlers {
  onMessage: (m: ServerMessage) => void;
  /** Called before the run restarts on a reconnect. */
  onRestart: () => void;
  onFatal: (message: string) => void;
}

export class CaseSocket {
  private ws: WebSocket | null = null;
  private reconnected = false;
  private closedByUs = false;
  private finished = false;

  constructor(
    private readonly caseId: string,
    private readonly handlers: CaseSocketHandlers,
    private readonly opening: ClientMessage = { type: "start" },
  ) {}

  open(): void {
    if (!WS_URL) {
      this.handlers.onFatal("This site isn't connected to Lana's server yet.");
      return;
    }
    let opened = false;
    const ws = new WebSocket(`${WS_URL}/ws/case/${encodeURIComponent(this.caseId)}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      opened = true;
      this.send(this.opening);
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") return;
      let msg: ServerMessage;
      try {
        msg = JSON.parse(e.data) as ServerMessage;
      } catch {
        return;
      }
      this.handlers.onMessage(msg);
    };
    ws.onclose = () => {
      if (this.ws !== ws || this.closedByUs || this.finished) return;
      if (!opened) {
        // Never connected: retrying the run won't help.
        this.handlers.onFatal("Can't reach Lana's server. Try again in a moment.");
      } else if (!this.reconnected) {
        this.reconnected = true;
        this.handlers.onRestart();
        this.open();
      } else {
        this.handlers.onFatal("Lost the connection to Lana's server. Start over to try again.");
      }
    };
  }

  /** The run reached its end; a close after this is expected, not an error. */
  markFinished(): void {
    this.finished = true;
  }

  send(msg: ClientMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  /** One 100 ms mic frame (16-bit mono PCM, 24 kHz). Dropped while the socket is not open. */
  sendAudio(frame: ArrayBuffer): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(frame);
  }

  stop(): void {
    this.closedByUs = true;
    this.send({ type: "stop" });
    this.ws?.close();
    this.ws = null;
  }
}
