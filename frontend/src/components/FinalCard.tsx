import { useState } from "react";
import { postJSON } from "../api";
import type { ApprovalResult, FinalEvent, Verdict } from "../types";

const VERDICT_STYLE: Record<Verdict, string> = {
  SUSPICIOUS: "bg-rose-500/15 text-rose-300 ring-rose-500/40",
  CLEAR: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40",
  INSUFFICIENT: "bg-zinc-500/15 text-zinc-300 ring-zinc-500/40",
};

const STATE_LABEL: Record<string, string> = {
  answered_with_path: "Answered with a graph path",
  answered_from_memory: "Answered from memory",
  memory_stale_reinvestigated: "Memory was stale, so it re-investigated",
  not_in_graph: "No exposure recorded in the graph",
  unknown_entity: "Entity is not in the graph",
};

function plannerLabel(final: FinalEvent) {
  if (final.planner === "llm") return `LLM planner (${final.model})`;
  if (final.planner === "llm+fallback") return "LLM, then deterministic planner";
  return "deterministic planner";
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

export function FinalCard({ final }: { final: FinalEvent }) {
  const [approval, setApproval] = useState<ApprovalResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const request = final.approval_request;
  const check = final.verification;

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
        <span className="text-sm text-zinc-300">{STATE_LABEL[final.answer_state] ?? final.answer_state}</span>
      </div>
      <div className="text-xs text-zinc-500">
        {final.tool_calls} of {final.max_steps} tool calls · {(final.elapsed_ms / 1000).toFixed(2)} s ·{" "}
        {plannerLabel(final)}
        {final.coverage && ` · ${final.coverage.entities_checked.toLocaleString()} entities checked at depth ${final.coverage.depth}`}
      </div>

      <ul className="flex list-disc flex-col gap-1.5 pl-4 text-sm leading-relaxed text-zinc-200 marker:text-zinc-600">
        {final.summary
          .split(/(?<=[.!?])\s+/)
          .filter(Boolean)
          .map((sentence, i) => (
            <li key={i}>{sentence}</li>
          ))}
      </ul>

      <div
        className={`rounded px-2 py-1 text-xs ring-1 ${check.verified ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30" : "bg-amber-500/10 text-amber-200 ring-amber-500/30"}`}
      >
        {check.verified ? "✓ " : "↺ "}
        {check.badge}
      </div>

      {final.claims.length > 0 && (
        <details>
          <summary className="text-xs text-zinc-500 hover:text-zinc-300">
            {final.claims.length} claims, each matched to an edge a tool returned
          </summary>
          <ul className="mt-1 flex flex-col gap-0.5 font-mono text-[11px] text-zinc-300">
            {final.claims.map((claim) => (
              <li key={`${claim.subject}-${claim.relation}-${claim.object}`}>
                <span className="text-emerald-400">✓</span> {claim.subject}{" "}
                <span className="text-zinc-500">-[{claim.relation}]→</span> {claim.object}
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
            <span className="text-zinc-200">{request.approver.name}</span> ({request.approver.clearance}, on call)
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
          ) : (
            <button
              onClick={signOff}
              disabled={busy}
              className="self-start rounded bg-rose-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-rose-500 disabled:opacity-50"
            >
              {busy ? "Signing off…" : `Sign off as ${request.approver.name} (${request.approver.clearance})`}
            </button>
          )}
          {error && <div className="text-xs text-rose-400">{error}</div>}
        </div>
      )}
    </section>
  );
}
