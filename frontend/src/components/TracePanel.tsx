import type { RunEvent, ToolCallEvent, ToolResultEvent } from "../types";

type Item =
  | { kind: "step"; call: ToolCallEvent; result?: ToolResultEvent }
  | { kind: "notice" | "error"; message: string };

function itemsOf(events: RunEvent[]): Item[] {
  const items: Item[] = [];
  const steps = new Map<number, Item & { kind: "step" }>();
  for (const event of events) {
    if (event.type === "tool_call") {
      const item = { kind: "step" as const, call: event };
      steps.set(event.step, item);
      items.push(item);
    } else if (event.type === "tool_result") {
      const item = steps.get(event.step);
      if (item) item.result = event;
    } else if (event.type === "notice" || event.type === "error") {
      items.push({ kind: event.type, message: event.message });
    }
  }
  return items;
}

function formatArgs(args: Record<string, unknown>) {
  return Object.entries(args)
    .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join("  ");
}

function Status({ result }: { result?: ToolResultEvent }) {
  if (!result) return <span className="animate-pulse text-sky-400">running</span>;
  if (!result.ok) return <span className="text-rose-400">error</span>;
  if (result.stop) return <span className="text-amber-300">stop: {result.stop}</span>;
  return (
    <span className="text-zinc-500">
      {result.meta.elapsed_ms} ms · {result.meta.rows.toLocaleString()} rows
    </span>
  );
}

function Step({ call, result }: { call: ToolCallEvent; result?: ToolResultEvent }) {
  const queries = result?.meta.queries ?? [];
  return (
    <li className="rounded-md border border-zinc-800 bg-zinc-900/60 px-3 py-2">
      <div className="flex items-baseline gap-2 text-xs">
        <span className="w-5 shrink-0 font-mono text-zinc-500">{call.step}</span>
        <span className="font-mono text-sm text-sky-300">{call.tool}</span>
        <span className="ml-auto shrink-0">
          <Status result={result} />
        </span>
      </div>
      <div className="mt-1 truncate pl-7 font-mono text-xs text-zinc-400" title={formatArgs(call.args)}>
        {formatArgs(call.args)}
      </div>
      {call.thought && <div className="mt-1 pl-7 text-xs italic text-zinc-500">{call.thought}</div>}
      {result?.error && <div className="mt-1 pl-7 text-xs text-rose-400">{result.error}</div>}
      {result && (
        <div className="mt-1 flex flex-col gap-1 pl-7">
          {queries.length > 0 && (
            <details>
              <summary className="text-xs text-zinc-500 hover:text-zinc-300">
                Cypher ({queries.length} {queries.length === 1 ? "query" : "queries"}, parameterized)
              </summary>
              {queries.map((query, i) => (
                <div key={i} className="mt-1">
                  <div className="text-[11px] text-zinc-500">
                    {query.calls}× · {query.ms} ms · params: {query.params.join(", ") || "none"}
                  </div>
                  <pre className="max-h-48 overflow-auto rounded bg-zinc-950 p-2 font-mono text-[11px] leading-snug text-emerald-200/90">
                    {query.cypher}
                  </pre>
                </div>
              ))}
            </details>
          )}
          {result.data && (
            <details>
              <summary className="text-xs text-zinc-500 hover:text-zinc-300">Result</summary>
              <pre className="mt-1 max-h-64 overflow-auto rounded bg-zinc-950 p-2 font-mono text-[11px] leading-snug text-zinc-300">
                {JSON.stringify(result.data, null, 2)}
              </pre>
            </details>
          )}
        </div>
      )}
    </li>
  );
}

export function TracePanel({ events }: { events: RunEvent[] }) {
  const started = events.find((event) => event.type === "run_started");
  const items = itemsOf(events);
  if (!started && !items.length) return null;
  return (
    <section className="flex flex-col gap-2">
      <h2 className="flex items-baseline gap-2 text-xs font-medium uppercase tracking-wider text-zinc-500">
        Tool trace
        {started && (
          <span className="font-mono normal-case tracking-normal text-zinc-600">
            {started.case_id} · max {started.max_steps} calls
          </span>
        )}
      </h2>
      <ol className="flex flex-col gap-2">
        {items.map((item, i) =>
          item.kind === "step" ? (
            <Step key={`s${item.call.step}`} call={item.call} result={item.result} />
          ) : (
            <li
              key={`n${i}`}
              className={`rounded-md px-3 py-2 text-xs ${item.kind === "error" ? "bg-rose-500/10 text-rose-300" : "bg-amber-500/10 text-amber-200"}`}
            >
              {item.message}
            </li>
          ),
        )}
      </ol>
    </section>
  );
}
