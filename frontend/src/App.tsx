import { useEffect, useMemo, useRef, useState } from "react";
import { getJSON, streamInvestigation } from "./api";
import { FinalCard } from "./components/FinalCard";
import { GraphCanvas } from "./components/GraphCanvas";
import { TracePanel } from "./components/TracePanel";
import { GraphStore, LAYERS, flaggedChains, graphOf, pinChains } from "./graph";
import type { Envelope, FinalEvent, Health, RunEvent, Scenario } from "./types";

type Depth = 1 | 5;
type View = Depth | "run";

function DepthBadge({ envelope, depth }: { envelope?: Envelope; depth: Depth }) {
  if (!envelope) return <>Loading depth {depth}…</>;
  if (!envelope.ok) return <span className="text-amber-200">{envelope.error}</span>;
  const flagged: { entity: { labels: string[] } }[] = envelope.data.flagged;
  const sanctioned = flagged.filter((hit) => hit.entity.labels.includes("Sanctioned")).length;
  const other = flagged.length - sanctioned;
  return (
    <>
      Depth {depth}: <b className="text-zinc-100">{envelope.data.total_reached.toLocaleString()}</b> entities checked in{" "}
      {envelope.meta.elapsed_ms} ms ·{" "}
      <span className={sanctioned ? "text-rose-300" : "text-emerald-300"}>
        {sanctioned ? `${sanctioned} sanctioned` : "nothing sanctioned"}
      </span>
      {other > 0 && <span className="text-amber-200"> · {other} risk-flagged</span>}
    </>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 rounded bg-zinc-900/80 px-2 py-1 text-[11px] text-zinc-400 ring-1 ring-zinc-800">
      {Object.values(LAYERS).map((layer) => (
        <span key={layer.label} className="flex items-center gap-1">
          <span className="inline-block size-2 rounded-full" style={{ background: layer.color }} />
          {layer.label}
        </span>
      ))}
      <span>— money · ┄ shared device/IP</span>
    </div>
  );
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [input, setInput] = useState("merch-nova-pay");
  const [entityId, setEntityId] = useState("merch-nova-pay");
  const [planner, setPlanner] = useState<"auto" | "fallback">("auto");
  const [view, setView] = useState<View>(1);
  const [explores, setExplores] = useState<Partial<Record<Depth, Envelope>>>({});
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [running, setRunning] = useState(false);
  const runGraph = useRef(new GraphStore());
  const runChains = useRef<string[][]>([]);
  const [runGraphVersion, setRunGraphVersion] = useState(0);
  const stopStream = useRef<(() => void) | null>(null);

  useEffect(() => {
    getJSON<Health>("/api/health").then(setHealth).catch((e: Error) => setApiError(e.message));
    getJSON<Scenario[]>("/api/scenarios").then(setScenarios).catch(() => setScenarios([]));
    return () => stopStream.current?.();
  }, []);

  useEffect(() => {
    let cancelled = false;
    setExplores({});
    for (const depth of [1, 5] as const) {
      getJSON<Envelope>(`/api/explore?${new URLSearchParams({ entity_id: entityId, depth: String(depth) })}`)
        .then((envelope) => !cancelled && setExplores((prev) => ({ ...prev, [depth]: envelope })))
        .catch((e: Error) => !cancelled && setApiError(e.message));
    }
    return () => {
      cancelled = true;
    };
  }, [entityId]);

  function resetRun() {
    stopStream.current?.();
    runGraph.current = new GraphStore();
    runChains.current = [];
    setRunGraphVersion((v) => v + 1);
    setEvents([]);
    setRunning(false);
  }

  function select(id: string) {
    resetRun();
    setInput(id);
    setEntityId(id);
    setView(1);
  }

  function investigate(id: string) {
    if (id !== entityId) setEntityId(id);
    resetRun();
    setRunning(true);
    setView("run");
    stopStream.current = streamInvestigation(id, planner, (event) => {
      setEvents((prev) => [...prev, event]);
      if (event.type === "tool_result" && event.ok) {
        const explore = event.tool === "explore_neighborhood";
        if (explore) runChains.current.push(...flaggedChains([event]));
        // The depth-1 neighbor sample has its own tab; here it would bury the evidence trail.
        if (!(explore && event.args.depth === 1) && runGraph.current.add(event.evidence)) {
          setRunGraphVersion((v) => v + 1);
        }
      }
      if (event.type === "final" || event.type === "error") setRunning(false);
    });
  }

  const graphData = useMemo(() => {
    if (view === "run") return pinChains(runGraph.current.data(), runChains.current);
    const envelope = explores[view];
    return pinChains(graphOf(envelope?.ok ? envelope.evidence : undefined), flaggedChains([envelope]));
    // runGraphVersion signals new evidence in the mutable run graph
  }, [view, explores, runGraphVersion]);

  const final = events.find((event): event is FinalEvent => event.type === "final");
  const toolCalls = events.filter((event) => event.type === "tool_call").length;
  const runSize = runGraph.current.size;

  const viewButton = (value: View, label: string) => (
    <button
      key={String(value)}
      onClick={() => setView(value)}
      disabled={value === "run" && !events.length}
      className={`rounded px-2.5 py-1 text-xs font-medium transition ${view === value ? "bg-zinc-700 text-white" : "text-zinc-400 hover:text-zinc-200 disabled:opacity-40"}`}
    >
      {label}
    </button>
  );

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-4 border-b border-zinc-800 px-5 py-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold tracking-tight">FinTrace</h1>
          <span className="text-xs text-zinc-500">AML investigations an agent runs over a FalkorDB graph</span>
        </div>
        <div className="ml-auto text-xs text-zinc-400">
          {health ? (
            <>
              <span className="font-mono">{health.graph}</span> · {health.nodes.toLocaleString()} nodes ·{" "}
              {health.llm.enabled ? `LLM: ${health.llm.model}` : "no LLM key, deterministic planner"}
            </>
          ) : (
            <span className={apiError ? "text-rose-400" : ""}>{apiError ?? "Connecting to the API…"}</span>
          )}
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-3 border-b border-zinc-800 px-5 py-3">
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (input.trim()) investigate(input.trim());
          }}
        >
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Entity ID, e.g. merch-nova-pay"
            className="w-72 rounded bg-zinc-900 px-3 py-1.5 font-mono text-sm outline-none ring-1 ring-zinc-800 focus:ring-sky-500"
          />
          <button
            type="submit"
            disabled={running || !input.trim()}
            className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-50"
          >
            {running ? `Investigating… ${toolCalls}` : "Investigate"}
          </button>
        </form>
        <select
          value={planner}
          onChange={(e) => setPlanner(e.target.value as "auto" | "fallback")}
          className="rounded bg-zinc-900 px-2 py-1.5 text-xs text-zinc-300 ring-1 ring-zinc-800"
        >
          <option value="auto">Planner: LLM when configured</option>
          <option value="fallback">Planner: deterministic</option>
        </select>
        <div className="ml-auto flex flex-wrap gap-2">
          {scenarios.map((scenario) => (
            <button
              key={scenario.id}
              title={scenario.expect}
              onClick={() => select(scenario.id)}
              className={`rounded px-2.5 py-1 text-xs ring-1 transition ${entityId === scenario.id ? "bg-zinc-800 text-white ring-zinc-600" : "text-zinc-400 ring-zinc-800 hover:text-zinc-200"}`}
            >
              {scenario.label}
            </button>
          ))}
        </div>
      </div>

      <main className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[minmax(0,1fr)_440px] lg:overflow-hidden">
        <section className="relative h-[65vh] min-h-0 overflow-hidden border-b border-zinc-800 lg:h-auto lg:border-b-0 lg:border-r">
          <GraphCanvas data={graphData} focusId={entityId} />
          <div className="pointer-events-none absolute left-4 top-4 z-10 flex flex-col items-start gap-2">
            <div className="pointer-events-auto flex rounded-md bg-zinc-900/90 p-0.5 ring-1 ring-zinc-800">
              {viewButton(1, "Depth 1")}
              {viewButton(5, "Depth 5")}
              {viewButton("run", "Investigation")}
            </div>
            <div className="rounded bg-zinc-900/90 px-2 py-1 text-xs text-zinc-300 ring-1 ring-zinc-800">
              {view === "run" ? (
                <>
                  Evidence from {toolCalls} tool calls: {runSize.nodes} entities, {runSize.links} edges
                </>
              ) : (
                <DepthBadge envelope={explores[view]} depth={view} />
              )}
            </div>
          </div>
          <div className="absolute bottom-3 left-4 right-4 z-10">
            <Legend />
          </div>
        </section>

        <aside className="flex min-h-0 flex-col gap-4 p-4 lg:overflow-y-auto">
          {final && <FinalCard key={final.case_id} final={final} />}
          <TracePanel events={events} />
          {!events.length && (
            <div className="text-sm leading-relaxed text-zinc-500">
              Pick a scenario, compare <b className="text-zinc-300">Depth 1</b> with{" "}
              <b className="text-zinc-300">Depth 5</b>, then press <b className="text-zinc-300">Investigate</b>. Each tool
              call streams here with the Cypher it ran.
            </div>
          )}
        </aside>
      </main>
    </div>
  );
}
