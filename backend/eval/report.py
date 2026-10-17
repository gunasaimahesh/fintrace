"""Markdown tables from backend/eval/results.json, for the README.

    python -m backend.eval.report
"""

from __future__ import annotations

import json

from backend.eval.common import RESULTS

CONDITIONS = {
    "cold": "Cold: the agent on a fresh graph",
    "warm": "Warm: after learning the NovaPay playbook",
    "revisit": "Revisit: same subject, right after cold",
    "baseline": "Baseline: one-entity lookups, same budget",
}
GROUPS = ("positive", "negative", "unknown", "incomplete", "demo")


def _score(group: dict | None) -> str:
    return f"{group['correct']}/{group['runs']}" if group else "–"


def _spread(s: dict | None, digits: int = 0) -> str:
    if not s:
        return "–"
    f = (lambda v: f"{v:,.{digits}f}")
    return f(s["median"]) if s["min"] == s["max"] else f"{f(s['median'])} ({f(s['min'])}–{f(s['max'])})"


def controlled(result: dict) -> str:
    config, summary = result["config"], result["summary"]
    llm = config["planner"] == "llm"
    head = ["Condition", "Detection", "Positives", "Negatives", "Unknown IDs", "Out of budget", "Demo", "Tool calls",
            "Verdict at call"] + (["LLM requests", "LLM tokens"] if llm else []) + ["Failures"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for condition, label in CONDITIONS.items():
        c = summary["conditions"].get(condition)
        if not c:
            continue
        row = [label, _score(c["detection"]), *(_score(c["groups"].get(g)) for g in GROUPS),
               _spread(c["tool_calls"]), _spread(c["verdict_step"])]
        if llm:
            row += [_spread(c["llm_requests"]), _spread(c["llm_tokens"])]
        fell_back = c["planner_used"].get("llm+fallback", 0)
        row.append(str(len(c["failures"])) + (f" ({fell_back} fell back)" if fell_back else ""))
        lines.append("| " + " | ".join(row) + " |")
    repeats = config["repeats"]
    lines.append("")
    lines.append(f"{'`' + config['model'] + '`, temperature 0' if llm else 'Deterministic planner'}; repeats "
                 f"{repeats[0]}–{repeats[1]}; medians (ranges) over the detection runs; graph `{config['graph']}`.")
    return "\n".join(lines)


def playbook(result: dict) -> str:
    warm = result["summary"]["warm_vs_cold"]
    lines = ["| Case | Correct | Tool calls (cold → warm) | LLM requests | LLM tokens |", "|---|---|---|---|---|"]
    for case_id in warm["playbook_used_on"]:
        d = warm["cases"][case_id]
        cell = (lambda key: f"{d[key]['cold']:,.0f} → {d[key]['warm']:,.0f}" if key in d else "–")
        lines.append(f"| {case_id} | {d['correct']} | {cell('tool_calls')} | {cell('llm_requests')} | "
                     f"{cell('llm_tokens')} |")
    return "\n".join(lines)


def retrieval(result: dict) -> str:
    strategies, families = result["summary"]["strategies"], result["summary"]["families"]
    lines = ["| Strategy | Recall | Found any | IDs returned | Not in the instance | Calls | DB queries | DB ms |",
             "|---|---|---|---|---|---|---|---|"]
    for name, s in strategies.items():
        lines.append(f"| `{name}`: {result['config']['strategies'][name]} | {s['macro_recall']:.3f} | "
                     f"{s['found_any']}/{s['instances']} | {_spread(s['returned'])} | {_spread(s['off_instance'])} | "
                     f"{_spread(s['calls'])} | {_spread(s['db_queries'])} | {_spread(s['db_ms'], 1)} |")
    names = list(strategies)
    lines += ["", "| Pattern family | Instances | " + " | ".join(f"`{n}`" for n in names) + " |",
              "|---|---|" + "---|" * len(names)]
    for family, f in families.items():
        best = max(f[n]["macro_recall"] for n in names)
        cells = [(f"**{f[n]['macro_recall']:.2f}**" if f[n]["macro_recall"] == best else f"{f[n]['macro_recall']:.2f}")
                 for n in names]
        lines.append(f"| {family.lower()} | {f['instances']} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def contention(result: dict) -> str:
    c, s = result["config"], result["summary"]
    statuses = ", ".join(f"{v} {k.lower()}" for k, v in s["statuses"].items())
    return (f"{s['runs']} runs of {c['claimants']} hunters racing for {c['alerts']} alerts ({c['lease_s']} s leases, "
            f"{c['crash']:.0%} of claims crash, {c['stall']:.0%} stall past their lease, {c['fail']:.0%} fail): "
            f"**{s['violations']} invariant violations**, {s['late_commits_refused']} late commits refused, "
            f"{s['late_commits_accepted']} accepted, {s['crashed_claims_recovered']} crashed claims recovered, "
            f"{_spread(s['alerts_per_s'], 1)} alerts/s; final statuses: {statuses}.")


def main() -> None:
    results = json.loads(RESULTS.read_text())
    for planner in ("llm", "fallback"):
        if result := results.get("controlled", {}).get(planner):
            print(f"### Controlled cases: {'LLM' if planner == 'llm' else 'deterministic'} planner\n")
            print(controlled(result) + "\n")
            if planner == "llm" and result["summary"]["warm_vs_cold"]["playbook_used_on"]:
                print(playbook(result) + "\n")
    if result := results.get("retrieval", {}).get("all"):
        print("### IBM retrieval\n")
        print(retrieval(result) + "\n")
    if result := results.get("contention"):
        print("### Contention\n")
        print(contention(result))


if __name__ == "__main__":
    main()
