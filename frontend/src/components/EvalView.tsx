import { useCallback, useEffect, useState } from "react";
import { getJSON } from "../api";
import type { Condition, ContentionResult, ControlledResult, EvalResults, RetrievalResult, Score, Spread } from "../types";
import { Empty, Section, VerdictBadge, secondary } from "./ui";

const CONDITIONS: { id: Condition; about: string }[] = [
  { id: "cold", about: "the FinTrace agent on a fresh graph" },
  { id: "warm", about: "the same, after learning the NovaPay playbook from an approved run" },
  { id: "revisit", about: "the same subject again, right after the cold run" },
  { id: "baseline", about: "one-entity lookups, 25 neighbours a page, same step budget and verdict rules" },
];
const GROUPS = [
  { id: "positive", label: "Positives" },
  { id: "negative", label: "Negatives" },
  { id: "unknown", label: "Unknown IDs" },
  { id: "incomplete", label: "Out of budget" },
  { id: "demo", label: "Demo" },
];
const th = "px-2 py-1.5 text-left font-medium text-zinc-500";
const td = "px-2 py-1.5 align-top";

// At least one decimal, so a median between two runs shows as 8.5 rather than rounding to a run count.
const num = (v: number, digits: number) => v.toLocaleString(undefined, { maximumFractionDigits: Math.max(digits, 1) });

function spread(s: Spread, digits = 0) {
  if (!s) return "—";
  const f = (v: number) => num(v, digits);
  return s.min === s.max ? f(s.median) : `${f(s.median)} (${f(s.min)}–${f(s.max)})`;
}

function SpreadCell({ s, digits = 0 }: { s: Spread; digits?: number }) {
  const f = (v: number) => num(v, digits);
  return (
    <td className={`${td} whitespace-nowrap font-mono`}>
      {s ? f(s.median) : <span className="text-zinc-600">—</span>}
      {s && s.min !== s.max && (
        <div className="text-[10px] text-zinc-500">
          {f(s.min)}–{f(s.max)}
        </div>
      )}
    </td>
  );
}

function ratio(text: string): Score {
  const [correct, runs] = text.split("/").map(Number);
  return { correct, runs };
}

function Command({ text }: { text: string }) {
  return <code className="whitespace-nowrap rounded bg-zinc-900 px-1.5 py-0.5 font-mono text-[11px] text-zinc-300 ring-1 ring-zinc-800">{text}</code>;
}

function ScoreCell({ score }: { score?: Score }) {
  if (!score) return <span className="text-zinc-600">—</span>;
  const tone = score.correct === score.runs ? "text-emerald-300" : score.correct === 0 ? "text-rose-300" : "text-amber-300";
  return (
    <span className={`font-mono ${tone}`}>
      {score.correct}/{score.runs}
    </span>
  );
}

function Toggle<T extends string>({ options, value, onChange }: { options: { id: T; label: string }[]; value: T; onChange: (v: T) => void }) {
  return (
    <span className="inline-flex rounded bg-zinc-900 p-0.5 ring-1 ring-zinc-800">
      {options.map((o) => (
        <button key={o.id} onClick={() => onChange(o.id)} className={`rounded px-2 py-0.5 text-[11px] ${value === o.id ? "bg-zinc-700 text-white" : "text-zinc-400"}`}>
          {o.label}
        </button>
      ))}
    </span>
  );
}

function Controlled({ result }: { result: ControlledResult }) {
  const { config, summary } = result;
  const rows = CONDITIONS.filter((c) => summary.conditions[c.id]);
  const llm = config.planner === "llm";
  const repeats = Array.isArray(config.repeats) ? config.repeats : [1, config.repeats];
  const stopped = [...(result.stages ?? []).map((s) => s.stopped_early), result.stopped_early].filter(Boolean);
  const stageCommits = (result.stages ?? []).flatMap((s) => (s.commit ? [`${s.commit}${s.uncommitted_backend_changes ? " (with uncommitted backend changes)" : ""}`] : []));
  const commits = stageCommits.length ? [...new Set(stageCommits)] : [String(config.commit)];
  const misses = Object.entries(summary.cases).filter(([, c]) => rows.some((r) => c[r.id] && ratio(c[r.id]!.correct).correct < ratio(c[r.id]!.correct).runs));
  const warm = summary.warm_vs_cold;
  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-xs text-zinc-300">
          <thead className="border-b border-zinc-800 bg-zinc-900/60">
            <tr>
              <th className={th}>Condition</th>
              <th className={th}>Detection</th>
              {GROUPS.map((g) => (
                <th key={g.id} className={th}>
                  {g.label}
                </th>
              ))}
              <th className={th}>Tool calls</th>
              <th className={th}>Verdict at call</th>
              {llm && <th className={th}>LLM requests</th>}
              {llm && <th className={th}>LLM tokens</th>}
              <th className={th}>DB queries</th>
              <th className={th}>Bytes to planner</th>
              <th className={th}>Failures</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ id, about }) => {
              const c = summary.conditions[id]!;
              return (
                <tr key={id} className="border-b border-zinc-800/60 last:border-0">
                  <td className={td}>
                    <div className="font-medium capitalize text-zinc-100">{id}</div>
                    <div className="w-44 text-[11px] text-zinc-500">{about}</div>
                  </td>
                  <td className={td}>
                    <ScoreCell score={c.detection} />
                  </td>
                  {GROUPS.map((g) => (
                    <td key={g.id} className={td}>
                      <ScoreCell score={c.groups[g.id]} />
                    </td>
                  ))}
                  <SpreadCell s={c.tool_calls} />
                  <SpreadCell s={c.verdict_step} />
                  {llm && <SpreadCell s={c.llm_requests} />}
                  {llm && <SpreadCell s={c.llm_tokens} />}
                  <SpreadCell s={c.db_queries} />
                  <SpreadCell s={c.bytes_to_planner} />
                  <td className={td}>
                    {c.failures.length ? (
                      <span className="text-rose-300" title={c.failures.map((f) => `${f.case_id} r${f.repeat}: ${f.failure}`).join("\n")}>
                        {c.failures.length}
                      </span>
                    ) : (
                      <span className="text-zinc-500">0</span>
                    )}
                    {llm && c.planner_used.fallback ? <div className="text-[11px] text-amber-300">{c.planner_used.fallback} fell back</div> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-[11px] leading-relaxed text-zinc-500">
        Detection covers the positive and negative cases; medians (with ranges) are over those runs. A verdict is correct only with the expected route
        (target and hop count) for suspicious cases and an exhaustive or full-depth search for clear ones. Unknown IDs must be reported as unknown, and
        out-of-budget runs must end insufficient rather than guess. {llm ? `${config.model}, temperature 0` : "Deterministic planner"} · repeats {repeats[0]}–
        {repeats[1]} · at most {config.max_steps} tool calls · prescribed depth {config.depth} · graph <span className="font-mono">{config.graph}</span> · {commits.length > 1 ? "commits" : "commit"}{" "}
        <span className="font-mono">{commits.join(", ")}</span>
      </p>
      {stopped.map((s) => (
        <p key={s} className="text-xs text-amber-300">
          Stopped early {s}
        </p>
      ))}
      {warm.playbook_used_on.length > 0 && (
        <div className="text-xs text-zinc-400">
          <span className="text-zinc-300">Playbook applied on </span>
          {warm.playbook_used_on.map((id) => {
            const d = warm.cases[id];
            if (!d) return null;
            const change = (key: "tool_calls" | "llm_requests" | "llm_tokens", label: string) =>
              d[key] ? `${label} ${d[key]!.cold.toLocaleString()} → ${d[key]!.warm.toLocaleString()}` : null;
            return (
              <span key={id} className="mr-3 inline-block">
                <span className="font-mono text-zinc-200">{id}</span> ({d.correct} correct;{" "}
                {[change("tool_calls", "tool calls"), change("llm_requests", "LLM requests"), change("llm_tokens", "tokens")].filter(Boolean).join(", ")})
              </span>
            );
          })}
        </div>
      )}
      {misses.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-zinc-800">
          <table className="w-full text-xs text-zinc-300">
            <thead className="border-b border-zinc-800 bg-zinc-900/60">
              <tr>
                <th className={th}>Case with a miss</th>
                <th className={th}>Expected</th>
                {rows.map((r) => (
                  <th key={r.id} className={`${th} capitalize`}>
                    {r.id}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {misses.map(([id, c]) => (
                <tr key={id} className="border-b border-zinc-800/60 last:border-0">
                  <td className={td}>
                    <span className="font-mono text-zinc-100">{id}</span> <span className="font-mono text-zinc-500">{c.subject_id}</span>
                    <div className="text-[11px] text-zinc-500">
                      {c.group}
                      {c.pattern && ` · ${c.pattern.replaceAll("_", " ")}`}
                      {c.split && ` · ${c.split}`}
                    </div>
                  </td>
                  <td className={td}>{c.expected ? <VerdictBadge verdict={c.expected} /> : <span className="text-zinc-500">unknown entity</span>}</td>
                  {rows.map((r) => {
                    const x = c[r.id];
                    return (
                      <td key={r.id} className={td}>
                        {x ? (
                          <>
                            <ScoreCell score={ratio(x.correct)} />
                            <div className="text-[11px] text-zinc-500">
                              {Object.entries(x.verdicts)
                                .map(([v, n]) => `${v.toLowerCase()} ×${n}`)
                                .join(", ")}
                            </div>
                          </>
                        ) : (
                          <span className="text-zinc-600">—</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Retrieval({ result }: { result: RetrievalResult }) {
  const { strategies, families } = result.summary;
  const names = Object.keys(strategies);
  const limits = result.config.output_limits;
  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-xs text-zinc-300">
          <thead className="border-b border-zinc-800 bg-zinc-900/60">
            <tr>
              <th className={th}>Strategy</th>
              <th className={th}>Recall</th>
              <th className={th}>Found any</th>
              <th className={th}>IDs returned</th>
              <th className={th}>Not in the instance</th>
              <th className={th}>Calls</th>
              <th className={th}>DB queries</th>
              <th className={th}>DB ms</th>
              <th className={th}>Bytes to planner</th>
              <th className={th} title="Graph tools: an output limit trimmed what they showed. Lookups: the call budget ended with accounts still queued.">
                Hit a limit
              </th>
            </tr>
          </thead>
          <tbody>
            {names.map((name) => {
              const s = strategies[name];
              return (
                <tr key={name} className="border-b border-zinc-800/60 last:border-0">
                  <td className={td}>
                    <div className="font-mono text-zinc-100">{name}</div>
                    <div className="max-w-64 text-[11px] text-zinc-500">{result.config.strategies[name]}</div>
                  </td>
                  <td className={`${td} font-mono text-zinc-100`}>{s.macro_recall.toFixed(3)}</td>
                  <td className={`${td} font-mono`}>
                    {s.found_any}/{s.instances}
                  </td>
                  <SpreadCell s={s.returned} />
                  <SpreadCell s={s.off_instance} />
                  <SpreadCell s={s.calls} />
                  <SpreadCell s={s.db_queries} />
                  <SpreadCell s={s.db_ms} digits={1} />
                  <SpreadCell s={s.bytes} />
                  <td className={`${td} font-mono`}>{s.with_caps}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-xs text-zinc-300">
          <thead className="border-b border-zinc-800 bg-zinc-900/60">
            <tr>
              <th className={th}>Pattern family</th>
              <th className={th}>Instances</th>
              {names.map((n) => (
                <th key={n} className={`${th} font-mono`}>
                  {n}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Object.entries(families).map(([family, f]) => {
              const best = Math.max(...names.map((n) => f[n].macro_recall));
              return (
                <tr key={family} className="border-b border-zinc-800/60 last:border-0">
                  <td className={`${td} font-mono text-zinc-100`}>{family.toLowerCase()}</td>
                  <td className={`${td} font-mono`}>{f.instances}</td>
                  {names.map((n) => (
                    <td key={n} className={`${td} font-mono ${f[n].macro_recall === best ? "text-zinc-100" : "text-zinc-500"}`}>
                      {f[n].macro_recall.toFixed(2)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-[11px] leading-relaxed text-zinc-500">
        Recall is the share of each instance's other members that appear in what the planner actually received, averaged over instances. The graph tools show
        the {limits.money_flow_top_per_hop} largest arrivals per hop and {limits.ring_members_shown} community members, lookups {limits.lookup_page_size}{" "}
        neighbours a page, and every result is cut at {limits.result_chars.toLocaleString()} characters. The seed is the {result.config.seed_rule}. This measures
        evidence retrieval from a known member, not detection from an alert.
      </p>
    </div>
  );
}

function Contention({ result }: { result: ContentionResult }) {
  const { config, summary } = result;
  const pct = (v: number) => `${Math.round(v * 100)}%`;
  const facts: [string, string, boolean?][] = [
    ["Invariant violations", String(summary.violations), summary.violations > 0],
    ["Late commits refused", String(summary.late_commits_refused)],
    ["Late commits accepted", String(summary.late_commits_accepted), summary.late_commits_accepted > 0],
    ["Crashed claims recovered", String(summary.crashed_claims_recovered)],
    ["Alerts per second", spread(summary.alerts_per_s, 1)],
    ["Final statuses", Object.entries(summary.statuses).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", ")],
  ];
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs leading-relaxed text-zinc-400">
        {summary.runs} runs of {config.claimants} hunters racing for {config.alerts} alerts with {config.lease_s}s leases and up to {config.max_attempts} attempts
        each. {pct(config.crash)} of claims crash without releasing, {pct(config.stall)} stall past their lease and then try to commit, and {pct(config.fail)}{" "}
        report a failure and are retried. <Command text="python -m backend.eval.contention" />
      </p>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
        {facts.map(([label, value, bad]) => (
          <div key={label} className="rounded-md border border-zinc-800 bg-zinc-900/60 px-3 py-2">
            <dt className="text-[11px] text-zinc-500">{label}</dt>
            <dd className={`font-mono text-sm ${bad ? "text-rose-300" : "text-zinc-100"}`}>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function EvalView({ active }: { active: boolean }) {
  const [results, setResults] = useState<EvalResults | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [planner, setPlanner] = useState<"llm" | "fallback">("llm");
  const [scope, setScope] = useState<"all" | "pilot">("all");

  const load = useCallback(() => {
    getJSON<EvalResults>("/api/eval")
      .then((r) => {
        setResults(r);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (active) load();
  }, [active, load]);

  if (!results) return <div className="p-5">{error ? <Empty>{error}</Empty> : <Empty>Loading the saved results…</Empty>}</div>;

  const planners = (["llm", "fallback"] as const).filter((p) => results.controlled?.[p]);
  const controlled = results.controlled?.[planners.includes(planner) ? planner : planners[0]];
  const scopes = (["all", "pilot"] as const).filter((s) => results.retrieval?.[s]);
  const retrieval = results.retrieval?.[scopes.includes(scope) ? scope : scopes[0]];

  return (
    <div className="flex flex-col gap-6 p-5">
      <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-500">
        <span>
          Saved results from <span className="font-mono">backend/eval/results.json</span>, updated {new Date(results.updated_at).toLocaleString()}. Failures are
          counted, not dropped, and each section names the command that reproduces it.
        </span>
        <button onClick={load} className={`${secondary} ml-auto`}>
          Reload
        </button>
      </div>

      <Section
        title="Controlled cases"
        right={
          planners.length > 1 && (
            <Toggle
              options={planners.map((p) => ({ id: p, label: p === "llm" ? "LLM planner" : "Deterministic planner" }))}
              value={planners.includes(planner) ? planner : planners[0]}
              onChange={setPlanner}
            />
          )
        }
      >
        <p className="text-xs leading-relaxed text-zinc-400">
          Sixteen labelled alerts on a seeded overlay outside the demo neighbourhood: seven positives with an eligible route to a sanctioned account within six
          hops, and nine look-alikes without one (IP-only links, money flowing the wrong way, routes longer than six hops). Then unknown IDs, runs starved of
          budget, and the two demo subjects. LLM and deterministic runs are reported separately. <Command text="python -m backend.eval.run_eval --planner llm --repeats 3" />
        </p>
        {controlled ? <Controlled result={controlled} /> : <Empty>No controlled runs saved yet.</Empty>}
      </Section>

      <Section
        title="IBM laundering patterns: agent-visible retrieval"
        right={
          scopes.length > 1 && (
            <Toggle
              options={scopes.map((s) => ({ id: s, label: s === "all" ? `All ${results.retrieval?.all?.config.instances}` : `Pilot ${results.retrieval?.pilot?.config.instances}` }))}
              value={scopes.includes(scope) ? scope : scopes[0]}
              onChange={setScope}
            />
          )
        }
      >
        <p className="text-xs leading-relaxed text-zinc-400">
          Starting from one member of each labelled laundering instance in the IBM dataset, how many of its other members does each strategy surface? The
          graph strategy follows money up to {retrieval?.config.hops ?? 4} hops each way plus the account's community (no hop limit); lookups go breadth-first
          up to the same {retrieval?.config.hops ?? 4} hops. <Command text="python -m backend.eval.retrieval" />
        </p>
        {retrieval ? <Retrieval result={retrieval} /> : <Empty>No retrieval runs saved yet.</Empty>}
      </Section>

      <Section title="Alert queue under contention">
        {results.contention ? <Contention result={results.contention} /> : <Empty>No contention runs saved yet.</Empty>}
      </Section>
    </div>
  );
}
