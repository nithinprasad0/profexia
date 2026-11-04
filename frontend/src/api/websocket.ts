import { tokenStore } from "./client";
import { WS_URL as WS_BASE } from "./config";

export type SocketEvent = {
  event: string;
  [key: string]: unknown;
};

export class ProfexiaSocket {
  private socket: WebSocket | null = null;

  constructor(private readonly path: string, private readonly onEvent: (event: SocketEvent) => void) {}

  connect(): void {
    const token = tokenStore.getAccessToken();
    if (!token) throw new Error("Authentication required for WebSocket connection");
    const separator = this.path.includes("?") ? "&" : "?";
    this.socket = new WebSocket(`${WS_BASE}${this.path}${separator}token=${encodeURIComponent(token)}`);
    this.socket.onmessage = (message) => {
      try {
        this.onEvent(JSON.parse(message.data) as SocketEvent);
      } catch {
        this.onEvent({ event: "message", data: message.data });
      }
    };
  }

  send(payload: unknown): void {
    if (this.socket?.readyState !== WebSocket.OPEN) throw new Error("WebSocket is not connected");
    this.socket.send(JSON.stringify(payload));
  }

  close(): void {
    this.socket?.close();
    this.socket = null;
  }
}

export const realtimeSockets = {
  chat: (roomId: string, onEvent: (event: SocketEvent) => void) =>
    new ProfexiaSocket(`/ws/chat/${encodeURIComponent(roomId)}`, onEvent),
  notifications: (onEvent: (event: SocketEvent) => void) =>
    new ProfexiaSocket("/ws/notifications", onEvent),
  learningSession: (sessionId: number, onEvent: (event: SocketEvent) => void) =>
    new ProfexiaSocket(`/ws/learning-sessions/${sessionId}`, onEvent),
};