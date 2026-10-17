"""The alert queue and the hunters that drain it, coordinating only through graph state.

Lifecycle (contracts.AlertStatus): PENDING -> CLAIMED -> COMPLETED | FAILED. A claim is one write that stamps a
fresh token and a lease and records an Attempt; the hunter renews the lease while it works, and renewing,
completing or failing commits only if the alert still carries its token and the lease has not run out (a lease
is good up to and including lease_until; the sweeper expires it after). While it works, the hunter's case,
memory, case merges and freeze request are written under the same condition, and it stops once the lease is
lost. The sweeper returns expired claims to PENDING and retries FAILED alerts until MAX_ATTEMPTS, after which
they stay FAILED and visible. Queue state is not evidence, so none of this touches the graph revision.

    python -m backend.app.coordination --workers 3          # drain the default tenant's queue
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from falkordb import Graph

from backend.app import agent
from backend.app.contracts import ALERT_RULES, LEASE_SECONDS, MAX_ATTEMPTS, AlertStatus, Claim
from backend.app.db import get_db, tenant_key

Emit = Callable[[dict], Awaitable[None]]

ALERT = ("al {.id, .subject_id, .rule, .priority, .created_at, .status, .attempt, .claimed_by, .claimed_at, "
         ".lease_until, .case_id, .verdict, .answer_state, .episode_id, .request_id, .error, .completed_at, "
         ".completed_by}")
HELD = f"al.status = '{AlertStatus.CLAIMED}' AND al.claim_token = $token AND al.lease_until >= $now"


def claim(graph: Graph, worker_id: str, now: float | None = None, lease_seconds: float = LEASE_SECONDS) -> dict | None:
    """Claim the highest-priority pending alert, oldest first, or None if none is pending. LIMIT comes before
    SET, so the write claims exactly one alert; the token is read back before the claim is trusted."""
    for _ in range(3):
        now = time.time() if now is None else now
        token = uuid.uuid4().hex
        rows = graph.query(f"""
            MATCH (al:Entity:Alert {{status: '{AlertStatus.PENDING}'}})
            WITH al ORDER BY al.priority DESC, al.created_at, al.id LIMIT 1
            SET al.status = '{AlertStatus.CLAIMED}', al.claim_token = $token, al.claimed_by = $worker,
                al.claimed_at = $now, al.lease_until = $until, al.attempt = coalesce(al.attempt, 0) + 1,
                al.case_id = coalesce(al.case_id, 'case-' + CASE WHEN al.id STARTS WITH 'alert-'
                                                                 THEN substring(al.id, 6) ELSE al.id END)
            CREATE (a:Attempt {{token: $token, alert_id: al.id, worker_id: $worker, attempt: al.attempt,
                                claimed_at: $now, status: '{AlertStatus.CLAIMED}'}})-[:FOR_ALERT]->(al)
            RETURN {ALERT}
        """, {"token": token, "worker": worker_id, "now": now, "until": now + lease_seconds}).result_set
        if not rows:
            return None
        alert = dict(rows[0][0])
        held = graph.ro_query("MATCH (al:Entity:Alert {id: $id}) RETURN al.claim_token",
                              {"id": alert["id"]}).result_set
        if held and held[0][0] == token:
            return {**alert, "token": token}
    return None


def renew(graph: Graph, alert_id: str, token: str, now: float | None = None,
          lease_seconds: float = LEASE_SECONDS) -> bool:
    """Extend the lease; False once the claim is lost (the alert no longer carries this token, or the lease ran out)."""
    now = time.time() if now is None else now
    return bool(graph.query(f"""
        MATCH (al:Entity:Alert {{id: $id}})
        WHERE {HELD}
        SET al.lease_until = $until
        RETURN al.id
    """, {"id": alert_id, "token": token, "now": now, "until": now + lease_seconds}).result_set)


def _refuse(graph: Graph, alert_id: str, token: str, what: str, now: float) -> None:
    graph.query("""
        MATCH (:Entity:Alert {id: $id})<-[:FOR_ALERT]-(a:Attempt {token: $token})
        SET a.commit_refused_at = $now, a.refused = $what
    """, {"id": alert_id, "token": token, "now": now,
          "what": f"{what} refused: this attempt no longer holds the claim (its lease ran out or the alert "
                  f"carries another token)"})


def complete(graph: Graph, alert_id: str, token: str, outcome: dict, now: float | None = None) -> bool:
    """Commit a finished investigation, only if this token still holds the claim."""
    now = time.time() if now is None else now
    done = graph.query(f"""
        MATCH (al:Entity:Alert {{id: $id}})<-[:FOR_ALERT]-(a:Attempt {{token: $token}})
        WHERE {HELD}
        SET al.status = '{AlertStatus.COMPLETED}', al.completed_at = $now, al.completed_by = al.claimed_by,
            al.lease_until = NULL, al.case_id = $case, al.verdict = $verdict, al.answer_state = $state,
            al.episode_id = $episode, al.request_id = $request, al.error = NULL,
            a.status = '{AlertStatus.COMPLETED}', a.finished_at = $now, a.episode_id = $episode
        RETURN al.id
    """, {"id": alert_id, "token": token, "now": now, "case": outcome.get("case_id"),
          "verdict": outcome.get("verdict"), "state": outcome.get("answer_state"),
          "episode": outcome.get("episode_id"), "request": outcome.get("request_id")}).result_set
    if not done:
        _refuse(graph, alert_id, token, "completion", now)
    return bool(done)


def fail(graph: Graph, alert_id: str, token: str, error: str, now: float | None = None) -> bool:
    """Record a failed attempt, only if this token still holds the claim; the sweeper retries it."""
    now = time.time() if now is None else now
    done = graph.query(f"""
        MATCH (al:Entity:Alert {{id: $id}})<-[:FOR_ALERT]-(a:Attempt {{token: $token}})
        WHERE {HELD}
        SET al.status = '{AlertStatus.FAILED}', al.failed_at = $now, al.error = $error, al.lease_until = NULL,
            a.status = '{AlertStatus.FAILED}', a.finished_at = $now, a.error = $error
        RETURN al.id
    """, {"id": alert_id, "token": token, "now": now, "error": error[:500]}).result_set
    if not done:
        _refuse(graph, alert_id, token, "failure", now)
    return bool(done)


def sweep(graph: Graph, now: float | None = None, max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Expire lapsed claims and retry failed alerts. An alert on its last attempt stays FAILED instead."""
    now = time.time() if now is None else now
    expired = graph.query(f"""
        MATCH (al:Entity:Alert {{status: '{AlertStatus.CLAIMED}'}})<-[:FOR_ALERT]-(a:Attempt)
        WHERE al.lease_until < $now AND a.token = al.claim_token
        SET a.status = 'EXPIRED', a.finished_at = $now,
            al.status = CASE WHEN al.attempt < $max THEN '{AlertStatus.PENDING}' ELSE '{AlertStatus.FAILED}' END,
            al.error = CASE WHEN al.attempt < $max THEN al.error
                            ELSE 'the lease expired on attempt ' + toString(al.attempt) END,
            al.claim_token = NULL, al.claimed_by = NULL, al.lease_until = NULL
        RETURN al.id, al.status
    """, {"now": now, "max": max_attempts}).result_set
    retried = graph.query(f"""
        MATCH (al:Entity:Alert {{status: '{AlertStatus.FAILED}'}})
        WHERE al.attempt < $max
        SET al.status = '{AlertStatus.PENDING}', al.claim_token = NULL, al.claimed_by = NULL
        RETURN al.id
    """, {"max": max_attempts}).result_set
    return {"expired": sorted(a for a, _ in expired),
            "gave_up": sorted(a for a, status in expired if status == AlertStatus.FAILED),
            "retried": sorted(a for (a,) in retried)}


def queue_state(graph: Graph) -> list[dict]:
    """Every alert with its owner, attempts and outcome, highest priority first."""
    rows = graph.ro_query(f"""
        MATCH (al:Entity:Alert)
        OPTIONAL MATCH (al)<-[:FOR_ALERT]-(a:Attempt)
        WITH al, a ORDER BY a.claimed_at
        WITH al, collect(a {{.worker_id, .attempt, .status, .claimed_at, .finished_at, .error, .episode_id,
                            .commit_refused_at, .refused}}) AS attempts
        OPTIONAL MATCH (c:Case {{id: al.case_id}})
        OPTIONAL MATCH (c)-[m:MERGED_INTO]->(into:Case)
        RETURN {ALERT}, attempts, into.id, m.shared
        ORDER BY al.priority DESC, al.created_at, al.id
    """).result_set
    return [{**dict(alert), "attempts": attempts, "merged_into": into, "merged_on": shared}
            for alert, attempts, into, shared in rows]


def raise_alert(graph: Graph, subject_id: str, rule: str, priority: int = 3, now: float | None = None) -> dict:
    """A new PENDING alert on an entity, as a monitoring rule would raise it."""
    if rule not in ALERT_RULES:
        raise ValueError(f"rule must be one of {list(ALERT_RULES)}")
    rows = graph.query(f"""
        MATCH (x:Entity {{id: $subject}}) WHERE x:Account
        CREATE (al:Entity:Alert {{id: $id, status: '{AlertStatus.PENDING}', priority: $priority, created_at: $now,
                                 rule: $rule, subject_id: $subject}})
        CREATE (al)-[:ALERT_ON]->(x)
        RETURN {ALERT}
    """, {"subject": subject_id, "id": f"alert-{uuid.uuid4().hex[:8]}", "priority": priority, "rule": rule,
          "now": int(time.time() if now is None else now)}).result_set
    if not rows:
        raise ValueError(f"{subject_id} is not an account in the graph")
    return dict(rows[0][0])


def reset_queue(graph: Graph) -> int:
    """Every alert back to PENDING with no attempts (demo and test resets)."""
    graph.query("MATCH (a:Attempt) DETACH DELETE a")
    return graph.query(f"""
        MATCH (al:Entity:Alert)
        SET al.status = '{AlertStatus.PENDING}', al.attempt = NULL, al.claim_token = NULL, al.claimed_by = NULL,
            al.claimed_at = NULL, al.lease_until = NULL, al.case_id = NULL, al.verdict = NULL,
            al.answer_state = NULL, al.episode_id = NULL, al.request_id = NULL, al.error = NULL,
            al.completed_at = NULL, al.completed_by = NULL, al.failed_at = NULL
        RETURN count(al)
    """).result_set[0][0]


async def _keep_lease(graph: Graph, alert: dict, lease_seconds: float, lost: asyncio.Event) -> None:
    while True:
        await asyncio.sleep(lease_seconds / 3)
        if not await asyncio.to_thread(renew, graph, alert["id"], alert["token"], lease_seconds=lease_seconds):
            lost.set()
            return


async def work(key: str, worker_id: str, alert: dict, *, mode: agent.Mode = "fallback",
               memory_mode: agent.MemoryMode = "on", max_steps: int = agent.MAX_STEPS,
               lease_seconds: float = LEASE_SECONDS, emit: Emit | None = None) -> dict:
    """Investigate one claimed alert and commit the outcome under its claim token."""
    graph = get_db().select_graph(key)

    async def say(event: dict) -> None:
        if emit:
            await emit({**event, "worker_id": worker_id, "alert_id": alert["id"]})

    await say({"type": "claimed", "subject_id": alert["subject_id"], "attempt": alert["attempt"],
               "case_id": alert["case_id"], "priority": alert["priority"]})
    lost = asyncio.Event()
    keeper = asyncio.create_task(_keep_lease(graph, alert, lease_seconds, lost))
    final, error = None, None

    async def follow() -> None:
        nonlocal final, error
        async for event in agent.investigate(alert["subject_id"], graph_key=key, worker_id=worker_id, mode=mode,
                                             max_steps=max_steps, case_id=alert["case_id"], memory_mode=memory_mode,
                                             claim=Claim(alert["id"], alert["token"])):
            await say(event)
            if event["type"] == "final":
                final = event
            elif event["type"] == "error":
                error = event["message"]

    # Writes made under the claim check it themselves; stopping here only saves the work a lost claim can't commit.
    run, gone = asyncio.create_task(follow()), asyncio.create_task(lost.wait())
    try:
        await asyncio.wait({run, gone}, return_when=asyncio.FIRST_COMPLETED)
        if run.done():
            run.result()
        else:
            final, error = None, ("the claim was lost (its lease ran out or another hunter took the alert), so the "
                                  "investigation stopped")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        for task in (run, gone, keeper):
            task.cancel()
        await asyncio.gather(run, gone, keeper, return_exceptions=True)
    memo = (final or {}).get("memory") or {}
    outcome = {"alert_id": alert["id"], "subject_id": alert["subject_id"], "worker_id": worker_id,
               "attempt": alert["attempt"], "case_id": alert["case_id"],
               "verdict": final["verdict"] if final else None,
               "answer_state": final["answer_state"] if final else None,
               "episode_id": memo.get("episode_id"), "merged_into": (memo.get("case") or {}).get("merged_into"),
               "request_id": ((final or {}).get("approval_request") or {}).get("request_id"),
               "tool_calls": final["tool_calls"] if final else None, "lost_lease": lost.is_set(), "error": error}
    if final:
        outcome["committed"] = await asyncio.to_thread(complete, graph, alert["id"], alert["token"], outcome)
        outcome["status"] = AlertStatus.COMPLETED.value
    else:
        outcome["committed"] = await asyncio.to_thread(fail, graph, alert["id"], alert["token"],
                                                       error or "the investigation ended without a result")
        outcome["status"] = AlertStatus.FAILED.value
    await say({"type": "alert_done", **outcome})
    return outcome


async def hunt(key: str, worker_id: str, *, lease_seconds: float = LEASE_SECONDS, max_alerts: int | None = None,
               **kwargs) -> list[dict]:
    """Claim and investigate alerts until none is pending (or `max_alerts` have been claimed)."""
    graph = get_db().select_graph(key)
    outcomes: list[dict] = []
    while max_alerts is None or len(outcomes) < max_alerts:
        alert = await asyncio.to_thread(claim, graph, worker_id, lease_seconds=lease_seconds)
        if alert is None:
            break
        outcomes.append(await work(key, worker_id, alert, lease_seconds=lease_seconds, **kwargs))
    return outcomes


async def drain(key: str, workers: int = 3, *, emit: Emit | None = None, **kwargs) -> dict:
    """Run `workers` hunters until the queue is empty, sweeping between rounds so retried alerts are picked up."""
    graph = get_db().select_graph(key)
    started = time.perf_counter()
    outcomes: list[dict] = []
    sweeps: list[dict] = []
    for _ in range(MAX_ATTEMPTS + 1):
        rounds = await asyncio.gather(*(hunt(key, f"hunter-{n}", emit=emit, **kwargs)
                                        for n in range(1, workers + 1)))
        outcomes += [o for r in rounds for o in r]
        swept = await asyncio.to_thread(sweep, graph)
        sweeps.append(swept)
        if emit:
            await emit({"type": "swept", **swept})
        if not swept["expired"] and not swept["retried"]:
            break
    return {"graph": key, "workers": workers, "outcomes": outcomes, "sweeps": sweeps,
            "queue": await asyncio.to_thread(queue_state, graph),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Drain the alert queue with concurrent hunters.")
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--graph", default=None, help="graph key; overrides --tenant")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--mode", choices=["auto", "llm", "fallback"], default="fallback")
    parser.add_argument("--reset", action="store_true", help="put every alert back to PENDING first")
    cli = parser.parse_args()
    graph_key = cli.graph or tenant_key(cli.tenant)
    if cli.reset:
        print(f"reset {reset_queue(get_db().select_graph(graph_key))} alerts")

    async def show(event: dict) -> None:
        if event["type"] in ("claimed", "alert_done", "swept"):
            print(json.dumps({k: v for k, v in event.items() if v is not None}, default=str))

    summary = asyncio.run(drain(graph_key, cli.workers, mode=cli.mode, emit=show))
    print(f"{len(summary['outcomes'])} attempts in {summary['elapsed_ms'] / 1000:.1f}s")
