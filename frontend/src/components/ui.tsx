import type { ReactNode } from "react";
import type { Verdict } from "../types";

export const VERDICT_STYLE: Record<Verdict, string> = {
  SUSPICIOUS: "bg-rose-500/15 text-rose-300 ring-rose-500/40",
  CLEAR: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40",
  INSUFFICIENT: "bg-zinc-500/15 text-zinc-300 ring-zinc-500/40",
};

const STATUS_STYLE: Record<string, string> = {
  PENDING: "text-zinc-300 ring-zinc-600",
  CLAIMED: "text-sky-300 ring-sky-500/50",
  COMPLETED: "text-emerald-300 ring-emerald-500/40",
  FAILED: "text-rose-300 ring-rose-500/50",
  EXPIRED: "text-amber-300 ring-amber-500/50",
  CURRENT: "text-emerald-300 ring-emerald-500/40",
  STALE: "text-amber-300 ring-amber-500/50",
  SUPERSEDED: "text-zinc-400 ring-zinc-600",
  PENDING_L3: "text-sky-300 ring-sky-500/50",
  APPROVED: "text-emerald-300 ring-emerald-500/40",
  OPEN: "text-zinc-200 ring-zinc-600",
  MERGED: "text-violet-300 ring-violet-500/50",
};

export function VerdictBadge({ verdict }: { verdict: Verdict | null }) {
  if (!verdict) return null;
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ring-1 ${VERDICT_STYLE[verdict]}`}>{verdict}</span>;
}

export function Status({ status, title }: { status: string; title?: string }) {
  return (
    <span title={title} className={`rounded px-1.5 py-0.5 font-mono text-[10px] ring-1 ${STATUS_STYLE[status] ?? "text-zinc-300 ring-zinc-700"}`}>
      {status}
    </span>
  );
}

export function Id({ id, tone = "plain" }: { id: string; tone?: "plain" | "freeze" | "spare" }) {
  const style = { plain: "text-zinc-300 ring-zinc-700", freeze: "text-rose-200 ring-rose-500/40", spare: "text-emerald-200 ring-emerald-500/40" }[tone];
  return <span className={`rounded px-1 py-0.5 font-mono text-[11px] ring-1 ${style}`}>{id}</span>;
}

export function Ids({ ids, tone }: { ids: string[]; tone?: "plain" | "freeze" | "spare" }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {ids.map((id) => (
        <Id key={id} id={id} tone={tone} />
      ))}
    </span>
  );
}

export function Section({ title, right, children }: { title: ReactNode; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="flex min-w-0 flex-col gap-2">
      <h2 className="flex items-baseline gap-2 text-xs font-medium uppercase tracking-wider text-zinc-500">
        {title}
        {right && <span className="ml-auto font-normal normal-case tracking-normal text-zinc-500">{right}</span>}
      </h2>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-md border border-dashed border-zinc-800 px-3 py-4 text-center text-xs text-zinc-500">{children}</div>;
}

export function clock(seconds: number | null | undefined) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export const button =
  "rounded px-2.5 py-1.5 text-xs font-medium ring-1 transition disabled:cursor-not-allowed disabled:opacity-40";
export const primary = `${button} bg-sky-600 text-white ring-sky-500 hover:bg-sky-500`;
export const secondary = `${button} bg-zinc-900 text-zinc-300 ring-zinc-700 hover:text-white`;
export const danger = `${button} bg-rose-600 text-white ring-rose-500 hover:bg-rose-500`;
export const field = "rounded bg-zinc-900 px-2 py-1.5 font-mono text-xs text-zinc-200 outline-none ring-1 ring-zinc-800 focus:ring-sky-500";
