"""Learned playbooks: learned only from a run a human approved, holding roles rather than IDs, matched by topology,
and applied to held-out subjects on their own live evidence."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from backend.app import agent, memory
from backend.app import playbook as pb
from backend.app import queries as q
from backend.app import scenario as s
from backend.app.db import get_db
from backend.app.overlay import CASES_PATH

pytestmark = pytest.mark.graph

CASES = {c["case_id"]: c for c in json.loads(CASES_PATH.read_text())["cases"]}
NOVAPAY_RING = {s.NOVAPAY, s.NOVA_OPS, s.VOLKOV, s.ORION, s.BALTIC, s.VORTEX}


def run(entity_id: str, key: str, **kwargs) -> tuple[list[dict], dict]:
    events = asyncio.run(agent.collect(entity_id, graph_key=key, mode="fallback", **kwargs))
    assert events[-1]["type"] == "final", events[-1]
    return events, events[-1]


def tools_called(events: list[dict]) -> list[str]:
    return [e["tool"] for e in events if e["type"] == "tool_call"]


def learn(key: str) -> dict:
    graph = get_db().select_graph(key)
    _, final = run(s.NOVAPAY, key)
    request = final["approval_request"]["request_id"]
    assert memory.learn_playbook(graph, request) is None, "learned before anyone signed off"
    q.approve_action(graph, request, s.PRIYA)
    return memory.learn_playbook(graph, request)


def test_a_playbook_is_learned_from_an_approved_run_and_holds_roles_not_ids(fresh_key):
    book = learn(fresh_key)
    assert (book["family"], book["required_patterns"]) == (pb.FAMILY, ["counterparty_shares_device"])
    assert [step["tool"] for step in book["steps"]] == [
        "explore_neighborhood", "explore_neighborhood", "trace_money_flow", "exposure_flow", "detect_ring",
        "find_brokers", "trace_governance", "freeze_impact", "request_action"]
    assert book["steps"][2]["args"]["entity_id"] == {"role": "route.after_device"}
    assert book["steps"][3]["args"]["source_ids"] == {"role": "route.device_sides"}
    assert not re.search(r"\b(acct|merch|dev|ip|emp|pol|apr|case)-", json.dumps(book["steps"]))
    [source] = book["derived_from"]
    assert (source["subject_id"], source["approved_by"]) == (s.NOVAPAY, s.PRIYA)
    again = memory.learn_playbook(get_db().select_graph(fresh_key), source["request_id"])
    assert (again["id"], again["derived_from"]) == (book["id"], book["derived_from"])


def test_it_matches_held_out_subjects_by_topology_alone(fresh_key):
    graph = get_db().select_graph(fresh_key)
    book = learn(fresh_key)
    matched = {cid for cid, case in CASES.items()
               if [p["id"] for p in memory.recall(graph, case["subject_id"], facts=False)["playbooks"]] == [book["id"]]}
    assert matched == {"c01", "c05", "c14", "c15"}
    assert {CASES[cid]["split"] for cid in matched} == {"heldout"}


@pytest.mark.parametrize("case_id", ["c01", "c05"])
def test_a_held_out_positive_is_investigated_on_its_own_evidence(fresh_key, other_key, case_id):
    case = CASES[case_id]
    book = learn(fresh_key)
    cold_events, cold = run(case["subject_id"], other_key, memory_mode="procedures")
    events, assisted = run(case["subject_id"], fresh_key, memory_mode="procedures")
    assert cold["playbook"] is None
    assert (assisted["playbook"]["id"], assisted["playbook"]["ran"]) == (book["id"], 8)
    assert tools_called(events) == tools_called(cold_events)

    for final in (cold, assisted):
        route = next(f for f in final["facts"] if f["predicate"] == "eligible_route_hops")
        assert final["verdict"] == "SUSPICIOUS"
        assert (route["object"], route["value"]) == (case["expected"]["sanctioned_target"],
                                                     case["expected"]["eligible_hops"])
        request = final["approval_request"]
        assert request["status"] == "PENDING_L3" and case["subject_id"] in request["target_ids"]
        assert not set(request["target_ids"]) & NOVAPAY_RING

    graph = get_db().select_graph(fresh_key)
    used = graph.ro_query("MATCH (:Episode {id: $ep})-[:USED_PLAYBOOK]->(p:Playbook) RETURN p.id, p.uses",
                          {"ep": assisted["memory"]["episode_id"]}).result_set
    assert used == [[book["id"], 1]]


def test_on_a_lookalike_it_stops_where_the_evidence_does_and_the_answer_stays_clear(fresh_key):
    book = learn(fresh_key)
    case = CASES["c14"]
    events, final = run(case["subject_id"], fresh_key, memory_mode="procedures")
    assert final["playbook"]["id"] == book["id"] and final["playbook"]["ran"] == 2
    assert final["playbook"]["stopped"].startswith("step 3 (trace_money_flow) needs the account just past")
    assert final["verdict"] == "CLEAR" and final["approval_request"] is None
    assert tools_called(events) == ["resolve_entity", "recall_memory", "explore_neighborhood", "explore_neighborhood"]


def test_remembered_findings_take_precedence_and_procedures_mode_never_reads_them(fresh_key):
    learn(fresh_key)
    events, warm = run(s.NOVAPAY, fresh_key)
    assert warm["playbook"] is None and tools_called(events) == ["resolve_entity", "recall_memory"]
    events, procedures = run(s.NOVAPAY, fresh_key, memory_mode="procedures")
    recall = next(e for e in events if e["type"] == "tool_result" and e["tool"] == "recall_memory")
    assert recall["data"]["facts"] == [] and recall["data"]["approvals"] == []
    assert procedures["playbook"]["ran"] == 8 and procedures["answer_state"] == "answered_with_path"
