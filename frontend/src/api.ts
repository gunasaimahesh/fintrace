import type { RunEvent } from "./types";

export async function getJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  return body as T;
}

export function postJSON<T>(url: string, payload: unknown): Promise<T> {
  return getJSON<T>(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** Streams one investigation; closes as soon as the final (or error) event arrives so EventSource can't reconnect and rerun it. */
export function streamInvestigation(entityId: string, mode: string, onEvent: (event: RunEvent) => void): () => void {
  const params = new URLSearchParams({ entity_id: entityId, mode });
  const source = new EventSource(`/api/investigate?${params}`);
  source.onmessage = (message) => {
    const event = JSON.parse(message.data) as RunEvent;
    if (event.type === "final" || event.type === "error") source.close();
    onEvent(event);
  };
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) return;
    source.close();
    onEvent({ type: "error", message: "Lost the connection to the FinTrace API." });
  };
  return () => source.close();
}
