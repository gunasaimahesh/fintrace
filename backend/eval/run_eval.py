"""Controlled evaluation: the challenge cases in backend/eval/cases.json, two unknown IDs and two deliberately
incomplete runs, under four conditions, each trial on a fresh copy of the pristine graph:

- cold: nothing remembered. Recall reads procedures only, and the copy holds none.
- warm: the same, on a copy holding one learned playbook, from the approved NovaPay investigation (not a case
  here). The subject's own findings are never read, so only the playbook differs from cold.
- revisit: the same subject again on its cold copy, now reading the findings the cold run remembered.
- baseline: the lookup baseline (lookup.py): one entity per call, same budget and verdict rule.

Expected outcomes stay in this evaluator; the planner never sees them. Every run is kept, failures included:
each is appended to backend/eval/runs/ as it finishes, and the set goes to results.json under
controlled.<planner>. Unknown and incomplete runs are scored apart from the detection cases, and so are the two
demo subjects, which development used and the playbook was learned from.

    python -m backend.eval.run_eval --planner fallback --repeats 3
    python -m backend.eval.run_eval --planner llm --repeats 3 [--cases c01 c05] [--conditions cold warm]

LLM runs can be staged within a provider quota: --merge adds a stage's runs to the saved ones (a rerun of the
same case, condition and repeat replaces the earlier one), and --first-repeat numbers later repeats. --rescore
applies the current scoring rules (SCORING) to the saved runs without running anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from urllib.parse import urlparse

from backend.app import agent, memory
from backend.app import queries as q
from backend.app import scenario as s
from backend.app.config import get_settings
from backend.app.contracts import PRESCRIBED_DEPTH
from backend.app.db import get_db
from backend.app.guardrails import search_clears
from backend.app.overlay import CASES_PATH
from backend.eval import lookup
from backend.eval.common import (RESULTS, RUNS, RetryCounter, db_work, now, provenance, save, scratch, sha256,
                                 source_graph, stats)

CONDITIONS = ("cold", "warm", "revisit", "baseline")
GROUPS = ("positive", "negative", "unknown", "incomplete", "demo")
DEMO = [("d01", s.NOVAPAY, {"verdict": "SUSPICIOUS", "target": s.VORTEX, "hops": 5}),
        ("d02", s.VIP, {"verdict": "CLEAR", "target": None, "hops": None})]
GROUP_OF = {"positive": "positive", "coordination": "positive", "negative_control": "negative",
            "out_of_scope_positive": "negative"}
UNKNOWN_IDS = [("u01", s.UNKNOWN), ("u02", "merch-harrow-fresh-foods")]
BUSIEST_ACCOUNT = "acct-100428660"
INCOMPLETE_NODE_BUDGET = 5_000


class QuotaExhausted(RuntimeError):
    """The provider's daily quota ran out: later LLM runs would only measure the fallback."""


def items() -> list[dict]:
    spec = json.loads(CASES_PATH.read_text())
    out = []
    for c in spec["cases"]:
        expected = c["expected"]
        positive = expected["verdict"] == "SUSPICIOUS"
        out.append({"case_id": c["case_id"], "subject_id": c["subject_id"], "group": GROUP_OF[c["category"]],
                    "category": c["category"], "pattern": c["pattern"], "split": c["split"],
                    "expected": {"verdict": expected["verdict"],
                                 "target": expected["sanctioned_target"] if positive else None,
                                 "hops": expected["eligible_hops"] if positive else None}})
    insufficient = {"verdict": "INSUFFICIENT", "target": None, "hops": None}
    for case_id, subject in UNKNOWN_IDS:
        out.append({"case_id": case_id, "subject_id": subject, "group": "unknown", "category": "unknown_id",
                    "pattern": "unknown_id", "split": None, "expected": insufficient})
    first = next(i for i in out if i["case_id"] == spec["cases"][0]["case_id"])
    out.append({"case_id": "i01", "subject_id": first["subject_id"], "group": "incomplete", "category": "incomplete",
                "pattern": "three_call_budget", "split": None, "expected": insufficient,
                "also_correct": first["expected"], "max_steps": 3,
                "why": f"{first['case_id']}'s positive subject with 3 calls: unless the planner establishes its route "
                       f"within them, the budget ends before the {PRESCRIBED_DEPTH}-hop search"})
    out.append({"case_id": "i02", "subject_id": BUSIEST_ACCOUNT, "group": "incomplete", "category": "incomplete",
                "pattern": "search_node_budget", "split": None, "expected": insufficient,
                "node_budget": INCOMPLETE_NODE_BUDGET,
                "why": f"the dataset's busiest account with the route search capped at {INCOMPLETE_NODE_BUDGET:,} "
                       "entities"})
    for case_id, subject, expected in DEMO:
        out.append({"case_id": case_id, "subject_id": subject, "group": "demo", "category": "demo",
                    "pattern": "demo_subject", "split": "dev", "expected": expected})
    return out


SCORING = {
    "SUSPICIOUS": "the verdict, sanctioned target and eligible hop count all match",
    "CLEAR": "the verdict matches and rests on a complete search that was exhausted or reached the prescribed depth",
    "unknown": "INSUFFICIENT, with the answer state unknown_entity",
    "incomplete": "INSUFFICIENT, or the subject's own verdict and route established within the budget",
}


def _checks(expected: dict, outcome: dict) -> dict:
    checks = {"verdict": outcome["verdict"] == expected["verdict"]}
    if expected["verdict"] == "SUSPICIOUS":
        checks["route"] = (outcome["target"], outcome["hops"]) == (expected["target"], expected["hops"])
    if expected["verdict"] == "CLEAR":
        checks["qualified"] = bool(outcome["qualified"])
    return checks


def score(item: dict, outcome: dict) -> dict:
    checks = _checks(item["expected"], outcome)
    if item["group"] == "unknown":
        checks["unknown_state"] = outcome["answer_state"] == "unknown_entity"
    if not all(checks.values()) and item.get("also_correct"):
        alternative = _checks(item["also_correct"], outcome)
        if all(alternative.values()):
            return {"correct": True, "checks": alternative}
    return {"correct": all(checks.values()), "checks": checks}


def _brief(args: dict) -> str:
    text = json.dumps(args, default=str, separators=(",", ":"))
    return text if len(text) <= 160 else text[:160] + "..."


def agent_outcome(events: list[dict]) -> dict:
    final = events[-1] if events and events[-1]["type"] == "final" else None
    results = [e for e in events if e["type"] == "tool_result"]
    notices = [e["message"] for e in events if e["type"] == "notice"]
    error = next((e["message"] for e in events if e["type"] == "error"), None)
    trace = [{"tool": r["tool"], "args": _brief(r["args"]), "ok": r["ok"], "ms": r["meta"]["elapsed_ms"],
              "error": r["error"]} for r in results]
    base = {"trace": trace, "notices": notices, "db": db_work(results, agent.for_model),
            "failure": error or next((n for n in notices if "failed" in n), None)}
    if final is None:
        return {**base, "verdict": None, "answer_state": None, "target": None, "hops": None, "route": None,
                "qualified": False, "tool_calls": len(results), "verdict_step": None, "elapsed_ms": None,
                "llm": None, "planner_used": None, "model_proposed": None, "playbook": None, "request": None}
    routes = sorted((f for f in final["facts"] if f["predicate"] == "eligible_route_hops"), key=lambda f: f["value"])
    route = routes[0] if routes else None
    basis = [f for f in final["facts"] if f["id"] in final["verdict_basis"]]
    steps = [f["source"]["step"] for f in basis if f.get("source")]
    request = final["approval_request"]
    llm_ms = final["llm_ms"]
    return {
        **base,
        "verdict": final["verdict"],
        "answer_state": final["answer_state"],
        "target": route and route["object"],
        "hops": route and route["value"],
        "route": route and route["scope"].get("path", {}).get("nodes"),
        "qualified": any(f["predicate"] == "no_eligible_route" and search_clears(f["scope"]) for f in basis),
        "tool_calls": final["tool_calls"],
        "verdict_step": max(steps) if steps and final["verdict"] != "INSUFFICIENT" else None,
        "budget_exhausted": final["budget_exhausted"],
        "elapsed_ms": final["elapsed_ms"],
        "llm": {"requests": len(llm_ms), "ms": round(sum(llm_ms), 1), "wait_ms": final["llm_wait_ms"],
                "tokens": final.get("llm_tokens")} if llm_ms else None,
        "planner_used": final["planner"],
        "model_proposed": (final["model_answer"] or {}).get("proposed_verdict"),
        "playbook": final["playbook"] and {k: final["playbook"][k] for k in ("id", "ran", "stopped")},
        "request": request and {"id": request["request_id"], "status": request["status"],
                                "targets": request["target_ids"]},
    }


def baseline_outcome(result: dict) -> dict:
    envelopes = result["envelopes"]
    return {
        "verdict": result["verdict"], "answer_state": result["answer_state"], "target": result.get("target"),
        "hops": result.get("hops"), "route": result["route"], "qualified": result["verdict"] == "CLEAR",
        "tool_calls": result["tool_calls"], "verdict_step": result["verdict_step"],
        "budget_exhausted": result["budget_exhausted"], "elapsed_ms": result["elapsed_ms"],
        "llm": result.get("llm"), "planner_used": result["planner"], "model_proposed": result.get("model_proposed"),
        "playbook": None, "request": None, "reached": result["reached"], "unread": result["unread"],
        "trace": [{"tool": e["tool"], "args": _brief(e["args"]), "ok": e["ok"], "ms": e["meta"]["elapsed_ms"],
                   "error": e["error"]} for e in envelopes],
        "notices": [], "db": db_work(envelopes, lookup.for_model), "failure": result.get("failure"),
    }


async def run_agent(item: dict, key: str, planner: str, memory_mode: str) -> dict:
    saved = q.SEARCH_NODE_BUDGET
    q.SEARCH_NODE_BUDGET = item.get("node_budget") or saved
    try:
        events = await agent.collect(item["subject_id"], graph_key=key, mode=planner, worker_id=f"eval-{planner}",
                                     memory_mode=memory_mode, max_steps=item.get("max_steps", agent.MAX_STEPS))
    finally:
        q.SEARCH_NODE_BUDGET = saved
    return agent_outcome(events)


async def run_baseline(item: dict, source: str, planner: str) -> dict:
    graph = get_db().select_graph(source)
    budget = item.get("max_steps", agent.MAX_STEPS)
    if planner == "llm":
        return baseline_outcome(await lookup.llm_search(graph, item["subject_id"], budget))
    return baseline_outcome(await asyncio.to_thread(lookup.search, graph, item["subject_id"], budget))


async def warm_template(source: str, key: str) -> dict:
    """One playbook, learned the only way the system learns one: from an investigation a human approved."""
    graph = get_db().select_graph(key)
    events = await agent.collect(s.NOVAPAY, graph_key=key, mode="fallback", worker_id="eval-teacher")
    request = events[-1]["approval_request"]["request_id"]
    q.approve_action(graph, request, s.PRIYA)
    book = memory.learn_playbook(graph, request)
    return {"id": book["id"], "family": book["family"], "required_patterns": book["required_patterns"],
            "steps": [step["tool"] for step in book["steps"]], "learned_from": book["derived_from"][0]}


async def evaluate(planner: str, repeats: range, case_ids: list[str], conditions: list[str], log,
                   runs: list[dict], extra: dict) -> None:
    source = source_graph()
    selected = [i for i in items() if not case_ids or i["case_id"] in case_ids]

    def record(item: dict, condition: str, repeat: int, outcome: dict, retries: int) -> None:
        run = {"planner": planner, "condition": condition, "repeat": repeat,
               **{k: item.get(k) for k in ("case_id", "subject_id", "group", "category", "pattern", "split")},
               "expected": item["expected"], "also_correct": item.get("also_correct"), **score(item, outcome),
               **outcome, "provider_retries": retries,
               "at": now()}
        runs.append(run)
        log.write(json.dumps(run, default=str) + "\n")
        log.flush()
        mark = "ok" if run["correct"] else "WRONG"
        print(f"{planner} r{repeat} {item['case_id']:>4} {condition:<8} {run['verdict'] or 'FAILED':<12} {mark:<5} "
              f"{run['tool_calls']:>2} calls {run['elapsed_ms'] or 0:>8.0f} ms"
              + (f" | {run['failure']}" if run["failure"] else ""), flush=True)
        if "daily quota reached" in (run["failure"] or ""):
            raise QuotaExhausted(run["failure"])

    with scratch(source) as template_key:
        extra["playbook"] = await warm_template(source, template_key) if "warm" in conditions else None
        for repeat in repeats:
            for item in selected:
                detection = item["group"] in ("positive", "negative")
                if {"cold", "revisit"} & set(conditions):
                    with scratch(source) as key:
                        with RetryCounter() as retries:
                            record(item, "cold", repeat, await run_agent(item, key, planner, "procedures"),
                                   retries.count)
                        if "revisit" in conditions and (detection or item["group"] == "demo"):
                            with RetryCounter() as retries:
                                record(item, "revisit", repeat, await run_agent(item, key, planner, "on"),
                                       retries.count)
                if "warm" in conditions and detection:
                    with scratch(template_key) as key, RetryCounter() as retries:
                        record(item, "warm", repeat, await run_agent(item, key, planner, "procedures"),
                               retries.count)
                if "baseline" in conditions:
                    with RetryCounter() as retries:
                        record(item, "baseline", repeat, await run_baseline(item, source, planner), retries.count)


def _total_tokens(run: dict) -> int | None:
    tokens = (run["llm"] or {}).get("tokens")
    return tokens["prompt"] + tokens["completion"] if tokens else None


def measures(runs: list[dict]) -> dict:
    return {
        "tool_calls": stats([r["tool_calls"] for r in runs]),
        "verdict_step": stats([r["verdict_step"] for r in runs]),
        "elapsed_ms": stats([r["elapsed_ms"] for r in runs]),
        "llm_requests": stats([(r["llm"] or {}).get("requests") for r in runs]),
        "llm_ms": stats([(r["llm"] or {}).get("ms") for r in runs]),
        "llm_tokens": stats([_total_tokens(r) for r in runs]),
        "db_queries": stats([r["db"]["queries"] for r in runs]),
        "db_ms": stats([r["db"]["ms"] for r in runs]),
        "bytes_to_planner": stats([r["db"]["bytes"] for r in runs]),
    }


def _delta(runs: list[dict], case_id: str, condition: str, versus: str = "cold") -> dict | None:
    mine = [r for r in runs if r["case_id"] == case_id and r["condition"] == condition]
    base = [r for r in runs if r["case_id"] == case_id and r["condition"] == versus]
    if not mine or not base:
        return None
    a, b = measures(mine), measures(base)
    out = {"runs": len(mine), "correct": f"{sum(r['correct'] for r in mine)}/{len(mine)}"}
    for key in ("tool_calls", "elapsed_ms", "llm_requests", "llm_tokens"):
        if a[key] and b[key]:
            out[key] = {condition: a[key]["median"], versus: b[key]["median"],
                        "change": round(a[key]["median"] - b[key]["median"], 1)}
    return out


def summarize(runs: list[dict]) -> dict:
    conditions = {}
    for condition in CONDITIONS:
        mine = [r for r in runs if r["condition"] == condition]
        if not mine:
            continue
        groups = {}
        for group in GROUPS:
            chosen = [r for r in mine if r["group"] == group]
            if chosen:
                groups[group] = {"correct": sum(r["correct"] for r in chosen), "runs": len(chosen),
                                 "verdicts": dict(Counter(r["verdict"] or "FAILED" for r in chosen))}
        detection = [r for r in mine if r["group"] in ("positive", "negative")]
        conditions[condition] = {
            "groups": groups,
            "detection": {"correct": sum(r["correct"] for r in detection), "runs": len(detection)},
            "failures": [{"case_id": r["case_id"], "repeat": r["repeat"], "failure": r["failure"]}
                         for r in mine if r["failure"]],
            "planner_used": dict(Counter(r["planner_used"] or "none" for r in mine)),
            "model_agreed": sum(r["model_proposed"] == r["verdict"] for r in mine if r["model_proposed"]),
            "model_answered": sum(1 for r in mine if r["model_proposed"]),
            "provider_retries": sum(r["provider_retries"] for r in mine),
            **measures(detection),
        }
    case_ids = list(dict.fromkeys(r["case_id"] for r in runs))
    cases = {}
    for case_id in case_ids:
        mine = [r for r in runs if r["case_id"] == case_id]
        cases[case_id] = {
            "subject_id": mine[0]["subject_id"], "group": mine[0]["group"], "pattern": mine[0]["pattern"],
            "split": mine[0]["split"], "expected": mine[0]["expected"]["verdict"],
            **{c: {"correct": f"{sum(r['correct'] for r in rs)}/{len(rs)}",
                   "verdicts": dict(Counter(r["verdict"] or "FAILED" for r in rs)),
                   "tool_calls": stats([r["tool_calls"] for r in rs]),
                   "elapsed_ms": stats([r["elapsed_ms"] for r in rs])}
               for c in CONDITIONS if (rs := [r for r in mine if r["condition"] == c])}}
    playbook_cases = sorted({r["case_id"] for r in runs if r["condition"] == "warm" and r["playbook"]})
    return {
        "conditions": conditions,
        "cases": cases,
        "warm_vs_cold": {"playbook_used_on": playbook_cases,
                         "cases": {c: _delta(runs, c, "warm") for c in playbook_cases}},
        "revisit_vs_cold": {c: _delta(runs, c, "revisit") for c in case_ids
                            if any(r["condition"] == "revisit" and r["case_id"] == c for r in runs)},
    }


def config(planner: str, repeats: range, conditions: list[str], source: str) -> dict:
    settings = get_settings()
    llm = planner == "llm"
    return {
        "planner": planner,
        "model": settings.llm_model if llm else None,
        "llm_host": urlparse(settings.llm_base_url or "").hostname if llm else None,
        "temperature": 0 if llm else None,
        "llm_requests_per_minute": settings.llm_requests_per_minute or None,
        "repeats": [repeats.start, repeats.stop - 1],
        "conditions": conditions,
        "max_steps": agent.MAX_STEPS,
        "depth": PRESCRIBED_DEPTH,
        "result_chars": agent.RESULT_CHARS,
        "lookup_page_size": lookup.PAGE_SIZE,
        "search_node_budget": q.SEARCH_NODE_BUDGET,
        "search_time_budget_s": q.SEARCH_TIME_BUDGET_S,
        "prompts_sha256": {"agent": sha256(agent.SYSTEM_PROMPT), "baseline": sha256(lookup.PROMPT)},
        "cases_sha256": sha256(CASES_PATH.read_text()),
        "scoring": SCORING,
        **provenance(source),
    }


def run_stage(args, saved: dict | None) -> tuple[list[dict], list[dict], dict | None]:
    repeats = range(args.first_repeat, args.first_repeat + args.repeats)
    started_at, started = now(), time.perf_counter()
    RUNS.mkdir(parents=True, exist_ok=True)
    path = RUNS / f"eval-{args.planner}-{started_at.replace(':', '').replace('-', '')}.jsonl"
    runs: list[dict] = []
    extra: dict = {"playbook": None, "stopped_early": None}
    with path.open("w") as log:
        try:
            asyncio.run(evaluate(args.planner, repeats, args.cases, args.conditions, log, runs, extra))
        except QuotaExhausted as exc:
            extra["stopped_early"] = f"after {len(runs)} runs: {exc}"
            print(f"Stopped early: {extra['stopped_early']}")
    stage = {"started_at": started_at, "finished_at": now(), "seconds": round(time.perf_counter() - started, 1),
             "log": str(path.relative_to(RUNS.parent.parent.parent)),
             "conditions": [c for c in CONDITIONS if any(r["condition"] == c for r in runs)],
             "cases": args.cases or "all", "repeats": [repeats.start, repeats.stop - 1], "runs": len(runs),
             "stopped_early": extra["stopped_early"]}
    if not saved:
        return runs, [stage], extra["playbook"]
    fresh = {(r["condition"], r["case_id"], r["repeat"]) for r in runs}
    kept = [r for r in saved["runs"] if (r["condition"], r["case_id"], r["repeat"]) not in fresh]
    return kept + runs, saved.get("stages", []) + [stage], extra["playbook"] or saved.get("playbook")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--planner", choices=["fallback", "llm"], default="fallback")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--first-repeat", type=int, default=1)
    parser.add_argument("--cases", nargs="*", default=[])
    parser.add_argument("--conditions", nargs="*", choices=CONDITIONS, default=list(CONDITIONS))
    parser.add_argument("--merge", action="store_true", help="add these runs to the saved ones for this planner")
    parser.add_argument("--rescore", action="store_true", help="re-score the saved runs; run nothing")
    parser.add_argument("--no-save", action="store_true", help="log the runs but leave results.json alone")
    args = parser.parse_args()
    if args.planner == "llm" and not get_settings().llm_enabled and not args.rescore:
        raise SystemExit("No LLM configured: set LLM_API_KEY in .env.")

    source = source_graph()
    saved = json.loads(RESULTS.read_text()).get("controlled", {}).get(args.planner) if RESULTS.exists() else None
    if args.rescore:
        if not saved:
            raise SystemExit(f"No saved {args.planner} runs to re-score.")
        runs, stages, playbook = saved["runs"], saved.get("stages", []), saved.get("playbook")
    else:
        runs, stages, playbook = run_stage(args, saved if args.merge else None)
    by_id = {i["case_id"]: i for i in items()}
    for run in runs:
        item = by_id[run["case_id"]]
        run.update(score(item, run), also_correct=item.get("also_correct"))
    summary = summarize(runs)
    numbers = [r["repeat"] for r in runs] or [args.first_repeat]
    result = {"config": config(args.planner, range(min(numbers), max(numbers) + 1),
                               [c for c in CONDITIONS if any(r["condition"] == c for r in runs)], source),
              "stages": stages, "playbook": playbook, "summary": summary, "runs": runs}
    if not args.no_save:
        save(["controlled", args.planner], result)
    for condition, c in summary["conditions"].items():
        groups = ", ".join(f"{g} {v['correct']}/{v['runs']}" for g, v in c["groups"].items())
        calls = c["tool_calls"]["median"] if c["tool_calls"] else None
        print(f"{condition:<8} detection {c['detection']['correct']}/{c['detection']['runs']} ({groups}); "
              f"median calls {calls}; failures {len(c['failures'])}")


if __name__ == "__main__":
    main()
