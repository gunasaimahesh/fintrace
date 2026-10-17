import { useState } from "react";
import { postJSON } from "../api";
import type { ApprovalResult, Fact, FinalEvent, ModelAnswer, SearchScope, Verdict } from "../types";

const VERDICT_STYLE: Record<Verdict, string> = {
  SUSPICIOUS: "bg-rose-500/15 text-rose-300 ring-rose-500/40",
  CLEAR: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40",
  INSUFFICIENT: "bg-zinc-500/15 text-zinc-300 ring-zinc-500/40",
};

const STATE_LABEL: Record<string, string> = {
  answered_with_path: "Eligible route to a sanctioned account",
  answered_from_memory: "Answered from memory",
  memory_stale_reinvestigated: "Memory was stale, so it re-investigated",
  unknown_entity: "Entity is not in the graph",
};

function stateLabel(final: FinalEvent) {
  if (final.answer_state === "not_in_graph") {
    return final.verdict === "CLEAR" ? "No eligible route within the stated scope" : "Not enough coverage to conclude";
  }
  return STATE_LABEL[final.answer_state] ?? final.answer_state;
}

function plannerLabel(final: FinalEvent) {
  if (final.planner === "llm") return `LLM planner (${final.model})`;
  if (final.planner === "llm+fallback") return "LLM, then deterministic planner";
  return "deterministic planner";
}

function coverageLabel(scope: SearchScope) {
  const how = scope.complete
    ? scope.exhausted
      ? "everything reachable"
      : "complete"
    : scope.truncated
      ? "stopped at the node budget"
      : scope.timed_out
        ? "timed out"
        : "incomplete";
  return `eligible-route search ${scope.completed_depth} of ${scope.requested_depth} hops, ${scope.reached.toLocaleString()} entities, ${how}`;
}

function citation(fact: Fact) {
  const where = fact.source.step === 0 ? "re-read from the graph" : `step ${fact.source.step}`;
  return `${fact.source.tool} (${where}) → ${fact.source.result_key}`;
}

function showValue(fact: Fact) {
  if (typeof fact.value === "number" && fact.unit?.startsWith("USD")) {
    return `$${Math.round(fact.value).toLocaleString()}${fact.unit === "USD/month" ? "/month" : ""}`;
  }
  return `${String(fact.value)}${fact.unit && !fact.unit.startsWith("USD") ? ` ${fact.unit}` : ""}`;
}

function Chips({ ids, tone }: { ids: string[]; tone: "freeze" | "spare" }) {
  const style = tone === "freeze" ? "ring-rose-500/40 text-rose-200" : "ring-emerald-500/40 text-emerald-200";
  return (
    <div className="flex flex-wrap gap-1">
      {ids.map((id) => (
        <span key={id} className={`rounded px-1.5 py-0.5 font-mono text-[11px] ring-1 ${style}`}>
          {id}
        </span>
      ))}
    </div>
  );
}

function ModelProposal({ model }: { model: ModelAnswer }) {
  const bound = model.statements.filter((s) => s.status === "bound").length;
  const rejected = model.statements.flatMap((s) => (s.status === "rejected" ? [s] : []));
  const check = model.rationale_check;
  return (
    <details className="rounded-md border border-zinc-800 bg-zinc-950/60 p-3">
      <summary className="text-xs text-zinc-400 hover:text-zinc-200">
        Model's proposal: {model.proposed_verdict ?? "no verdict"}
        {model.proposed_verdict && (model.agrees ? " (matches the rule)" : " (overruled by the eligible-route rule)")}
        {" · "}
        {bound} of {model.statements.length} statements bound to facts
      </summary>
      <div className="mt-2 flex flex-col gap-2 text-xs">
        {model.rationale && (
          <div>
            <div className="text-[11px] uppercase tracking-wider text-zinc-500">Rationale (unverified)</div>
            <p className="italic text-zinc-400">{model.rationale}</p>
          </div>
        )}
        {check.unknown_ids.length > 0 && (
          <div className="text-amber-200">Mentions IDs no tool returned: {check.unknown_ids.join(", ")}</div>
        )}
        {check.rejected_claims.map((r, i) => (
          <div key={i} className="text-amber-200">
            Claimed edge {JSON.stringify(r.claim)}: {r.reason}
          </div>
        ))}
        {rejected.length > 0 && (
          <ul className="flex flex-col gap-1">
            {rejected.map((r, i) => (
              <li key={i} className="text-amber-200">
                <span className="font-mono text-zinc-400">{JSON.stringify(r.statement)}</span> rejected: {r.reason}
              </li>
            ))}
          </ul>
        )}
      </div>
    </details>
  );
}

export function FinalCard({ final }: { final: FinalEvent }) {
  const [approval, setApproval] = useState<ApprovalResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const request = final.approval_request;
  const facts = new Map(final.facts.map((f) => [f.id, f]));

  async function signOff() {
    if (!request) return;
    setBusy(true);
    setError(null);
    try {
      setApproval(
        await postJSON<ApprovalResult>(`/api/approvals/${request.request_id}/approve`, {
          approver_id: request.approver.id,
        }),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded px-2 py-0.5 text-sm font-semibold ring-1 ${VERDICT_STYLE[final.verdict]}`}>
          {final.verdict}
        </span>
        <span className="text-sm text-zinc-300">{stateLabel(final)}</span>
      </div>
      <div className="text-xs text-zinc-500">
        {final.tool_calls} of {final.max_steps} tool calls · {(final.elapsed_ms / 1000).toFixed(2)} s ·{" "}
        {plannerLabel(final)}
        {final.coverage && ` · ${coverageLabel(final.coverage.eligible)}`}
        {final.evidence_rev !== null && ` · graph revision ${final.evidence_rev}`}
      </div>

      <ul className="flex list-disc flex-col gap-1.5 pl-4 text-sm leading-relaxed text-zinc-200 marker:text-zinc-600">
        {final.statements.map((statement, i) => (
          <li key={i}>
            {statement.text}{" "}
            {statement.facts.map((id) => {
              const fact = facts.get(id);
              return (
                <span
                  key={id}
                  title={fact ? citation(fact) : id}
                  className={`ml-0.5 cursor-help rounded px-1 font-mono text-[10px] ring-1 ${final.verdict_basis.includes(id) ? "text-sky-200 ring-sky-500/50" : "text-zinc-400 ring-zinc-700"}`}
                >
                  {id}
                </span>
              );
            })}
          </li>
        ))}
      </ul>

      <div className="rounded bg-emerald-500/10 px-2 py-1 text-xs text-emerald-300 ring-1 ring-emerald-500/30">
        ✓ {final.badge}
      </div>

      {final.model_answer && <ModelProposal model={final.model_answer} />}

      <details>
        <summary className="text-xs text-zinc-500 hover:text-zinc-300">
          {final.facts.length} facts, each from one tool result
        </summary>
        <ul className="mt-1 flex flex-col gap-0.5 font-mono text-[11px] text-zinc-300">
          {final.facts.map((fact) => (
            <li key={fact.id} title={citation(fact)}>
              <span className="text-zinc-500">{fact.id}</span> {fact.subject}{" "}
              <span className="text-zinc-500">{fact.predicate}</span>
              {fact.object && ` ${fact.object}`} = {showValue(fact)}
              <span className="text-zinc-600"> · {fact.source.tool}</span>
            </li>
          ))}
        </ul>
      </details>

      {final.claims.length > 0 && (
        <details>
          <summary className="text-xs text-zinc-500 hover:text-zinc-300">
            {final.claims.length} graph edges behind these statements
          </summary>
          <ul className="mt-1 flex flex-col gap-0.5 font-mono text-[11px] text-zinc-300">
            {final.claims.map((claim) => (
              <li key={`${claim.subject}-${claim.relation}-${claim.object}`}>
                {claim.subject} <span className="text-zinc-500">-[{claim.relation}]→</span> {claim.object}
              </li>
            ))}
          </ul>
        </details>
      )}

      {request && (
        <div className="flex flex-col gap-2 rounded-md border border-zinc-800 bg-zinc-950/60 p-3">
          <div className="text-xs text-zinc-400">
            Freeze request <span className="font-mono text-zinc-200">{request.request_id}</span> under{" "}
            <span className="font-mono">{request.policy_id}</span>, routed to{" "}
            <span className="text-zinc-200">{request.approver.name}</span> ({request.approver.clearance}, on call) ·
            status <span className="font-mono text-zinc-200">{approval?.status ?? request.status}</span>
          </div>
          <div className="text-[11px] uppercase tracking-wider text-zinc-500">Freeze</div>
          <Chips ids={request.target_ids} tone="freeze" />
          {request.excluded_ids.length > 0 && (
            <>
              <div className="text-[11px] uppercase tracking-wider text-zinc-500">Leave untouched</div>
              <Chips ids={request.excluded_ids} tone="spare" />
            </>
          )}
          {approval ? (
            <div className="text-xs text-emerald-300">
              Approved by {request.approver.name}: {approval.frozen.length} accounts frozen,{" "}
              {approval.spared.join(", ") || "nothing"} spared.
            </div>
          ) : request.status === "PENDING_L3" ? (
            <button
              onClick={signOff}
              disabled={busy}
              className="self-start rounded bg-rose-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-rose-500 disabled:opacity-50"
            >
              {busy ? "Signing off…" : `Sign off as ${request.approver.name} (${request.approver.clearance})`}
            </button>
          ) : null}
          {error && <div className="text-xs text-rose-400">{error}</div>}
        </div>
      )}
    </section>
  );
}
