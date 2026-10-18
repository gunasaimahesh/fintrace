"""Hunters coordinate only through graph state: atomic claims with leases, token-checked commits, a sweeper,
one pending freeze request per account, shared findings, and case merging."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.app import agent, board, memory, mutations
from backend.app import coordination as co
from backend.app import queries as q
from backend.app import scenario as s
from backend.app.contracts import LEASE_SECONDS, MAX_ATTEMPTS, Claim
from backend.app.db import get_db
from backend.app.overlay import CASES_PATH
from backend.eval import contention
from backend.eval.contention import plant as plant_alerts

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
    assert not co.complete(graph, "alert-1", slow["token"], {"verdict": "SUSPICIOUS"}, now=1021)
    assert co.complete(graph, "alert-1", fast["token"], {"verdict": "CLEAR"}, now=1021)
    history = attempts(graph, "alert-1")
    assert [(a["worker_id"], a["status"]) for a in history] == [("slow", "EXPIRED"), ("fast", "COMPLETED")]
    assert history[0]["refused"].startswith("completion refused")
    assert graph.ro_query("MATCH (al:Alert) RETURN al.verdict, al.completed_by").result_set == [["CLEAR", "fast"]]


def test_a_lapsed_lease_cannot_renew_complete_or_fail_even_before_the_sweep(queue_key):
    graph = plant_alerts(queue_key, 2)
    held = co.claim(graph, "w1", now=1000, lease_seconds=10)
    assert co.renew(graph, held["id"], held["token"], now=1010, lease_seconds=10), "a lease holds through lease_until"
    assert co.complete(graph, held["id"], held["token"], {"verdict": "CLEAR"}, now=1020)
    late = co.claim(graph, "w2", now=1000, lease_seconds=10)
    assert not co.renew(graph, late["id"], late["token"], now=1011, lease_seconds=10)
    assert not co.complete(graph, late["id"], late["token"], {"verdict": "CLEAR"}, now=1011)
    assert not co.fail(graph, late["id"], late["token"], "boom", now=1011)
    assert attempts(graph, late["id"])[0]["refused"].startswith("failure refused: this attempt no longer holds")
    assert co.sweep(graph, now=1010)["expired"] == [], "swept at lease_until"
    assert co.sweep(graph, now=1011)["expired"] == [late["id"]]
    assert not co.complete(graph, late["id"], late["token"], {"verdict": "CLEAR"}, now=1012)


def test_writes_under_a_claim_that_passed_to_another_hunter_change_nothing(fresh_key):
    graph = get_db().select_graph(fresh_key)
    old = co.claim(graph, "old")
    co.sweep(graph, now=time.time() + LEASE_SECONDS + 1)
    new = co.claim(graph, "new")
    assert new["id"] == old["id"]
    lost, held = Claim(old["id"], old["token"]), Claim(new["id"], new["token"])
    assert not memory.open_case(graph, "case-lost", s.NOVAPAY, "old", claim=lost)
    assert memory.open_case(graph, "case-held", s.NOVAPAY, "new", claim=held)
    with pytest.raises(ValueError, match="old no longer holds the claim"):
        q.request_action(graph, "case-lost", [s.NOVA_OPS], [], "FREEZE", s.POLICY, s.PRIYA, "late", requested_by="old",
                         claim=lost)
    assert memory.link_case(graph, "case-held", [s.VORTEX], "old", claim=lost) is None
    q.request_action(graph, "case-held", [s.NOVA_OPS], [], "FREEZE", s.POLICY, s.PRIYA, "held", requested_by="new",
                     claim=held)
    assert graph.ro_query("""
        OPTIONAL MATCH (c:Case {id: 'case-lost'})
        OPTIONAL MATCH (:Case {id: 'case-held'})-[e:EVIDENCE]->()
        OPTIONAL MATCH (r:ApprovalRequest)
        RETURN count(DISTINCT c), count(DISTINCT e), collect(DISTINCT [r.requested_by, r.depends_on])
    """).result_set == [[0, 0, [["new", [s.NOVA_OPS]]]]]


def test_a_hunter_whose_claim_passed_on_mid_run_publishes_nothing_and_the_new_owner_finishes(fresh_key, monkeypatch):
    graph = get_db().select_graph(fresh_key)
    plan, gate = agent.fallback_plan, {}

    async def paused(inv):
        if inv.worker_id == "old":
            gate["paused"].set()
            await gate["resume"].wait()
        await plan(inv)

    monkeypatch.setattr(agent, "fallback_plan", paused)

    async def scenario():
        gate.update(paused=asyncio.Event(), resume=asyncio.Event())
        alert = await asyncio.to_thread(co.claim, graph, "old")
        old = asyncio.create_task(co.work(fresh_key, "old", alert))
        await gate["paused"].wait()
        await asyncio.to_thread(co.sweep, graph, time.time() + LEASE_SECONDS + 1)
        taken = await asyncio.to_thread(co.claim, graph, "new")
        gate["resume"].set()
        first = await old
        evidence = graph.ro_query("MATCH (:Case {id: $c})-[e:EVIDENCE]->() RETURN count(e)",
                                  {"c": alert["case_id"]}).result_set[0][0]
        return alert, taken, first, evidence, await co.work(fresh_key, "new", taken)

    alert, taken, first, evidence, second = asyncio.run(scenario())
    assert (taken["id"], taken["attempt"]) == (alert["id"], 2)
    assert first["verdict"] == "SUSPICIOUS" and not first["committed"] and first["request_id"] is None
    assert evidence == 0, "the old hunter attached evidence to the case"
    [(lost, cached, note)] = graph.ro_query(
        "MATCH (ep:Episode {worker_id: 'old'}) RETURN ep.claim_lost, ep.facts_cached, ep.cache_note").result_set
    assert lost and cached is False and "old no longer held its claim" in note
    assert graph.ro_query("""
        OPTIONAL MATCH (f:SemanticFact)-[:FROM_EPISODE]->(:Episode {worker_id: 'old'})
        OPTIONAL MATCH (r:ApprovalRequest {requested_by: 'old'})
        RETURN count(f), count(r)
    """).result_set == [[0, 0]]

    assert second["committed"] and second["verdict"] == "SUSPICIOUS" and second["request_id"]
    [state] = [a for a in co.queue_state(graph) if a["id"] == alert["id"]]
    assert (state["status"], state["completed_by"], state["episode_id"]) == ("COMPLETED", "new", second["episode_id"])
    assert [(a["worker_id"], a["status"]) for a in state["attempts"]] == [("old", "EXPIRED"), ("new", "COMPLETED")]
    current = graph.ro_query("MATCH (f:SemanticFact {status: 'CURRENT'})-[:ABOUT]->(:Entity {id: $x}) "
                             "RETURN collect(DISTINCT f.episode_id)", {"x": alert["subject_id"]}).result_set[0][0]
    assert current == [second["episode_id"]]
    assert [e["id"] for e in memory.recall(graph, alert["subject_id"])["episodes"]] == [second["episode_id"]]


def test_a_hunter_whose_claim_passes_on_just_after_it_files_leaves_one_complete_request(fresh_key, monkeypatch):
    graph = get_db().select_graph(fresh_key)
    call, gate = agent.Investigation.call, {}

    async def call_then_pause(inv, tool, args, thought=None):
        envelope = await call(inv, tool, args, thought)
        if tool == "request_action" and inv.worker_id == "old":
            gate["filed"].set()
            await gate["resume"].wait()
        return envelope

    monkeypatch.setattr(agent.Investigation, "call", call_then_pause)

    async def scenario():
        gate.update(filed=asyncio.Event(), resume=asyncio.Event())
        alert = await asyncio.to_thread(co.claim, graph, "old")
        old = asyncio.create_task(co.work(fresh_key, "old", alert))
        await gate["filed"].wait()
        await asyncio.to_thread(co.sweep, graph, time.time() + LEASE_SECONDS + 1)
        taken = await asyncio.to_thread(co.claim, graph, "new")
        gate["resume"].set()
        return await old, await co.work(fresh_key, "new", taken)

    first, second = asyncio.run(scenario())
    assert not first["committed"] and second["committed"] and second["verdict"] == "SUSPICIOUS"
    [(request, by, targets, linked, depends_on)] = graph.ro_query("""
        MATCH (r:ApprovalRequest)
        OPTIONAL MATCH (r)-[:TARGETS]->(t:Account)
        RETURN r.id, r.requested_by, r.target_ids, collect(t.id), r.depends_on
    """).result_set
    assert by == "old" and sorted(linked) == sorted(targets)
    pinned = graph.ro_query("MATCH (n:Sanctioned) WHERE n.id IN $deps RETURN collect(n.id)",
                            {"deps": depends_on}).result_set[0][0]
    assert pinned and any("|" in d for d in depends_on), "the request does not rest on the route it was filed on"
    assert q.approve_action(graph, request, s.PRIYA)["frozen"] == sorted(targets)


def test_a_hunter_stops_once_it_notices_its_lease_is_gone(fresh_key, monkeypatch):
    graph = get_db().select_graph(fresh_key)
    plan, gate = agent.fallback_plan, {}

    async def stuck(inv):
        gate["started"].set()
        await asyncio.sleep(30)
        await plan(inv)

    monkeypatch.setattr(agent, "fallback_plan", stuck)

    async def scenario():
        gate["started"] = asyncio.Event()
        alert = await asyncio.to_thread(co.claim, graph, "slow", lease_seconds=0.6)
        slow = asyncio.create_task(co.work(fresh_key, "slow", alert, lease_seconds=0.6))
        await gate["started"].wait()
        await asyncio.to_thread(co.sweep, graph, time.time() + 1)
        await asyncio.to_thread(co.claim, graph, "fast")
        return await asyncio.wait_for(slow, 5)

    outcome = asyncio.run(scenario())
    assert outcome["lost_lease"] and outcome["verdict"] is None and not outcome["committed"]
    assert outcome["error"].startswith("the claim was lost")


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
    result = contention.run(queue_key, claimants=20, alerts=200)
    assert result["violations"] == []
    assert sum(result["statuses"].values()) == 200 and set(result["statuses"]) <= {"COMPLETED", "FAILED"}
    assert result["late_commits_refused"] and result["reclaimed"], "the run never exercised a lapsed lease"


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

    persisted = board.board(graph)
    episodes = {ep["id"]: ep for ep in persisted["episodes"]}
    assert [r["episode_id"] for r in episodes[second["episode_id"]]["reused"]] == [first["episode_id"]]
    assert [d["request_id"] for d in episodes[second["episode_id"]]["deferred_to"]] == [first["request_id"]]
    merged = {c["id"]: c["merged_into"] for c in persisted["cases"] if c["merged_into"]}
    assert merged == {second["case_id"]: first["case_id"], third["case_id"]: first["case_id"]}
    shared = [f for f in persisted["facts"]["recent"] if f["shared"]]
    assert shared and all(f["status"] == "CURRENT" for f in shared)

    mutations.add_transfer(graph, "acct-9330192", FIRST["expected"]["sanctioned_target"], 950_000)
    after = {f["id"]: f for f in board.board(graph)["facts"]["recent"]}
    assert all(after[f["id"]]["status"] == "STALE" and "acct-9330192" in after[f["id"]]["changed"] for f in shared)


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
