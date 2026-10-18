import { useCallback, useEffect, useState } from "react";
import { api, getJSON, postJSON } from "./api";
import { ApprovalsView } from "./components/ApprovalsView";
import { EvalView } from "./components/EvalView";
import { HuntersView } from "./components/HuntersView";
import { InvestigateView } from "./components/InvestigateView";
import { MemoryView } from "./components/MemoryView";
import type { Board, Health, Scenario } from "./types";

type Tab = "investigate" | "hunters" | "memory" | "approvals" | "eval";

const TABS: { id: Tab; label: string }[] = [
  { id: "investigate", label: "Investigate" },
  { id: "hunters", label: "Hunters" },
  { id: "memory", label: "Memory" },
  { id: "approvals", label: "Approvals" },
  { id: "eval", label: "Eval" },
];

const DEMO_NOTE_DISMISSED = "fintrace.demoNoteDismissed";

function initialTenant() {
  return new URLSearchParams(window.location.search).get("tenant") ?? "";
}

export default function App() {
  const [tenants, setTenants] = useState<string[]>([]);
  const [tenant, setTenant] = useState(initialTenant);
  const [tab, setTab] = useState<Tab>("investigate");
  const [health, setHealth] = useState<Health | null>(null);
  const [board, setBoard] = useState<Board | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [resetting, setResetting] = useState(false);
  const [resets, setResets] = useState(0);
  const [demoNote, setDemoNote] = useState(() => localStorage.getItem(DEMO_NOTE_DISMISSED) !== "1");

  useEffect(() => {
    getJSON<{ default: string; tenants: string[] }>("/api/tenants")
      .then((list) => {
        setTenants(list.tenants);
        setTenant((current) => (current && list.tenants.includes(current) ? current : list.default));
      })
      .catch((e: Error) => setApiError(e.message));
    getJSON<Scenario[]>("/api/scenarios").then(setScenarios).catch(() => setScenarios([]));
  }, []);

  const refresh = useCallback(() => {
    if (!tenant) return;
    getJSON<Health>(api("/api/health", tenant))
      .then((h) => {
        setHealth(h);
        setApiError(null);
      })
      .catch((e: Error) => setApiError(e.message));
    getJSON<Board>(api("/api/board", tenant)).then(setBoard).catch((e: Error) => setApiError(e.message));
  }, [tenant]);

  useEffect(() => {
    setBoard(null);
    refresh();
    if (tenant) window.history.replaceState(null, "", `?tenant=${encodeURIComponent(tenant)}`);
  }, [tenant, refresh]);

  async function resetTenant() {
    if (!window.confirm(`Restore ${tenant} to its seeded state? Memory, queue, approvals, freezes and graph changes are discarded.`)) return;
    setResetting(true);
    try {
      await postJSON(api("/api/reset", tenant));
      setResets((n) => n + 1);
      refresh();
    } catch (e) {
      setApiError((e as Error).message);
    } finally {
      setResetting(false);
    }
  }

  const demo = health?.public_demo ?? null;
  const queued = board?.queue.filter((a) => a.status === "PENDING").length ?? 0;
  const stale = board?.facts.counts.STALE ?? 0;
  const pending = board?.approvals.filter((r) => r.status === "PENDING_L3").length ?? 0;
  const badges: Partial<Record<Tab, string>> = {
    hunters: queued ? `${queued} queued` : undefined,
    memory: stale ? `${stale} stale` : undefined,
    approvals: pending ? `${pending} pending` : undefined,
  };
  const shown = (id: Tab) => (tab === id ? "flex min-h-0 flex-1 flex-col" : "hidden");

  return (
    <div className="flex h-full flex-col">
      <header className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-zinc-800 px-5 py-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold tracking-tight">FinTrace</h1>
          <span className="hidden text-xs text-zinc-500 xl:inline">AML investigations agents run over a FalkorDB graph</span>
        </div>
        <nav className="flex rounded-md bg-zinc-900 p-0.5 ring-1 ring-zinc-800">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 rounded px-3 py-1 text-xs font-medium transition ${tab === t.id ? "bg-zinc-700 text-white" : "text-zinc-400 hover:text-zinc-200"}`}
            >
              {t.label}
              {badges[t.id] && <span className="rounded-full bg-zinc-800 px-1.5 text-[10px] text-zinc-300">{badges[t.id]}</span>}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex flex-wrap items-center gap-3 text-xs text-zinc-400">
          <label className="flex items-center gap-1.5">
            Tenant
            <select
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              className="rounded bg-zinc-900 px-2 py-1 font-mono text-xs text-zinc-200 ring-1 ring-zinc-800"
            >
              {tenants.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          {health ? (
            <span title="Each tenant has its own graph: demo data isolation, not access control">
              <span className="font-mono">{health.graph}</span> · {health.nodes.toLocaleString()} nodes · revision {health.dataset.rev ?? 0} ·{" "}
              {health.llm.enabled ? `LLM: ${health.llm.model}` : "no LLM key, deterministic planner"}
            </span>
          ) : (
            <span className={apiError ? "text-rose-400" : ""}>{apiError ?? "Connecting to the API…"}</span>
          )}
          <button
            onClick={resetTenant}
            disabled={!tenant || resetting}
            className="rounded px-2 py-1 text-xs text-zinc-400 ring-1 ring-zinc-800 hover:text-zinc-200 disabled:opacity-40"
          >
            {resetting ? "Resetting…" : "Reset tenant"}
          </button>
        </div>
      </header>
      {apiError && health && <div className="border-b border-rose-500/30 bg-rose-500/10 px-5 py-1.5 text-xs text-rose-300">{apiError}</div>}
      {demo && demoNote && (
        <div className="flex items-start gap-3 border-b border-sky-500/20 bg-sky-500/5 px-5 py-1.5 text-xs text-sky-200/80">
          <span>
            Public demo: every visitor shares these graphs, so changes are applied one at a time and everyone sees them. Reset tenant restores the seeded data.
            {health?.llm.enabled &&
              ` Model runs are capped at ${demo.llm_runs_per_hour} an hour (${demo.llm_runs_left} left) and ${demo.llm_runs_per_visitor_per_hour} per visitor; past that, investigations use the deterministic planner, as hunters always do here.`}
          </span>
          <button
            onClick={() => {
              localStorage.setItem(DEMO_NOTE_DISMISSED, "1");
              setDemoNote(false);
            }}
            title="Dismiss"
            aria-label="Dismiss the public demo note"
            className="ml-auto px-1 text-sm leading-none text-sky-200/60 hover:text-sky-100"
          >
            ×
          </button>
        </div>
      )}

      {tenant && (
        <>
          <div className={shown("investigate")}>
            <InvestigateView tenant={tenant} resets={resets} scenarios={scenarios} onError={setApiError} onFinished={refresh} />
          </div>
          <div className={`${shown("hunters")} overflow-y-auto`}>
            <HuntersView
              tenant={tenant}
              resets={resets}
              board={board}
              rules={health?.alert_rules ?? []}
              llmOff={!health?.llm.enabled ? "no key" : demo ? "off in the public demo" : null}
              refresh={refresh}
            />
          </div>
          <div className={`${shown("memory")} overflow-y-auto`}>
            <MemoryView tenant={tenant} board={board} refresh={refresh} />
          </div>
          <div className={`${shown("approvals")} overflow-y-auto`}>
            <ApprovalsView tenant={tenant} board={board} refresh={refresh} />
          </div>
          <div className={`${shown("eval")} overflow-y-auto`}>
            <EvalView active={tab === "eval"} />
          </div>
        </>
      )}
    </div>
  );
}
