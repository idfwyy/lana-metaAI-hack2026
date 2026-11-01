// The case WebSocket: sends start / stop, hands every message to onMessage.
// An unexpected close reconnects once (the server cancels the run when a socket drops,
// so the caller resets and the run starts over). A second close is fatal.

import type { ClientMessage, ServerMessage } from "./types";

export const WS_URL = (import.meta.env.VITE_WS_URL as string | undefined) ?? "ws://localhost:8000";

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
  ) {}

  open(): void {
    const ws = new WebSocket(`${WS_URL}/ws/case/${encodeURIComponent(this.caseId)}`);
    this.ws = ws;
    ws.onopen = () => this.send({ type: "start" });
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
      if (!this.reconnected) {
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

  stop(): void {
    this.closedByUs = true;
    this.send({ type: "stop" });
    this.ws?.close();
    this.ws = null;
  }
}
