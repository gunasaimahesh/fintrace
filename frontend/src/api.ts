import type { DrainEvent, RunEvent } from "./types";

export async function getJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `${res.status} ${res.statusText}`);
  return body as T;
}

export function postJSON<T>(url: string, payload: unknown = {}): Promise<T> {
  return getJSON<T>(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** An API path scoped to one tenant's graph. */
export function api(path: string, tenant: string, params: Record<string, string | number> = {}): string {
  const query = new URLSearchParams({ tenant, ...Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)])) });
  return `${path}?${query}`;
}

type StreamError = { type: "error"; message: string };

/** Opens an event stream and closes it on its last event, so EventSource can't reconnect and rerun the work. A
 * refused stream (an HTTP error) closes without a message, so any early close is reported. */
function stream<E extends { type: string }>(url: string, last: string[], early: string, onEvent: (event: E | StreamError) => void) {
  const source = new EventSource(url);
  let done = false;
  source.onmessage = (message) => {
    const event = JSON.parse(message.data) as E;
    if (last.includes(event.type)) {
      done = true;
      source.close();
    }
    onEvent(event);
  };
  source.onerror = () => {
    if (done) return;
    done = true;
    source.close();
    onEvent({ type: "error", message: early });
  };
  return () => {
    done = true;
    source.close();
  };
}

export function streamInvestigation(entityId: string, mode: string, tenant: string, onEvent: (event: RunEvent) => void) {
  return stream<RunEvent>(api("/api/investigate", tenant, { entity_id: entityId, mode }), ["final", "error"],
    "Lost the connection to the FinTrace API.", onEvent);
}

export function streamDrain(tenant: string, workers: number, mode: string, onEvent: (event: DrainEvent) => void) {
  return stream<DrainEvent>(api("/api/queue/drain", tenant, { workers, mode }), ["drained", "error"],
    "The hunters' stream closed early: hunters may already be draining this tenant's queue, or the connection dropped.",
    onEvent);
}
