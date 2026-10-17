"""Contention measurement: claimants and a sweeper work one alert queue through graph state alone. Claimants crash
(claim and never commit), stall past their lease and then try to commit, or fail the alert so it is retried;
the rest complete. Every run is checked for lost alerts, alerts completed twice, completions credited to the
wrong worker and late commits that got through, and timed. Results go to results.json under contention.

    python -m backend.eval.contention [--runs 5] [--claimants 20] [--alerts 200]
"""

from __future__ import annotations

import argparse
import random
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from falkordb import Graph

from backend.app import coordination as co
from backend.app.contracts import MAX_ATTEMPTS
from backend.app.db import get_db
from backend.eval.common import now, save, stats

CRASH, STALL, FAIL = 0.08, 0.06, 0.06


def plant(key: str, n: int) -> Graph:
    """A graph holding only n pending alerts, for queue mechanics."""
    graph = get_db().select_graph(key)
    graph.query("""
        UNWIND range(1, $n) AS i
        CREATE (:Entity:Alert {id: 'alert-' + toString(i), status: 'PENDING', priority: i % 5, created_at: i,
                               subject_id: 'acct-' + toString(i)})
    """, {"n": n})
    return graph


def check(queue: list[dict], alerts: int, refused: list[str]) -> list[str]:
    """Everything that would mean lost or duplicated work; empty when the run is sound."""
    problems = [] if len(queue) == alerts else [f"{alerts - len(queue)} alerts missing from the queue"]
    for alert in queue:
        numbers = [a["attempt"] for a in alert["attempts"]]
        completions = [a for a in alert["attempts"] if a["status"] == "COMPLETED"]
        if alert["status"] not in ("COMPLETED", "FAILED"):
            problems.append(f"{alert['id']} lost: still {alert['status']}")
        if numbers != list(range(1, (alert["attempt"] or 0) + 1)):
            problems.append(f"{alert['id']} attempts numbered {numbers}")
        if alert["status"] == "COMPLETED" and [a["worker_id"] for a in completions] != [alert["completed_by"]]:
            problems.append(f"{alert['id']} completed by {[a['worker_id'] for a in completions]}, "
                            f"credited to {alert['completed_by']}")
        if alert["status"] == "FAILED" and (completions or alert["attempt"] != MAX_ATTEMPTS):
            problems.append(f"{alert['id']} FAILED after {alert['attempt']} attempts with {len(completions)} "
                            "completions")
    by_id = {a["id"]: a for a in queue}
    for alert_id in refused:
        if not any(a["refused"] and a["status"] == "EXPIRED" for a in by_id[alert_id]["attempts"]):
            problems.append(f"{alert_id}'s refused late commit left no EXPIRED, refused attempt")
    return problems


def run(key: str, claimants: int = 20, alerts: int = 200, lease: float = 0.3, seed: int = 0,
        timeout: float = 90.0) -> dict:
    graph = plant(key, alerts)
    stop, lock = threading.Event(), threading.Lock()
    events: Counter = Counter()
    refused: list[str] = []

    def claimant(n: int) -> None:
        rng = random.Random(seed * 1000 + n)
        while True:
            alert = co.claim(graph, f"w{n:02d}", lease_seconds=lease)
            if alert is None:
                if stop.is_set():
                    return
                time.sleep(0.02)
                continue
            roll, outcome = rng.random(), "completed"
            if roll < CRASH:
                outcome = "crashed"
            elif roll < CRASH + STALL:
                time.sleep(lease * 3)
                committed = co.complete(graph, alert["id"], alert["token"], {"verdict": "CLEAR"})
                outcome = "late_commit_accepted" if committed else "late_commit_refused"
                if not committed:
                    with lock:
                        refused.append(alert["id"])
            elif roll < CRASH + STALL + FAIL:
                co.fail(graph, alert["id"], alert["token"], "simulated failure")
                outcome = "failed"
            elif not co.complete(graph, alert["id"], alert["token"], {"verdict": "CLEAR"}):
                outcome = "completion_refused"
            with lock:
                events["claims"] += 1
                events[outcome] += 1

    def sweeper() -> None:
        while not stop.is_set():
            swept = co.sweep(graph)
            with lock:
                for kind in ("expired", "retried", "gave_up"):
                    events[f"swept_{kind}"] += len(swept[kind])
            time.sleep(0.05)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=claimants + 1) as pool:
        futures = [pool.submit(claimant, n) for n in range(claimants)] + [pool.submit(sweeper)]
        deadline = time.time() + timeout
        while time.time() < deadline:
            unfinished = graph.ro_query("""
                MATCH (al:Alert)
                WHERE al.status IN ['PENDING', 'CLAIMED'] OR (al.status = 'FAILED' AND al.attempt < $max)
                RETURN count(al)
            """, {"max": MAX_ATTEMPTS}).result_set[0][0]
            if unfinished == 0:
                break
            time.sleep(0.1)
        seconds = time.perf_counter() - started
        stop.set()
        for future in futures:
            future.result()

    queue = co.queue_state(graph)
    return {"claimants": claimants, "alerts": alerts, "lease_s": lease, "seed": seed,
            "seconds": round(seconds, 2), "alerts_per_s": round(alerts / seconds, 1),
            "events": dict(events), "late_commits_refused": len(refused),
            "statuses": dict(Counter(a["status"] for a in queue)),
            "attempts": {str(k): v for k, v in sorted(Counter(a["attempt"] for a in queue).items())},
            "reclaimed": sum(a["attempt"] > 1 for a in queue),
            "violations": check(queue, alerts, refused)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--claimants", type=int, default=20)
    parser.add_argument("--alerts", type=int, default=200)
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    runs = []
    for seed in range(1, args.runs + 1):
        key = f"fintrace:test-contention-{uuid.uuid4().hex[:8]}"
        try:
            result = run(key, args.claimants, args.alerts, seed=seed)
        finally:
            if key in get_db().list_graphs():
                get_db().select_graph(key).delete()
        runs.append(result)
        print(f"run {seed}: {result['alerts']} alerts in {result['seconds']} s ({result['alerts_per_s']}/s), "
              f"{result['statuses']}, {result['late_commits_refused']} late commits refused, "
              f"{len(result['violations'])} violations", flush=True)
    summary = {"runs": len(runs), "violations": sum(len(r["violations"]) for r in runs),
               "seconds": stats([r["seconds"] for r in runs]), "alerts_per_s": stats([r["alerts_per_s"] for r in runs]),
               "late_commits_refused": sum(r["late_commits_refused"] for r in runs),
               "late_commits_accepted": sum(r["events"].get("late_commit_accepted", 0) for r in runs),
               "crashed_claims_recovered": sum(r["events"].get("crashed", 0) for r in runs),
               "statuses": dict(sum((Counter(r["statuses"]) for r in runs), Counter()))}
    if not args.no_save:
        save(["contention"], {"at": now(), "config": {"claimants": args.claimants, "alerts": args.alerts,
                                                      "lease_s": 0.3, "max_attempts": MAX_ATTEMPTS,
                                                      "crash": CRASH, "stall": STALL, "fail": FAIL},
                              "summary": summary, "runs": runs})
    print(f"{summary['runs']} runs, {summary['violations']} violations, median {summary['alerts_per_s']['median']} "
          f"alerts/s")


if __name__ == "__main__":
    main()
