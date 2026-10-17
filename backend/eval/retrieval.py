"""IBM retrieval benchmark: starting from one member of each labelled laundering instance, how many of the
instance's other accounts reach the planner. Deterministic strategies, no LLM.

Strategies, all on the pristine graph:
- graph: time-ordered money flow out and in (trace_money_flow, 4 hops) and the account's community
  (detect_ring), 3 calls through the agent's own tools.
- lookup@3 and lookup@14: one-entity lookups (lookup.py), breadth-first over transfers in both directions up to
  the same 4 hops, at the graph strategy's 3 calls and at the agent's full budget.

Scored on agent-visible retrieval: account IDs in the results as the planner receives them, after the tools'
output limits and the agent's result cap. For instance i with labelled members G_i and seed s_i, and R_i the
accounts returned:

    recall_i = |(R_i - {s_i}) & (G_i - {s_i})| / |G_i - {s_i}|

Every instance has at least two members, so the denominator is never zero. Returned accounts outside the instance
are counted as off-instance, not as false positives: they may belong to another instance or be legitimately
connected. This measures evidence retrieval from a known member, not detection of laundering from an alert.

Seeds: the member with the smallest sha256(instance_id + account_id). Pilot: per family, the 5 instances with the
smallest sha256(instance_id). Labels stay in this evaluator.

    python -m backend.eval.retrieval --pilot      # 40 instances
    python -m backend.eval.retrieval              # all 370
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import statistics
import time
from collections import deque
from pathlib import Path

from backend.app import agent, tools
from backend.app.db import get_db
from backend.app.tools import ToolContext
from backend.eval import lookup
from backend.eval.common import EVAL_DIR, now, provenance, save, sha256, source_graph, stats

INSTANCES = EVAL_DIR / "instances.csv"
HOPS = 4
PILOT_PER_FAMILY = 5
QUOTED_ACCOUNT = re.compile(r'"((?:acct|merch)-[^"]+)"')
STRATEGIES = ("graph", "lookup@3", "lookup@14")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def instances(path: Path = INSTANCES) -> list[dict]:
    rows = list(csv.DictReader(path.open()))
    return [{"instance_id": r["instance_id"], "family": r["pattern_type"], "detail": r["pattern_detail"],
             "members": r["accounts"].split("|"),
             "seed": min(r["accounts"].split("|"), key=lambda a, i=r["instance_id"]: _digest(i + a))} for r in rows]


def pilot(chosen: list[dict]) -> list[dict]:
    by_family: dict[str, list[dict]] = {}
    for inst in sorted(chosen, key=lambda i: _digest(i["instance_id"])):
        by_family.setdefault(inst["family"], []).append(inst)
    return [inst for family in sorted(by_family) for inst in by_family[family][:PILOT_PER_FAMILY]]


def visible_accounts(texts: list[str]) -> set[str]:
    """Complete account IDs in what the planner received; an ID cut off by the result cap doesn't count."""
    return {m for text in texts for m in QUOTED_ACCOUNT.findall(text)}


def graph_strategy(graph, seed: str) -> dict:
    ctx = ToolContext(graph, worker_id="eval-retrieval")
    calls = [("trace_money_flow", {"entity_id": seed, "direction": "out", "max_hops": HOPS}),
             ("trace_money_flow", {"entity_id": seed, "direction": "in", "max_hops": HOPS}),
             ("detect_ring", {"entity_id": seed})]
    envelopes = [tools.run_tool(ctx, name, args) for name, args in calls]
    caps = []
    for e in envelopes:
        data = e["data"] or {}
        if data.get("capped_hops"):
            caps.append(f"{e['tool']} {e['args']['direction']}: hops kept only the largest arrivals")
        if e["tool"] == "detect_ring" and data and len(data["members"]) < data["community_size"]:
            caps.append(f"detect_ring: {len(data['members'])} of {data['community_size']} members shown")
    texts = [agent.for_model(e) for e in envelopes]
    return {"envelopes": envelopes, "texts": texts, "caps": caps,
            "truncated": sum(t.endswith("...(truncated)") for t in texts),
            "edges": sum(len(e["evidence"]["edges"]) for e in envelopes)}


def lookup_strategy(graph, seed: str, budget: int) -> dict:
    """Breadth-first over transfers in both directions, one page per call, up to HOPS from the seed."""
    envelopes: list[dict] = []
    hops, queue, pages = {seed: 0}, deque([seed]), {}
    while queue and len(envelopes) < budget:
        node = queue[0]
        page = pages.get(node, 0)
        envelope = lookup.lookup(graph, node, "both", page)
        envelopes.append(envelope)
        data = envelope["data"]
        if data:
            for row in data["rows"]:
                if row["rel"] == "FLOWS_TO" and row["id"] not in hops:
                    hops[row["id"]] = hops[node] + 1
                    if hops[row["id"]] < HOPS:
                        queue.append(row["id"])
        if data and data["has_more"]:
            pages[node] = page + 1
        else:
            queue.popleft()
    caps = [f"budget of {budget} calls ended with {len(queue)} accounts still to look up"] if queue else []
    texts = [lookup.for_model(e) for e in envelopes]
    return {"envelopes": envelopes, "texts": texts, "caps": caps,
            "truncated": sum(t.endswith("...(truncated)") for t in texts),
            "edges": sum(e["meta"]["rows"] for e in envelopes)}


def measure(inst: dict, outcome: dict, seconds: float) -> dict:
    members, seed = set(inst["members"]), inst["seed"]
    returned = visible_accounts(outcome["texts"]) - {seed}
    found = returned & (members - {seed})
    queries = [x for e in outcome["envelopes"] for x in e["meta"]["queries"]]
    return {"recall": round(len(found) / len(members - {seed}), 4), "found": len(found),
            "others": len(members - {seed}), "returned": len(returned), "off_instance": len(returned - members),
            "calls": len(outcome["envelopes"]), "failures": [e["error"] for e in outcome["envelopes"] if not e["ok"]],
            "caps": outcome["caps"], "truncated_results": outcome["truncated"],
            "db": {"queries": sum(x["calls"] for x in queries), "ms": round(sum(x["ms"] for x in queries), 1),
                   "edges": outcome["edges"], "bytes": sum(len(t.encode()) for t in outcome["texts"])},
            "seconds": round(seconds, 3)}


def summarize(rows: list[dict]) -> dict:
    out = {}
    for strategy in STRATEGIES:
        mine = [r[strategy] for r in rows]
        out[strategy] = {
            "instances": len(mine),
            "macro_recall": round(statistics.fmean(m["recall"] for m in mine), 4),
            "recall": stats([m["recall"] for m in mine]),
            "found_any": sum(m["found"] > 0 for m in mine),
            "returned": stats([m["returned"] for m in mine]),
            "off_instance": stats([m["off_instance"] for m in mine]),
            "calls": stats([m["calls"] for m in mine]),
            "db_queries": stats([m["db"]["queries"] for m in mine]),
            "db_ms": stats([m["db"]["ms"] for m in mine]),
            "bytes": stats([m["db"]["bytes"] for m in mine]),
            "with_caps": sum(bool(m["caps"]) for m in mine),
            "truncated_results": sum(m["truncated_results"] for m in mine),
            "failures": sum(bool(m["failures"]) for m in mine),
        }
    families = {}
    for family in sorted({r["family"] for r in rows}):
        mine = [r for r in rows if r["family"] == family]
        families[family] = {"instances": len(mine), **{
            strategy: {"macro_recall": round(statistics.fmean(r[strategy]["recall"] for r in mine), 4),
                       "found_any": f"{sum(r[strategy]['found'] > 0 for r in mine)}/{len(mine)}",
                       "found": sum(r[strategy]["found"] for r in mine),
                       "others": sum(r[strategy]["others"] for r in mine)}
            for strategy in STRATEGIES}}
    return {"strategies": out, "families": families}


def run(chosen: list[dict], source: str) -> list[dict]:
    graph = get_db().select_graph(source)
    rows = []
    for n, inst in enumerate(chosen, 1):
        row = {k: inst[k] for k in ("instance_id", "family", "detail", "seed")} | {"members": len(inst["members"])}
        for strategy in STRATEGIES:
            started = time.perf_counter()
            outcome = (graph_strategy(graph, inst["seed"]) if strategy == "graph"
                       else lookup_strategy(graph, inst["seed"], int(strategy.split("@")[1])))
            row[strategy] = measure(inst, outcome, time.perf_counter() - started)
        rows.append(row)
        print(f"{n:>3}/{len(chosen)} {inst['instance_id']:<20} "
              + "  ".join(f"{s} {row[s]['found']}/{row[s]['others']}" for s in STRATEGIES), flush=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pilot", action="store_true", help=f"{PILOT_PER_FAMILY} instances per family")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    source = source_graph()
    everything = instances()
    chosen = pilot(everything) if args.pilot else everything
    seeds = {i["instance_id"]: i["seed"] for i in chosen}
    print(f"{len(chosen)} instances; seeds sha256 {sha256(repr(sorted(seeds.items())))[:16]}")
    started_at, started = now(), time.perf_counter()
    rows = run(chosen, source)
    result = {
        "started_at": started_at, "seconds": round(time.perf_counter() - started, 1),
        "config": {"scope": "pilot" if args.pilot else "all", "instances": len(chosen), "hops": HOPS,
                   "strategies": {"graph": "trace_money_flow out + in (max_hops 4), detect_ring: 3 calls",
                                  "lookup@3": "lookup_entity breadth-first over transfers both ways, 3 calls",
                                  "lookup@14": "the same, 14 calls (the agent's budget)"},
                   "output_limits": {"result_chars": agent.RESULT_CHARS, "money_flow_top_per_hop": 3,
                                     "ring_members_shown": tools.MEMBER_SAMPLE, "lookup_page_size": lookup.PAGE_SIZE},
                   "seed_rule": "member with the smallest sha256(instance_id + account_id)",
                   "pilot_rule": f"per family, the {PILOT_PER_FAMILY} instances with the smallest sha256(instance_id)",
                   "recall": "|(R_i - {s_i}) & (G_i - {s_i})| / |G_i - {s_i}|, over agent-visible account IDs",
                   "instances_sha256": sha256(INSTANCES.read_text()), **provenance(source)},
        "seeds": seeds, "summary": summarize(rows), "instances": rows,
    }
    if not args.no_save:
        save(["retrieval", "pilot" if args.pilot else "all"], result)
    for strategy, s in result["summary"]["strategies"].items():
        print(f"{strategy:<10} macro recall {s['macro_recall']:.3f}, found any {s['found_any']}/{s['instances']}, "
              f"median returned {s['returned']['median']}, median off-instance {s['off_instance']['median']}, "
              f"median DB queries {s['db_queries']['median']}, failures {s['failures']}")


if __name__ == "__main__":
    main()
