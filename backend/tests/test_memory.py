"""Memory across sessions, revisions and mutations, each test on a private copy of the seeded graph."""

from __future__ import annotations

import asyncio
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.app import agent, memory
from backend.app import company as c
from backend.app import mutations as m
from backend.app import queries as q
from backend.app import scenario as s
from backend.app.contracts import current_rev
from backend.app.db import get_db

pytestmark = pytest.mark.graph


def run(entity_id: str, key: str, **kwargs) -> tuple[list[dict], dict]:
    events = asyncio.run(agent.collect(entity_id, graph_key=key, mode="fallback", **kwargs))
    assert events[-1]["type"] == "final", events[-1]
    return events, events[-1]


def tools_called(events: list[dict]) -> list[str]:
    return [e["tool"] for e in events if e["type"] == "tool_call"]


def recalled(graph, entity_id: str, predicate: str, subject: str | None = None) -> dict:
    return next(f for f in memory.recall(graph, entity_id)["facts"]
                if f["predicate"] == predicate and (subject is None or f["subject"] == subject))


def statuses(graph, entity_id: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for f in memory.recall(graph, entity_id)["facts"]:
        out.setdefault(f["status"], set()).add(f["predicate"])
    return out


def summary(graph, a: str, b: str) -> tuple[list, list]:
    raw = graph.ro_query("MATCH (:Account {id: $a})-[t:TRANSFERRED]->(:Account {id: $b}) RETURN count(t), sum(t.amount)",
                         {"a": a, "b": b}).result_set[0]
    flow = graph.ro_query("MATCH (:Account {id: $a})-[f:FLOWS_TO]->(:Account {id: $b}) RETURN f.transfers, f.amount",
                          {"a": a, "b": b}).result_set
    return raw, flow


def route_transfer(graph) -> str:
    return graph.ro_query("MATCH (:Account {id: $a})-[t:TRANSFERRED]->(:Account {id: $b}) RETURN t.tx_id ORDER BY t.ts",
                          {"a": s.VOLKOV, "b": s.BALTIC}).result_set[0][0]


def test_a_new_session_answers_from_memory(fresh_key):
    graph = get_db().select_graph(fresh_key)
    _, first = run(s.NOVAPAY, fresh_key)
    assert first["memory"]["cached"] and first["memory"]["facts_written"] >= 6
    events, second = run(s.NOVAPAY, fresh_key)
    assert tools_called(events) == ["resolve_entity", "recall_memory"]
    assert (second["verdict"], second["answer_state"]) == ("SUSPICIOUS", "answered_from_memory")
    assert second["statements"][0]["text"].startswith("From memory: ")
    assert "waiting for Priya Nair (emp-priya-nair, L3) to sign off" in second["summary"]
    assert first["memory"]["episode_id"] in [e["id"] for e in memory.recall(graph, s.NOVAPAY)["episodes"]]


def test_an_unrelated_transfer_keeps_the_route_and_refreshes_only_the_aggregates(fresh_key):
    graph = get_db().select_graph(fresh_key)
    run(s.NOVAPAY, fresh_key)
    m.add_transfer(graph, s.ISOLATED, s.VIP, 500_000)
    assert statuses(graph, s.NOVAPAY)["STALE"] == {"max_flow_capacity", "ring_size", "top_broker"}
    stale = {f["id"] for f in memory.recall(graph, s.NOVAPAY)["facts"] if f["status"] == "STALE"}

    events, again = run(s.NOVAPAY, fresh_key)
    assert tools_called(events) == ["resolve_entity", "recall_memory", "exposure_flow", "detect_ring", "find_brokers"]
    assert (again["verdict"], again["answer_state"]) == ("SUSPICIOUS", "answered_from_memory")
    assert set(again["memory"]["superseded"]) == stale
    assert "STALE" not in statuses(graph, s.NOVAPAY)
    edges = graph.ro_query("MATCH (n:SemanticFact)-[:SUPERSEDES]->(o:SemanticFact) "
                           "RETURN n.predicate, o.status ORDER BY n.predicate").result_set
    assert edges == [["max_flow_capacity", "SUPERSEDED"], ["ring_size", "SUPERSEDED"], ["top_broker", "SUPERSEDED"]]


def test_deleting_a_route_transfer_updates_its_summary_and_stales_the_route(fresh_key):
    graph = get_db().select_graph(fresh_key)
    run(s.NOVAPAY, fresh_key)
    (count, amount), _ = summary(graph, s.VOLKOV, s.BALTIC)
    m.delete_transfer(graph, route_transfer(graph))
    raw, flow = summary(graph, s.VOLKOV, s.BALTIC)
    assert raw[0] == count - 1 and (flow == [raw] if raw[0] else flow == [])
    route = recalled(graph, s.NOVAPAY, "eligible_route_hops")
    assert route["status"] == "STALE" and s.VOLKOV in route["changed"] and s.BALTIC in route["changed"]


def test_deleting_a_route_node_stales_the_route_through_a_missing_dependency(fresh_key):
    graph = get_db().select_graph(fresh_key)
    run(s.NOVAPAY, fresh_key)
    m.delete_entity(graph, s.BALTIC)
    route = recalled(graph, s.NOVAPAY, "eligible_route_hops")
    assert route["status"] == "STALE" and route["why_stale"].startswith(
        f"{s.BALTIC}, {s.VOLKOV} → {s.BALTIC} (FLOWS_TO) and {s.BALTIC} → {s.VORTEX} (FLOWS_TO) no longer exist; ")
    events, again = run(s.NOVAPAY, fresh_key)
    assert "explore_neighborhood" in tools_called(events)
    assert again["answer_state"] == "memory_stale_reinvestigated"


def test_a_change_in_the_same_second_as_the_fact_still_stales_it(fresh_key):
    graph = get_db().select_graph(fresh_key)
    events, final = run(s.NOVAPAY, fresh_key, memory_mode="off")
    results = [e for e in events if e["type"] == "tool_result"]
    second = 1_800_000_000
    memory.persist_run(graph, case_id="case-same-second", entity_id=s.NOVAPAY, worker_id="pytest",
                       planner="fallback", model=None, start_rev=current_rev(graph), results=results, final=final,
                       now=float(second))
    m.add_transfer(graph, s.VOLKOV, s.BALTIC, 100, ts=second)
    assert recalled(graph, s.NOVAPAY, "eligible_route_hops")["status"] == "STALE"


def test_a_change_during_the_investigation_is_not_cached(fresh_key, monkeypatch):
    graph = get_db().select_graph(fresh_key)
    persist = memory.persist_run

    def concurrent_change(graph_, **kwargs):
        m.add_transfer(graph_, s.ISOLATED, s.VIP, 100)
        return persist(graph_, **kwargs)

    monkeypatch.setattr(memory, "persist_run", concurrent_change)
    _, final = run(s.NOVAPAY, fresh_key)
    assert not final["memory"]["cached"] and final["memory"]["facts_written"] == 0
    assert "during the investigation" in final["memory"]["note"]
    recall = memory.recall(graph, s.NOVAPAY)
    assert recall["facts"] == [] and recall["episodes"][0]["facts_cached"] is False


def test_a_policy_edit_refreshes_the_waiver_check_and_keeps_the_route(fresh_key, tmp_path):
    graph = get_db().select_graph(fresh_key)
    run(s.NOVAPAY, fresh_key)
    sources = Path(shutil.copytree(c.COMPANY_DIR, tmp_path / "company"))
    policy = sources / "policies" / f"{s.POLICY}.md"
    policy.write_text(policy.read_text().replace('version = "2026.2"', 'version = "2026.3"')
                      .replace("waiver_threshold_monthly = 25_000_000", "waiver_threshold_monthly = 50_000_000"))
    assert c.sync_company(graph, sources)["changed"] == [s.POLICY]
    assert recalled(graph, s.NOVAPAY, "eligible_route_hops")["status"] == "CURRENT"
    waiver = recalled(graph, s.NOVAPAY, "waiver_compliance", s.WAIVER)
    assert waiver["status"] == "STALE" and waiver["why_stale"] == f"{s.POLICY} changed after graph revision 0"

    events, again = run(s.NOVAPAY, fresh_key)
    assert "trace_governance" in tools_called(events) and "explore_neighborhood" not in tools_called(events)
    assert (again["verdict"], again["answer_state"]) == ("SUSPICIOUS", "answered_from_memory")
    assert f"That waiver is within {s.POLICY} (version 2026.3)." in [st["text"] for st in again["statements"]]


def test_a_clear_finding_goes_stale_on_any_change(fresh_key):
    graph = get_db().select_graph(fresh_key)
    _, first = run(s.VIP, fresh_key)
    assert first["verdict"] == "CLEAR"
    m.add_transfer(graph, s.ISOLATED, s.LUMEN, 100)
    assert recalled(graph, s.VIP, "no_eligible_route")["status"] == "STALE"
    events, again = run(s.VIP, fresh_key)
    assert tools_called(events).count("explore_neighborhood") == 2
    assert (again["verdict"], again["answer_state"]) == ("CLEAR", "memory_stale_reinvestigated")


def test_sign_off_is_refused_once_the_evidence_changed(fresh_key):
    graph = get_db().select_graph(fresh_key)
    _, final = run(s.NOVAPAY, fresh_key)
    request = final["approval_request"]["request_id"]
    m.delete_transfer(graph, route_transfer(graph))
    with pytest.raises(ValueError, match="is now STALE"):
        q.approve_action(graph, request, s.PRIYA)
    assert q.approval_statuses(graph, [request]) == {request: "STALE"}
    frozen = graph.ro_query("MATCH (n:Entity) WHERE n.status = 'FROZEN' RETURN count(n)").result_set[0][0]
    assert frozen == 0


def test_sign_off_still_works_after_an_unrelated_change(fresh_key):
    graph = get_db().select_graph(fresh_key)
    _, final = run(s.NOVAPAY, fresh_key)
    m.add_transfer(graph, s.ISOLATED, s.VIP, 100)
    assert q.approve_action(graph, final["approval_request"]["request_id"], s.PRIYA)["status"] == "APPROVED"


def test_a_sanctions_designation_decides_while_it_holds_and_goes_stale_when_lifted(fresh_key):
    graph = get_db().select_graph(fresh_key)
    assert run(s.ISOLATED, fresh_key)[1]["verdict"] == "CLEAR"
    m.set_sanctioned(graph, s.ISOLATED, True, "EU-CONSOLIDATED")
    _, listed = run(s.ISOLATED, fresh_key)
    assert listed["verdict"] == "SUSPICIOUS" and listed["approval_request"] is None
    basis = next(f for f in listed["facts"] if f["id"] == listed["verdict_basis"][0])
    assert (basis["predicate"], basis["value"]) == ("subject_sanctioned", "EU-CONSOLIDATED")
    assert recalled(graph, s.ISOLATED, "subject_sanctioned")["status"] == "CURRENT"

    m.set_sanctioned(graph, s.ISOLATED, False)
    designation = recalled(graph, s.ISOLATED, "subject_sanctioned")
    assert designation["status"] == "STALE" and designation["changed"] == [s.ISOLATED]
    _, lifted = run(s.ISOLATED, fresh_key)
    assert (lifted["verdict"], lifted["answer_state"]) == ("CLEAR", "memory_stale_reinvestigated")
    assert not [f for f in lifted["facts"] if f["predicate"] == "subject_sanctioned"]
    assert any(st["text"].startswith("The remembered sanctions designation went stale") for st in lifted["statements"])
    left = graph.ro_query("MATCH (f:SemanticFact {predicate: 'subject_sanctioned'}) RETURN collect(f.status)")
    assert left.result_set[0][0] == ["SUPERSEDED"]


def pending_request(key: str) -> str:
    _, final = run(s.NOVAPAY, key)
    return final["approval_request"]["request_id"]


def signed_off(graph, request: str) -> tuple[str, int, list[str]]:
    status = q.approval_statuses(graph, [request])[request]
    signatures = graph.ro_query("MATCH (:Employee)-[s:SIGNED_OFF]->(:ApprovalRequest {id: $r}) RETURN count(s)",
                                {"r": request}).result_set[0][0]
    frozen = graph.ro_query("MATCH (a:Account) WHERE a.frozen_by = $r RETURN collect(a.id)",
                            {"r": request}).result_set[0][0]
    return status, signatures, sorted(frozen)


def during_check(monkeypatch, change) -> None:
    """Run `change` once, after sign-off has checked the evidence and before it writes."""
    check, pending = q.changed_dependencies, [change]

    def checked(graph, depends_on, rev):
        result = check(graph, depends_on, rev)
        while pending:
            pending.pop()(graph)
        return result

    monkeypatch.setattr(q, "changed_dependencies", checked)


def test_evidence_removed_while_sign_off_checks_it_freezes_nothing(fresh_key, monkeypatch):
    graph = get_db().select_graph(fresh_key)
    request = pending_request(fresh_key)
    during_check(monkeypatch, lambda g: m.delete_identity_link(g, s.NOVA_OPS, s.SHARED_DEVICE, "USED_DEVICE"))
    with pytest.raises(ValueError, match="is now STALE"):
        q.approve_action(graph, request, s.PRIYA)
    assert signed_off(graph, request) == ("STALE", 0, [])


def test_an_unrelated_change_while_sign_off_checks_still_lets_it_through(fresh_key, monkeypatch):
    graph = get_db().select_graph(fresh_key)
    request = pending_request(fresh_key)
    targets = q.list_approvals(graph, "PENDING_L3")[0]["target_ids"]
    during_check(monkeypatch, lambda g: m.add_transfer(g, s.ISOLATED, s.VIP, 100))
    approved = q.approve_action(graph, request, s.PRIYA)
    assert approved["status"] == "APPROVED" and signed_off(graph, request) == ("APPROVED", 1, sorted(targets))


def test_a_policy_raising_its_clearance_while_sign_off_checks_is_honoured(fresh_key, monkeypatch):
    graph = get_db().select_graph(fresh_key)
    request = pending_request(fresh_key)
    during_check(monkeypatch, lambda g: g.query("MATCH (p:Entity {id: $p}) SET p.required_clearance_level_for_freeze "
                                                "= 4, p.required_clearance_for_freeze = 'L4'", {"p": s.POLICY}))
    with pytest.raises(ValueError, match="this action requires L4"):
        q.approve_action(graph, request, s.PRIYA)
    assert signed_off(graph, request) == ("PENDING_L3", 0, [])


def test_racing_sign_offs_approve_once(fresh_key, monkeypatch):
    graph = get_db().select_graph(fresh_key)
    request = pending_request(fresh_key)
    both_checked, check = threading.Barrier(2, timeout=10), q.changed_dependencies

    def checked(graph_, depends_on, rev):
        result = check(graph_, depends_on, rev)
        if not both_checked.broken:
            try:
                both_checked.wait()
            except threading.BrokenBarrierError:
                pass
        return result

    monkeypatch.setattr(q, "changed_dependencies", checked)

    def sign(_) -> str:
        try:
            return q.approve_action(get_db().select_graph(fresh_key), request, s.PRIYA)["status"]
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(sign, range(2)))
    assert outcomes == ["APPROVED", f"{request} is already APPROVED"]
    assert signed_off(graph, request)[:2] == ("APPROVED", 1)


def test_transfer_writes_keep_raw_and_summary_edges_consistent(fresh_key):
    graph = get_db().select_graph(fresh_key)
    before = current_rev(graph)
    first = m.add_transfer(graph, s.ISOLATED, s.LUMEN, 1_234_500)["tx_id"]
    assert summary(graph, s.ISOLATED, s.LUMEN) == ([1, 1_234_500], [[1, 1_234_500]])
    second = m.add_transfer(graph, s.ISOLATED, s.LUMEN, 10_000)["tx_id"]
    assert summary(graph, s.ISOLATED, s.LUMEN) == ([2, 1_244_500], [[2, 1_244_500]])
    m.update_transfer(graph, first, 100)
    assert summary(graph, s.ISOLATED, s.LUMEN) == ([2, 10_100], [[2, 10_100]])
    m.delete_transfer(graph, first)
    m.delete_transfer(graph, second)
    assert summary(graph, s.ISOLATED, s.LUMEN) == ([0, 0], [])
    assert current_rev(graph) == before + 5
    revs = graph.ro_query("MATCH (n:Entity) WHERE n.id IN $ids RETURN collect(DISTINCT n.topology_rev)",
                          {"ids": [s.ISOLATED, s.LUMEN]}).result_set[0][0]
    assert revs == [before + 5]


def test_identity_and_sanction_changes_bump_the_revision(fresh_key):
    graph = get_db().select_graph(fresh_key)
    before = current_rev(graph)
    m.add_identity_link(graph, s.ISOLATED, s.SHARED_DEVICE, "USED_DEVICE")
    _, linked = run(s.ISOLATED, fresh_key, memory_mode="off")
    assert linked["verdict"] == "SUSPICIOUS"
    m.set_sanctioned(graph, s.LUMEN, True, "OFAC-SDN")
    m.set_sanctioned(graph, s.LUMEN, False)
    m.delete_identity_link(graph, s.ISOLATED, s.SHARED_DEVICE, "USED_DEVICE")
    assert current_rev(graph) == before + 4
    with pytest.raises(ValueError, match="no account"):
        m.add_transfer(graph, s.ISOLATED, "acct-does-not-exist", 100)
    assert current_rev(graph) == before + 4
