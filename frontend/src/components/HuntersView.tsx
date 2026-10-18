import { useEffect, useRef, useState } from "react";
import { api, postJSON, streamDrain } from "../api";
import type { AlertOutcome, Board, CaseRow, DrainEvent, EpisodeRow, QueueAlert } from "../types";
import { Empty, Ids, Section, Status, VerdictBadge, clock, field, primary, secondary } from "./ui";

type LaneItem = {
  alert_id: string;
  subject_id: string;
  attempt: number;
  case_id: string;
  steps: string[];
  notices: string[];
  outcome?: AlertOutcome;
};

// llmOff says why the LLM planner can't be picked (null when it can); resets counts tenant resets made from this page.
type Props = { tenant: string; resets: number; board: Board | null; rules: string[]; llmOff: string | null; refresh: () => void };

function Lane({ worker, items }: { worker: string; items: LaneItem[] }) {
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-sm text-sky-300">{worker}</span>
        <span className="text-[11px] text-zinc-500">
          {items.filter((i) => i.outcome).length} done{items.some((i) => !i.outcome) && " · working"}
        </span>
      </div>
      <ol className="flex max-h-80 flex-col gap-1.5 overflow-y-auto">
        {items.map((item) => (
          <li key={`${item.alert_id}-${item.attempt}`} className="rounded bg-zinc-950/70 px-2 py-1.5 text-xs">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-mono text-zinc-200">{item.alert_id}</span>
              <span className="truncate font-mono text-zinc-500">{item.subject_id}</span>
              {item.attempt > 1 && <span className="text-amber-300">attempt {item.attempt}</span>}
              <span className="ml-auto">
                {item.outcome ? (
                  item.outcome.committed ? (
                    <VerdictBadge verdict={item.outcome.verdict} />
                  ) : (
                    <Status status={item.outcome.status} title={item.outcome.error ?? undefined} />
                  )
                ) : (
                  <span className="animate-pulse text-sky-400">{item.steps.at(-1) ?? "claimed"}</span>
                )}
              </span>
            </div>
            {item.outcome && (
              <div className="mt-0.5 text-[11px] text-zinc-500">
                {item.outcome.tool_calls} calls · {item.case_id}
                {item.outcome.merged_into && <span className="text-violet-300"> → merged into {item.outcome.merged_into}</span>}
                {item.outcome.request_id && <span className="text-rose-300"> · filed {item.outcome.request_id}</span>}
              </div>
            )}
            {item.notices.map((notice, i) => (
              <div key={i} className="mt-0.5 text-[11px] leading-snug text-amber-200/80">
                {notice}
              </div>
            ))}
          </li>
        ))}
      </ol>
    </div>
  );
}

function Attempts({ alert }: { alert: QueueAlert }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {alert.attempts.map((a) => (
        <span
          key={a.attempt}
          className="inline-flex items-center gap-0.5"
          title={[`claimed ${clock(a.claimed_at)}`, a.error, a.refused].filter(Boolean).join(" · ")}
        >
          <span className="whitespace-nowrap font-mono text-[10px] text-zinc-400">
            {a.worker_id}#{a.attempt}
          </span>
          <Status status={a.status} />
          {a.refused && <span className="text-amber-300">⚠</span>}
        </span>
      ))}
    </span>
  );
}

function QueueTable({ queue }: { queue: QueueAlert[] }) {
  if (!queue.length) return <Empty>No alerts in this tenant's queue.</Empty>;
  return (
    <div className="overflow-x-auto rounded-lg border border-zinc-800">
      <table className="w-full text-left text-xs">
        <thead className="bg-zinc-900 text-[11px] uppercase tracking-wider text-zinc-500">
          <tr>
            {["Alert", "Subject", "Rule", "P", "Status", "Attempts (owner#n)", "Case", "Verdict", "Request"].map((h) => (
              <th key={h} className="px-2 py-1.5 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-800/70">
          {queue.map((alert) => (
            <tr key={alert.id} className="align-top">
              <td className="whitespace-nowrap px-2 py-1.5 font-mono text-zinc-200">{alert.id}</td>
              <td className="max-w-48 truncate px-2 py-1.5 font-mono text-zinc-400" title={alert.subject_id}>
                {alert.subject_id}
              </td>
              <td className="px-2 py-1.5 text-zinc-400">{alert.rule.replaceAll("_", " ")}</td>
              <td className="px-2 py-1.5 text-zinc-400">{alert.priority}</td>
              <td className="px-2 py-1.5">
                <Status status={alert.status} title={alert.error ?? undefined} />
              </td>
              <td className="px-2 py-1.5">
                <Attempts alert={alert} />
              </td>
              <td className="whitespace-nowrap px-2 py-1.5 font-mono text-zinc-400">
                {alert.case_id}
                {alert.merged_into && (
                  <div className="text-violet-300" title={`shares ${(alert.merged_on ?? []).join(", ")}`}>
                    → {alert.merged_into}
                  </div>
                )}
              </td>
              <td className="px-2 py-1.5">
                <VerdictBadge verdict={alert.verdict} />
              </td>
              <td className="whitespace-nowrap px-2 py-1.5 font-mono text-zinc-400">{alert.request_id}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Cases({ cases }: { cases: CaseRow[] }) {
  const children = (id: string) => cases.filter((c) => c.merged_into === id);
  const shown = cases.filter((c) => !c.merged_into && (c.requests.length || children(c.id).length));
  if (!shown.length) return <Empty>Cases with a freeze request or a merge show up here.</Empty>;
  return (
    <ul className="flex flex-col gap-2">
      {shown.map((c) => (
        <li key={c.id} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm text-zinc-100">{c.id}</span>
            <Status status={c.status} />
            <span className="font-mono text-zinc-500">{c.subject_id}</span>
            <span className="ml-auto text-zinc-500">{c.evidence.length} evidence entities</span>
          </div>
          {c.requests.length > 0 && (
            <div className="mt-1 text-zinc-400">
              Freeze requests: <Ids ids={c.requests} />
            </div>
          )}
          {children(c.id).map((m) => (
            <div key={m.id} className="mt-1.5 rounded bg-violet-500/10 px-2 py-1 text-violet-200 ring-1 ring-violet-500/30">
              <span className="font-mono">{m.id}</span> ({m.alerts.join(", ") || m.subject_id}) merged in by{" "}
              <span className="font-mono">{m.merge?.by}</span>: {m.merge?.rule}{" "}
              <span className="inline-flex flex-wrap gap-1">
                <Ids ids={m.merge?.shared ?? []} />
              </span>
            </div>
          ))}
        </li>
      ))}
    </ul>
  );
}

function Handoffs({ episodes }: { episodes: EpisodeRow[] }) {
  const shown = episodes.filter((ep) => ep.reused.length || ep.deferred_to.length);
  if (!shown.length) return <Empty>When one hunter builds on another's finding or freeze request, the link shows up here.</Empty>;
  return (
    <ul className="flex flex-col gap-2">
      {shown.map((ep) => (
        <li key={ep.id} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-xs leading-relaxed text-zinc-300">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sky-300">{ep.worker_id}</span>
            <span className="font-mono text-zinc-500">{ep.id}</span>
            <span className="font-mono">{ep.entity_id}</span>
            <VerdictBadge verdict={ep.verdict} />
            <span className="ml-auto text-zinc-500">{ep.tool_calls} calls</span>
          </div>
          {ep.reused.map((r) => (
            <div key={r.fact_id} className="mt-1">
              <span className="text-emerald-300">Reused</span> the route <span className="font-mono">{r.found_from}</span> →{" "}
              <span className="font-mono">{r.target}</span> that <span className="font-mono text-sky-300">{r.worker_id}</span> found in{" "}
              <span className="font-mono">{r.episode_id}</span> ({r.case_id}), instead of searching again.
            </div>
          ))}
          {ep.deferred_to.map((d) => (
            <div key={d.request_id} className="mt-1">
              <span className="text-amber-300">Deferred</span> to <span className="font-mono">{d.request_id}</span> (
              {d.status}) for <Ids ids={d.targets} tone="freeze" />: already awaiting sign-off, so not requested twice.
            </div>
          ))}
        </li>
      ))}
    </ul>
  );
}

export function HuntersView({ tenant, resets, board, rules, llmOff, refresh }: Props) {
  const [workers, setWorkers] = useState(3);
  const [mode, setMode] = useState<"fallback" | "auto">("fallback");
  const [running, setRunning] = useState(false);
  const [lanes, setLanes] = useState<Record<string, LaneItem[]>>({});
  const [summary, setSummary] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [subject, setSubject] = useState("acct-9330192");
  const [rule, setRule] = useState(rules[0] ?? "large_outbound_wire");
  const [priority, setPriority] = useState(9);
  const stop = useRef<(() => void) | null>(null);

  useEffect(() => () => stop.current?.(), []);
  useEffect(() => {
    stop.current?.();
    setLanes({});
    setSummary(null);
    setRunning(false);
  }, [tenant, resets]);

  function onEvent(event: DrainEvent) {
    if (event.type === "error") {
      setError(event.message);
      setRunning(false);
      return;
    }
    if (event.type === "drained") {
      const done = event.outcomes.filter((o) => o.committed).length;
      setSummary(`${done} of ${event.outcomes.length} claims committed by ${event.workers} hunters in ${(event.elapsed_ms / 1000).toFixed(1)} s`);
      setRunning(false);
      refresh();
      return;
    }
    if (event.type === "swept") {
      if (event.expired.length || event.gave_up.length)
        setSummary(`Sweeper returned ${event.retried.length} expired claims to the queue; ${event.gave_up.length} gave up`);
      return;
    }
    setLanes((prev) => {
      const items = [...(prev[event.worker_id] ?? [])];
      if (event.type === "claimed") {
        items.push({ alert_id: event.alert_id, subject_id: event.subject_id, attempt: event.attempt, case_id: event.case_id, steps: [], notices: [] });
      } else {
        const i = items.findLastIndex((item) => item.alert_id === event.alert_id);
        if (i < 0) return prev;
        const item = { ...items[i] };
        if (event.type === "tool_call") item.steps = [...item.steps, event.tool];
        if (event.type === "notice") item.notices = [...item.notices, event.message];
        if (event.type === "alert_done") item.outcome = event;
        items[i] = item;
      }
      return { ...prev, [event.worker_id]: items };
    });
    if (event.type === "alert_done") refresh();
  }

  function run() {
    stop.current?.();
    setLanes({});
    setSummary(null);
    setError(null);
    setRunning(true);
    stop.current = streamDrain(tenant, workers, mode, onEvent);
  }

  async function act(action: () => Promise<unknown>) {
    setError(null);
    try {
      await action();
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const queue = board?.queue ?? [];
  const pending = queue.filter((a) => a.status === "PENDING").length;
  const workerNames = Object.keys(lanes).sort();

  return (
    <div className="flex flex-col gap-5 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <button className={primary} disabled={running || !pending} onClick={run}>
          {running ? "Hunters working…" : `Run ${workers} hunters on ${pending} pending alerts`}
        </button>
        <select className={field} value={workers} onChange={(e) => setWorkers(Number(e.target.value))} disabled={running}>
          {[1, 2, 3, 4, 5, 6].map((n) => (
            <option key={n} value={n}>
              {n} {n === 1 ? "hunter" : "hunters"}
            </option>
          ))}
        </select>
        <select className={field} value={mode} onChange={(e) => setMode(e.target.value as "fallback" | "auto")} disabled={running}>
          <option value="fallback">Planner: deterministic</option>
          <option value="auto" disabled={!!llmOff}>
            Planner: LLM{llmOff ? ` (${llmOff})` : ""}
          </option>
        </select>
        <button className={secondary} disabled={running} onClick={() => act(() => postJSON(api("/api/queue/reset", tenant)))}>
          Reset queue
        </button>
        <form
          className="ml-auto flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            act(() => postJSON(api("/api/alerts", tenant), { subject_id: subject.trim(), rule, priority }));
          }}
        >
          <span className="text-xs text-zinc-500">Raise an alert on</span>
          <input className={`${field} w-44`} value={subject} onChange={(e) => setSubject(e.target.value)} />
          <select className={field} value={rule} onChange={(e) => setRule(e.target.value)}>
            {rules.map((r) => (
              <option key={r} value={r}>
                {r.replaceAll("_", " ")}
              </option>
            ))}
          </select>
          <select className={field} value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
            {[9, 7, 5, 3, 1].map((p) => (
              <option key={p} value={p}>
                priority {p}
              </option>
            ))}
          </select>
          <button className={secondary} disabled={!subject.trim()}>
            Raise
          </button>
        </form>
      </div>
      {error && <div className="rounded bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{error}</div>}
      {summary && <div className="text-xs text-zinc-400">{summary}</div>}

      {workerNames.length > 0 && (
        <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${Math.min(workerNames.length, 3)}, minmax(0, 1fr))` }}>
          {workerNames.map((w) => (
            <Lane key={w} worker={w} items={lanes[w]} />
          ))}
        </div>
      )}

      <Section
        title="Alert queue"
        right={`${queue.filter((a) => a.status === "COMPLETED").length} completed · ${pending} pending · ${queue.filter((a) => a.status === "FAILED").length} failed — claims are atomic, commits check the claim token`}
      >
        <QueueTable queue={queue} />
      </Section>

      <div className="grid gap-5 lg:grid-cols-2">
        <Section title="Cases" right="merged on a shared high-risk entity, or two shared entities">
          <Cases cases={board?.cases ?? []} />
        </Section>
        <Section title="Handoffs between hunters" right="read back from REUSED and DEFERRED_TO edges">
          <Handoffs episodes={board?.episodes ?? []} />
        </Section>
      </div>
    </div>
  );
}
