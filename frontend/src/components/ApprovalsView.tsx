import { useState } from "react";
import { api, postJSON } from "../api";
import type { ApprovalResult, ApprovalRow, Board } from "../types";
import { Empty, Ids, Section, Status, clock, danger } from "./ui";

type Props = { tenant: string; board: Board | null; refresh: () => void };

type RequestProps = { request: ApprovalRow; tenant: string; refresh: () => void; onActed: (id: string) => void };

function Request({ request, tenant, refresh, onActed }: RequestProps) {
  const [result, setResult] = useState<ApprovalResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const approver = request.approver;

  async function signOff() {
    if (!approver) return;
    setBusy(true);
    setError(null);
    onActed(request.id);
    try {
      setResult(await postJSON<ApprovalResult>(api(`/api/approvals/${request.id}/approve`, tenant), { approver_id: approver.id }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      refresh();
    }
  }

  return (
    <li className="flex flex-col gap-2 rounded-lg border border-zinc-800 bg-zinc-900/60 p-4 text-xs text-zinc-300">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm text-zinc-100">{request.id}</span>
        <Status status={request.status} />
        <span className="text-zinc-500">{request.action_type}</span>
        {request.case && (
          <span className="font-mono text-zinc-400">
            {request.case.id} <span className="text-zinc-600">({request.case.status.toLowerCase()})</span>
          </span>
        )}
        <span className="ml-auto text-zinc-500">
          filed by <span className="font-mono text-sky-300">{request.requested_by}</span> at {clock(request.created_at)} · evidence at revision{" "}
          {request.evidence_rev}
        </span>
      </div>
      <p className="leading-relaxed">{request.reason}</p>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] uppercase tracking-wider text-zinc-500">Freeze</span>
        <Ids ids={request.target_ids} tone="freeze" />
      </div>
      {request.excluded_ids.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] uppercase tracking-wider text-zinc-500">Leave untouched</span>
          <Ids ids={request.excluded_ids} tone="spare" />
        </div>
      )}
      <div className="text-zinc-400">
        Under <span className="text-zinc-200">{request.policy?.title ?? request.policy?.id}</span>, which requires {request.required_clearance}; routed to{" "}
        <span className="text-zinc-200">{approver?.name}</span> ({approver?.title}, {approver?.clearance}).
      </div>
      {request.status === "STALE" && <div className="text-amber-200">Refused at sign-off: {request.stale_reason}. Re-investigate before freezing.</div>}
      {request.status === "APPROVED" && !result && <div className="text-emerald-300">Approved by {request.approved_by}.</div>}
      {result && (
        <div className="text-emerald-300">
          Approved: {result.frozen.length} accounts frozen{result.spared.length > 0 && `, ${result.spared.length} spared`}.
          {result.playbook ? (
            <>
              {" "}
              Learned playbook <span className="font-mono">{result.playbook.id}</span> ({result.playbook.steps.length} steps) from this run; it now
              applies to alerts with the same topology.
            </>
          ) : (
            <span className="text-zinc-500">
              {" "}
              No playbook learned: playbooks cover device-linked exposure, and neither this subject nor its direct counterparties share a device.
            </span>
          )}
        </div>
      )}
      {error && request.status !== "STALE" && <div className="text-rose-300">{error}</div>}
      {request.status === "PENDING_L3" && !result && approver && (
        <button onClick={signOff} disabled={busy} className={`${danger} self-start`}>
          {busy ? "Signing off…" : `Sign off as ${approver.name} (${approver.clearance})`}
        </button>
      )}
    </li>
  );
}

export function ApprovalsView({ tenant, board, refresh }: Props) {
  const [pendingOnly, setPendingOnly] = useState(true);
  const [acted, setActed] = useState<Set<string>>(new Set());
  const all = board?.approvals ?? [];
  const shown = pendingOnly ? all.filter((r) => r.status === "PENDING_L3" || acted.has(r.id)) : all;
  return (
    <div className="flex flex-col gap-4 p-5">
      <Section
        title="Approval inbox"
        right={
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={pendingOnly} onChange={(e) => setPendingOnly(e.target.checked)} />
            pending only ({all.filter((r) => r.status === "PENDING_L3").length} of {all.length})
          </label>
        }
      >
        <p className="text-xs leading-relaxed text-zinc-500">
          Agents never freeze anything. They file a request under the freeze policy, routed to an on-call approver with the clearance it requires. Sign-off
          re-checks the evidence the request was filed on: if any of it changed since, the request goes stale instead of freezing.
        </p>
        {shown.length ? (
          <ul className="flex flex-col gap-3">
            {shown.map((r) => (
              <Request key={r.id} request={r} tenant={tenant} refresh={refresh} onActed={(id) => setActed((prev) => new Set(prev).add(id))} />
            ))}
          </ul>
        ) : (
          <Empty>{pendingOnly ? "Nothing awaits sign-off." : "No requests yet."}</Empty>
        )}
      </Section>
    </div>
  );
}
