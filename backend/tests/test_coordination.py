"""Hunters coordinate only through graph state: atomic claims with leases, token-checked commits, a sweeper,
one pending freeze request per account, shared findings, and case merging."""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.app import coordination as co
from backend.app import memory
from backend.app import queries as q
from backend.app import scenario as s
from backend.app.contracts import MAX_ATTEMPTS
from backend.app.db import get_db
from backend.app.overlay import CASES_PATH

pytestmark = pytest.mark.graph

CASES = {c["case_id"]: c for c in json.loads(CASES_PATH.read_text())["cases"]}
FIRST, SECOND = CASES["c01"], CASES["c07"]


@pytest.fixture
def queue_key():
    """A small graph holding only alerts, for queue mechanics under contention."""
    key = f"fintrace:test-queue-{uuid.uuid4().hex[:8]}"
    yield key
    if key in get_db().list_graphs():
        get_db().select_graph(key).delete()


def plant_alerts(key: str, n: int):
    graph = get_db().select_graph(key)
    graph.query("""
        UNWIND range(1, $n) AS i
        CREATE (:Entity:Alert {id: 'alert-' + toString(i), status: 'PENDING', priority: i % 5, created_at: i,
                               subject_id: 'acct-' + toString(i)})
    """, {"n": n})
    return graph


def attempts(graph, alert_id: str) -> list[dict]:
    rows = graph.ro_query("""
        MATCH (:Entity:Alert {id: $id})<-[:FOR_ALERT]-(a:Attempt)
        RETURN a {.worker_id, .attempt, .status, .refused} ORDER BY a.attempt
    """, {"id": alert_id}).result_set
    return [dict(a) for (a,) in rows]


def test_claims_take_the_highest_priority_alert_and_record_an_attempt(queue_key):
    graph = plant_alerts(queue_key, 6)
    first, second = co.claim(graph, "w1"), co.claim(graph, "w2")
    assert (first["id"], second["id"]) == ("alert-4", "alert-3")
    assert first["status"] == "CLAIMED" and first["attempt"] == 1 and first["case_id"] == "case-4"
    assert attempts(graph, "alert-4") == [{"worker_id": "w1", "attempt": 1, "status": "CLAIMED", "refused": None}]
    assert co.complete(graph, "alert-4", first["token"], {"verdict": "CLEAR", "case_id": "case-4"})
    assert not co.complete(graph, "alert-4", first["token"], {"verdict": "CLEAR"}), "completed twice"
    assert not co.complete(graph, "alert-3", first["token"], {}), "a token from another alert committed"


def test_an_expired_claim_goes_back_to_the_queue_and_its_token_can_no_longer_commit(queue_key):
    graph = plant_alerts(queue_key, 1)
    slow = co.claim(graph, "slow", now=1000, lease_seconds=10)
    assert co.renew(graph, "alert-1", slow["token"], now=1005, lease_seconds=10)
    assert co.sweep(graph, now=1012)["expired"] == [], "a renewed lease was swept"
    assert co.sweep(graph, now=1016)["expired"] == ["alert-1"]
    assert not co.renew(graph, "alert-1", slow["token"], now=1017)
    fast = co.claim(graph, "fast", now=1020)
    assert fast["attempt"] == 2
    assert not co.complete(graph, "alert-1", slow["token"], {"verdict": "SUSPICIOUS"})
    assert co.complete(graph, "alert-1", fast["token"], {"verdict": "CLEAR"})
    history = attempts(graph, "alert-1")
    assert [(a["worker_id"], a["status"]) for a in history] == [("slow", "EXPIRED"), ("fast", "COMPLETED")]
    assert history[0]["refused"].startswith("completion refused")
    assert graph.ro_query("MATCH (al:Alert) RETURN al.verdict, al.completed_by").result_set == [["CLEAR", "fast"]]


def test_failed_alerts_are_retried_until_the_last_attempt_then_stay_failed_and_visible(queue_key):
    graph = plant_alerts(queue_key, 1)
    for n in range(1, MAX_ATTEMPTS + 1):
        alert = co.claim(graph, f"w{n}")
        assert alert["attempt"] == n
        assert co.fail(graph, alert["id"], alert["token"], f"boom {n}")
        swept = co.sweep(graph)
        assert swept["retried"] == ([] if n == MAX_ATTEMPTS else ["alert-1"])
    assert co.claim(graph, "w9") is None
    [state] = co.queue_state(graph)
    assert (state["status"], state["error"]) == ("FAILED", f"boom {MAX_ATTEMPTS}")
    assert [a["status"] for a in state["attempts"]] == ["FAILED"] * MAX_ATTEMPTS


def test_twenty_claimants_and_a_sweeper_lose_nothing_and_complete_each_alert_once(queue_key):
    graph = plant_alerts(queue_key, 200)
    lease, stop, refused = 0.3, threading.Event(), []

    def claimant(n: int) -> None:
        rng = random.Random(n)
        while True:
            alert = co.claim(graph, f"w{n:02d}", lease_seconds=lease)
            if alert is None:
                if stop.is_set():
                    return
                time.sleep(0.02)
                continue
            roll = rng.random()
            if roll < 0.08:  # crashes: never commits, so only the sweeper can recover the alert
                continue
            if roll < 0.14:  # stalls past its lease, then tries to commit anyway
                time.sleep(lease * 3)
                if not co.complete(graph, alert["id"], alert["token"], {"verdict": "CLEAR"}):
                    refused.append(alert["id"])
            elif roll < 0.20:
                co.fail(graph, alert["id"], alert["token"], "simulated failure")
            else:
                co.complete(graph, alert["id"], alert["token"], {"verdict": "CLEAR"})

    def sweeper() -> None:
        while not stop.is_set():
            co.sweep(graph)
            time.sleep(0.05)

    with ThreadPoolExecutor(max_workers=21) as pool:
        futures = [pool.submit(claimant, n) for n in range(20)] + [pool.submit(sweeper)]
        deadline = time.time() + 90
        while time.time() < deadline:
            unfinished = graph.ro_query("""
                MATCH (al:Alert)
                WHERE al.status IN ['PENDING', 'CLAIMED'] OR (al.status = 'FAILED' AND al.attempt < $max)
                RETURN count(al)
            """, {"max": MAX_ATTEMPTS}).result_set[0][0]
            if unfinished == 0:
                break
            time.sleep(0.1)
        stop.set()
        for future in futures:
            future.result()

    queue = co.queue_state(graph)
    assert len(queue) == 200 and {a["status"] for a in queue} <= {"COMPLETED", "FAILED"}
    for alert in queue:
        numbers = [a["attempt"] for a in alert["attempts"]]
        assert numbers == list(range(1, alert["attempt"] + 1)), f"{alert['id']} attempts {numbers}"
        completions = [a for a in alert["attempts"] if a["status"] == "COMPLETED"]
        if alert["status"] == "COMPLETED":
            assert [a["worker_id"] for a in completions] == [alert["completed_by"]]
        else:
            assert completions == [] and alert["attempt"] == MAX_ATTEMPTS
    retried = [a for a in queue if a["attempt"] > 1]
    assert refused and retried, "the run never exercised a lapsed lease"
    for alert_id in refused:
        history = next(a for a in queue if a["id"] == alert_id)["attempts"]
        assert any(a["refused"] and a["status"] == "EXPIRED" for a in history)


def test_three_hunters_drain_the_queue_without_losing_or_duplicating_work(fresh_key):
    graph = get_db().select_graph(fresh_key)
    summary = asyncio.run(co.drain(fresh_key, 3, mode="fallback"))
    queue = {a["id"]: a for a in summary["queue"]}
    assert len(queue) == len(CASES) and {a["status"] for a in queue.values()} == {"COMPLETED"}
    assert all(len(a["attempts"]) == 1 and a["attempts"][0]["status"] == "COMPLETED" for a in queue.values())
    assert {o["worker_id"] for o in summary["outcomes"]} == {"hunter-1", "hunter-2", "hunter-3"}
    for case in CASES.values():
        assert queue[case["alert_id"]]["verdict"] == case["expected"]["verdict"], case["case_id"]
    doubled = graph.ro_query("""
        MATCH (r:ApprovalRequest {status: 'PENDING_L3'})
        UNWIND r.target_ids AS t
        WITH t, collect(r.id) AS requests WHERE size(requests) > 1
        RETURN t, requests
    """).result_set
    assert doubled == []
    second = queue[SECOND["alert_id"]]
    assert (second["merged_into"], set(second["merged_on"])) == (
        "case-" + FIRST["alert_id"].removeprefix("alert-"),
        {"acct-thorne-freight", "acct-9330192", FIRST["expected"]["sanctioned_target"]})


def test_a_later_hunter_reuses_an_earlier_hunters_route_and_defers_to_its_freeze(fresh_key):
    graph = get_db().select_graph(fresh_key)
    [first] = asyncio.run(co.hunt(fresh_key, "hunter-1", max_alerts=1))
    assert first["alert_id"] == FIRST["alert_id"] and first["request_id"]
    events: list[dict] = []

    async def keep(event: dict) -> None:
        events.append(event)

    [second] = asyncio.run(co.hunt(fresh_key, "hunter-2", max_alerts=1, emit=keep))
    final = next(e for e in events if e["type"] == "final")
    tools = [e["tool"] for e in events if e["type"] == "tool_call"]
    assert second["alert_id"] == SECOND["alert_id"] and "explore_neighborhood" not in tools
    route = next(f for f in final["facts"] if f["predicate"] == "eligible_route_hops")
    origin = route["scope"]["shared"]
    assert (origin["worker_id"], origin["episode_id"]) == ("hunter-1", first["episode_id"])
    assert (route["object"], route["value"]) == (SECOND["expected"]["sanctioned_target"],
                                                 SECOND["expected"]["eligible_hops"])
    assert final["verdict"] == "SUSPICIOUS" and final["approval_request"] is None
    assert final["memory"]["deferred_to"] == [first["request_id"]]
    assert final["memory"]["case"]["merged_into"] == first["case_id"]
    assert any(st["text"].startswith("From shared memory: hunter-1 found a route") for st in final["statements"])
    reused = graph.ro_query("""
        MATCH (ep:Episode {id: $ep})-[:REUSED]->(f:SemanticFact)-[:FROM_EPISODE]->(src:Episode)
        RETURN src.worker_id, src.id
    """, {"ep": second["episode_id"]}).result_set
    assert reused == [["hunter-1", first["episode_id"]]]

    alert = co.raise_alert(graph, "acct-9330192", "large_outbound_wire", priority=9)
    [third] = asyncio.run(co.hunt(fresh_key, "hunter-3", max_alerts=1))
    assert third["alert_id"] == alert["id"] and third["verdict"] == "SUSPICIOUS" and third["tool_calls"] < 11
    assert third["merged_into"] == first["case_id"]


def test_two_cases_racing_to_freeze_the_same_accounts_cannot_both_file(fresh_key):
    graph = get_db().select_graph(fresh_key)
    targets = [s.NOVA_OPS, s.VOLKOV]

    def file(n: int) -> str:
        try:
            return q.request_action(graph, f"case-race-{n}", targets[n % 2:] + targets[:n % 2], [], "FREEZE",
                                    s.POLICY, s.PRIYA, "race", requested_by=f"w{n}")["request_id"]
        except ValueError as exc:
            assert "already awaiting sign-off" in str(exc)
            return "refused"

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(file, range(10)))
    assert outcomes.count("refused") == 9
    assert graph.ro_query("MATCH (r:ApprovalRequest) RETURN count(r)").result_set == [[1]]


def test_cases_merge_only_on_a_high_risk_entity_or_two_shared_entities(fresh_key):
    graph = get_db().select_graph(fresh_key)
    for n, case in enumerate(["case-a", "case-b", "case-c", "case-d", "case-e", "case-f"]):
        memory.open_case(graph, case, s.NOVAPAY, "pytest", now=1000 + n)
    assert memory.link_case(graph, "case-a", [s.NOVA_OPS], "w")["merged_into"] is None
    one = memory.link_case(graph, "case-b", [s.NOVA_OPS], "w")
    assert (one["merged_into"], one["rule"]) == (None, None), "merged on one ordinary shared account"
    memory.link_case(graph, "case-c", [s.ORION, s.BALTIC], "w")
    two = memory.link_case(graph, "case-d", [s.ORION, s.BALTIC], "w")
    assert (two["merged_into"], two["rule"]) == ("case-c", "shares two or more entities")
    memory.link_case(graph, "case-e", [s.VORTEX], "w")
    risky = memory.link_case(graph, "case-f", [s.VORTEX], "w")
    assert (risky["merged_into"], risky["rule"]) == ("case-e", "shares a high-risk entity")
    joined = memory.link_case(graph, "case-c", [s.VORTEX], "w")
    assert joined["canonical"] == "case-c"
    roots = graph.ro_query("MATCH (m:Case)-[:MERGED_INTO]->(k:Case) RETURN m.id, k.id ORDER BY m.id").result_set
    assert roots == [["case-d", "case-c"], ["case-e", "case-c"], ["case-f", "case-c"]]
