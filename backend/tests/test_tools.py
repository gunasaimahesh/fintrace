"""The agent tools on a copy of the seeded graph: envelopes, governance, collateral and the freeze workflow."""

from __future__ import annotations

import pytest

from backend.app import queries as q
from backend.app import scenario as s
from backend.app import tools as t
from backend.app.db import get_db

pytestmark = pytest.mark.graph


@pytest.fixture(scope="module")
def ctx(scratch_key: str) -> t.ToolContext:
    return t.ToolContext(get_db().select_graph(scratch_key), worker_id="pytest")


def test_an_unknown_entity_stops_the_investigation(ctx):
    result = t.resolve_entity(ctx, entity_id=s.UNKNOWN)
    assert result["ok"] and result["stop"] == "unknown_entity"
    assert result["data"] == {"entity_id": s.UNKNOWN, "found": False}


def test_bad_arguments_come_back_as_errors_the_model_can_act_on(ctx):
    assert "is not a valid entity ID" in t.resolve_entity(ctx, entity_id="x' OR 1=1 //")["error"]
    assert t.explore_neighborhood(ctx, entity_id=s.NOVAPAY, depth=9)["error"] == "depth must be between 1 and 6"
    assert t.path_between(ctx, from_id=s.NOVAPAY, to_id=s.UNKNOWN)["error"] == f"not in the graph: ['{s.UNKNOWN}']"


def test_results_carry_parameterized_cypher_and_evidence(ctx):
    result = t.explore_neighborhood(ctx, entity_id=s.NOVAPAY, depth=5)
    queries = result["meta"]["queries"]
    assert queries and all(s.NOVAPAY not in entry["cypher"] for entry in queries)
    vortex = next(f for f in result["data"]["flagged"] if f["entity"]["id"] == s.VORTEX)
    assert vortex["depth"] == 5
    assert vortex["identity_hops"] == [{"id": s.SHARED_DEVICE, "type": "Device", "accounts_on_path": [s.NOVA_OPS, s.VOLKOV]}]
    assert [s.VOLKOV, "USED_DEVICE", s.SHARED_DEVICE] in result["evidence"]["edges"]


def test_money_trail_is_time_ordered(ctx):
    data = t.trace_money_flow(ctx, entity_id=s.VOLKOV, direction="out", max_hops=4)["data"]
    trail = data["sanctioned"][0]["trail"]
    assert [(leg["from"], leg["to"]) for leg in trail] == [(s.VOLKOV, s.BALTIC), (s.BALTIC, s.VORTEX)]
    assert trail[0]["time"] <= trail[1]["time"]


def test_governance_finds_the_breach_and_the_on_call_approver(ctx):
    data = t.trace_governance(ctx, entity_id=s.NOVAPAY)["data"]
    waiver = next(d for d in data["decisions"] if d["id"] == s.WAIVER)
    assert waiver["approved_by"]["id"] == s.ROHAN
    assert [v["policy_id"] for v in waiver["violations"]] == [s.POLICY]
    assert {src["id"] for src in waiver["sources"]} == {s.TICKET, s.MESSAGE}
    assert [(o["decision_id"], o["violations"]) for o in data["approver_other_decisions"]] == [("dec-waiver-81", [])]
    assert [p["id"] for p in data["on_call_approvers"]] == [s.PRIYA]


def test_freeze_impact_protects_the_vip_and_keeps_the_proxy_ip_open(ctx):
    data = t.freeze_impact(ctx, target_ids=[s.NOVA_OPS, s.VOLKOV])["data"]
    assert [c["id"] for c in data["collateral"]] == [s.VIP]
    safe = {inf["id"]: inf["safe_to_block"] for inf in data["infrastructure"]}
    assert safe[s.SHARED_DEVICE] is True and safe[s.PROXY_IP] is False


def test_a_freeze_needs_an_on_call_l3_and_happens_only_on_sign_off(fresh_key):
    graph = get_db().select_graph(fresh_key)
    ctx = t.ToolContext(graph, worker_id="pytest")
    args = {"target_ids": [s.VOLKOV, s.BALTIC], "policy_id": s.POLICY, "reason": "pytest", "case_id": "case-pytest",
            "excluded_ids": [s.VIP]}

    refused = t.request_action(ctx, **args, approver_id=s.ROHAN)
    assert not refused["ok"] and "Use trace_governance" in refused["error"]

    request = t.request_action(ctx, **args, approver_id=s.PRIYA)
    assert request["ok"] and request["data"]["status"] == "PENDING_L3" and request["data"]["created"]
    repeat = t.request_action(ctx, **args, approver_id=s.PRIYA)["data"]
    assert (repeat["request_id"], repeat["created"]) == (request["data"]["request_id"], False)
    assert t.resolve_entity(ctx, entity_id=s.VOLKOV)["data"]["status"] == "ACTIVE"

    with pytest.raises(ValueError, match="requires L3"):
        q.approve_action(graph, request["data"]["request_id"], s.ROHAN)
    q.approve_action(graph, request["data"]["request_id"], s.PRIYA)
    status = {e: t.resolve_entity(ctx, entity_id=e)["data"]["status"] for e in (s.VOLKOV, s.BALTIC, s.VIP)}
    assert status == {s.VOLKOV: "FROZEN", s.BALTIC: "FROZEN", s.VIP: "ACTIVE"}
