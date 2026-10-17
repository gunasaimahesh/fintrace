import { useState } from "react";
import { api, postJSON } from "../api";
import type { Board, FactRow, PlaybookRow } from "../types";
import { Empty, Section, Status, VerdictBadge, clock, field, secondary } from "./ui";

type Props = { tenant: string; board: Board | null; refresh: () => void };

type Mutation = { kind: string } & Record<string, string | number | boolean>;
type Applied = { label: string; rev: number; detail: string };

const PRESETS: { label: string; body: Mutation; explain: string }[] = [
  {
    label: "Add a $9,500 transfer Volkov → Baltic",
    body: { kind: "add_transfer", src: "acct-volkov-clearing", dst: "acct-baltic-trade", amount_usd: 9500 },
    explain: "Touches two accounts on NovaPay's route: route facts through them go stale, routes elsewhere stay current.",
  },
  {
    label: "Add a $9,500 transfer Harlan Seafood → acct-5131326",
    body: { kind: "add_transfer", src: "acct-9330192", dst: "acct-5131326", amount_usd: 9500 },
    explain: "Touches the route the hunters shared: every case built on it goes stale; other cases' routes stay current.",
  },
  {
    label: "Unlink Volkov from the shared device",
    body: { kind: "delete_identity_link", account: "acct-volkov-clearing", target: "dev-fp-8821", rel: "USED_DEVICE" },
    explain: "Removes the identity hop NovaPay's route crosses, so the remembered route no longer holds.",
  },
  {
    label: "Sanction Orion Logistics",
    body: { kind: "set_sanctioned", account: "acct-orion-logistics", sanctioned: true, list_name: "demo watchlist" },
    explain: "A new sanctioned account: findings of no route anywhere are re-checked, because the whole graph matters to them.",
  },
];

function showValue(fact: FactRow) {
  const v = fact.value;
  const text = typeof v === "number" ? (fact.unit?.startsWith("USD") ? `$${Math.round(v).toLocaleString()}` : String(v)) : JSON.stringify(v);
  const unit = fact.unit && !fact.unit.startsWith("USD") ? ` ${fact.unit}` : "";
  return `${text}${unit}`;
}

function FactsTable({ facts }: { facts: FactRow[] }) {
  if (!facts.length) return <Empty>No facts yet: investigate an alert or run the hunters.</Empty>;
  return (
    <div className="overflow-x-auto rounded-lg border border-zinc-800">
      <table className="w-full text-left text-xs">
        <thead className="bg-zinc-900 text-[11px] uppercase tracking-wider text-zinc-500">
          <tr>
            {["Status", "Subject", "Predicate", "Value", "Rev", "Why", "Recorded by"].map((h) => (
              <th key={h} className="px-2 py-1.5 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-800/70">
          {facts.map((f) => (
            <tr key={f.id} className="align-top" title={f.claim}>
              <td className="px-2 py-1.5">
                <Status status={f.status} title={f.status !== f.recorded_status ? `recorded ${f.recorded_status}; re-checked now` : undefined} />
              </td>
              <td className="max-w-44 truncate px-2 py-1.5 font-mono text-zinc-300" title={f.subject}>
                {f.subject}
              </td>
              <td className="px-2 py-1.5 font-mono text-zinc-400">
                {f.predicate}
                {f.object && <span className="text-zinc-500"> → {f.object}</span>}
              </td>
              <td className="max-w-40 truncate px-2 py-1.5 font-mono text-zinc-300">{showValue(f)}</td>
              <td className="px-2 py-1.5 text-zinc-400">{f.valid_rev}</td>
              <td className="max-w-80 px-2 py-1.5 text-zinc-400">
                {f.status === "STALE" && <span className="text-amber-200">{f.why_stale}</span>}
                {f.status === "SUPERSEDED" && <span>replaced by {f.superseded_by}</span>}
                {f.supersedes.length > 0 && <span className="text-emerald-300">supersedes {f.supersedes.join(", ")}</span>}
                {f.shared && (
                  <span className="text-sky-200">
                    {" "}
                    from {f.shared.worker_id}'s route via {f.shared.found_from} ({f.shared.episode_id})
                  </span>
                )}
              </td>
              <td className="whitespace-nowrap px-2 py-1.5 font-mono text-[11px] text-zinc-500">
                <span className="text-sky-300">{f.worker_id}</span> · {f.episode_id}
                <div>{f.case_id}</div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Playbooks({ playbooks }: { playbooks: PlaybookRow[] }) {
  if (!playbooks.length) return <Empty>A playbook is learned when a freeze request is approved: sign one off in the Approvals tab.</Empty>;
  return (
    <ul className="flex flex-col gap-2">
      {playbooks.map((p) => (
        <li key={p.id} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-xs text-zinc-300">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="font-mono text-sm text-zinc-100">{p.id}</span>
            <span className="text-zinc-500">{p.family}</span>
            <span className="ml-auto text-zinc-500">used {p.uses} times</span>
          </div>
          <div className="mt-1 font-mono text-[11px] text-sky-200/80">{p.steps.join(" → ")}</div>
          <div className="mt-1 text-[11px] text-zinc-500">matches entities with {p.required_patterns.join(", ")}</div>
          {p.learned_from.map((s) => (
            <div key={s.episode_id} className="mt-1">
              Learned from <span className="font-mono">{s.episode_id}</span> on <span className="font-mono">{s.subject_id}</span>, whose freeze{" "}
              <span className="font-mono">{s.request_id}</span> {s.approved_by} approved at {clock(s.at)}.
            </div>
          ))}
          {p.used_by.length > 0 && (
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              Applied to:
              {p.used_by.map((u) => (
                <span key={u.episode_id} className="inline-flex items-center gap-1 font-mono">
                  {u.subject_id} <VerdictBadge verdict={u.verdict} />
                </span>
              ))}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

export function MemoryView({ tenant, board, refresh }: Props) {
  const [applied, setApplied] = useState<Applied[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function mutate(label: string, body: Mutation) {
    setBusy(true);
    setError(null);
    try {
      const result = await postJSON<{ rev: number; tx_id?: string; neighbours_stamped?: number }>(api("/api/mutations", tenant), body);
      setApplied((prev) => [{ label, rev: result.rev, detail: result.tx_id ?? "" }, ...prev]);
      refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const counts = board?.facts.counts;
  const facts = board?.facts.recent ?? [];
  return (
    <div className="flex flex-col gap-5 p-5">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_420px]">
        <Section title="What memory holds" right={board && `graph revision ${board.rev}`}>
          <div className="flex flex-wrap gap-3">
            {(["CURRENT", "STALE", "SUPERSEDED"] as const).map((s) => (
              <div key={s} className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900/60 px-4 py-3">
                <span className="text-2xl font-semibold text-zinc-100">{counts?.[s] ?? 0}</span>
                <Status status={s} />
              </div>
            ))}
          </div>
          <p className="text-xs leading-relaxed text-zinc-500">
            Every fact records the graph revision it was read at and the nodes and edges it depends on. A write bumps the revision and stamps what it touched,
            so a route or money trail goes stale only when its own nodes or edges change. Results computed over a whole neighbourhood (flow capacity, rings,
            broker rankings) and findings of no route can be changed by a write anywhere, so any write re-opens them. Statuses here are re-checked on every
            read; an investigation that recalls a stale fact re-runs the call that produced it, and the new fact supersedes the old one.
          </p>
        </Section>
        <Section title="Change the graph" right="each write bumps the revision">
          <div className="flex flex-col gap-2">
            {PRESETS.map((p) => (
              <button key={p.label} className={`${secondary} text-left`} disabled={busy} title={p.explain} onClick={() => mutate(p.label, p.body)}>
                {p.label}
                <div className="mt-0.5 text-[11px] font-normal text-zinc-500">{p.explain}</div>
              </button>
            ))}
            <TransferForm busy={busy} onSubmit={(body, label) => mutate(label, body)} />
            {error && <div className="text-xs text-rose-300">{error}</div>}
            {applied.map((a, i) => (
              <div key={i} className="text-[11px] text-zinc-400">
                revision {a.rev}: {a.label} {a.detail && <span className="font-mono text-zinc-500">{a.detail}</span>}
              </div>
            ))}
          </div>
        </Section>
      </div>

      <Section title="Facts" right={`${facts.length} most recent · hover a row for the statement it backs`}>
        <FactsTable facts={facts} />
      </Section>

      <Section title="Playbooks" right="procedures learned from approved freezes, matched by topology">
        <Playbooks playbooks={board?.playbooks ?? []} />
      </Section>
    </div>
  );
}

function TransferForm({ busy, onSubmit }: { busy: boolean; onSubmit: (body: Mutation, label: string) => void }) {
  const [src, setSrc] = useState("");
  const [dst, setDst] = useState("");
  const [amount, setAmount] = useState(5000);
  return (
    <form
      className="flex flex-wrap items-center gap-1.5"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({ kind: "add_transfer", src: src.trim(), dst: dst.trim(), amount_usd: amount }, `$${amount.toLocaleString()} ${src.trim()} → ${dst.trim()}`);
      }}
    >
      <input className={`${field} w-36`} placeholder="from account" value={src} onChange={(e) => setSrc(e.target.value)} />
      <input className={`${field} w-36`} placeholder="to account" value={dst} onChange={(e) => setDst(e.target.value)} />
      <input className={`${field} w-20`} type="number" min={1} value={amount} onChange={(e) => setAmount(Number(e.target.value))} />
      <button className={secondary} disabled={busy || !src.trim() || !dst.trim() || amount <= 0}>
        Add transfer
      </button>
    </form>
  );
}
