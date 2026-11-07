// Live events from GET /api/v1/events (Server-Sent Events).
// EventSource can't send an Authorization header, so this reads the stream with fetch.
import { useEffect, useRef } from "react";
import { getToken } from "../api/client";

export type ServerEvent =
  | { type: "job.updated"; data: JobEvent }
  | { type: "game.updated"; data: { id: string } }
  | { type: "game.update_available"; data: { id: string; version: string } };

export interface JobEvent {
  id: number;
  kind: string;
  game_id: string | null;
  status: string;
  progress: number | null;
  message: string | null;
}

type Listener = (e: ServerEvent) => void;
const listeners = new Set<Listener>();
let running = false;

async function connect() {
  running = true;
  let delay = 1000;
  while (listeners.size > 0) {
    const token = getToken();
    if (!token) break;
    try {
      const res = await fetch("/api/v1/events", { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      delay = 1000;
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += value;
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let type = "";
          let data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event: ")) type = line.slice(7);
            else if (line.startsWith("data: ")) data += line.slice(6);
          }
          if (type && data) {
            const ev = { type, data: JSON.parse(data) } as ServerEvent;
            listeners.forEach((l) => l(ev));
          }
        }
      }
    } catch {
      /* reconnect below */
    }
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(delay * 2, 30000);
  }
  running = false;
}

/** Subscribe to server events while the component is mounted. */
export function useServerEvents(fn: Listener) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    const l: Listener = (e) => ref.current(e);
    listeners.add(l);
    if (!running) connect();
    return () => {
      listeners.delete(l);
    };
  }, []);
}
