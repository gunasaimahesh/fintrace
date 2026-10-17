"""The investigation agent end to end over in-process MCP, on a copy of the seeded graph.

The LLM path runs against a scripted stand-in for the OpenAI client, so it needs no API key.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from itertools import count
from types import SimpleNamespace

import pytest

from backend.app import agent
from backend.app import queries as q
from backend.app import scenario as s
from backend.app.config import get_settings
from backend.app.db import get_db

pytestmark = pytest.mark.graph

_ids = count(1)


def run(entity_id: str, key: str, **kwargs) -> tuple[list[dict], dict]:
    events = asyncio.run(agent.collect(entity_id, graph_key=key, **kwargs))
    assert events[-1]["type"] == "final", events[-1]
    return events, events[-1]


def tools_called(events: list[dict]) -> list[str]:
    return [e["tool"] for e in events if e["type"] == "tool_call"]


def calls(*pairs: tuple[str, dict]) -> SimpleNamespace:
    return SimpleNamespace(content=None, tool_calls=[
        SimpleNamespace(id=f"call_{next(_ids)}", type="function",
                        function=SimpleNamespace(name=name, arguments=json.dumps(args))) for name, args in pairs])


def answer(**fields) -> SimpleNamespace:
    return SimpleNamespace(content=json.dumps(fields), tool_calls=None)


def fact(final: dict, fact_id: str) -> dict:
    return next(f for f in final["facts"] if f["id"] == fact_id)


def cited(final: dict) -> bool:
    ids = {f["id"] for f in final["facts"]}
    return all(st["facts"] and set(st["facts"]) <= ids for st in final["statements"])


class FakeLLM:
    """Replays scripted assistant turns in place of openai.AsyncOpenAI; a turn may be a callable or an exception."""

    def __init__(self, *turns):
        self.turns, self.requests, self.closed = list(turns), [], False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def close(self):
        self.closed = True

    async def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        if isinstance(turn, Exception):
            raise turn
        return SimpleNamespace(choices=[SimpleNamespace(message=turn(request) if callable(turn) else turn)])


@pytest.fixture(autouse=True)
def forget(scratch_key):
    """Each test starts cold: memory, cases and pending requests an earlier test wrote to the shared graph are
    removed (an account can be in only one pending request)."""
    get_db().select_graph(scratch_key).query(
        "MATCH (n) WHERE n:SemanticFact OR n:Episode OR n:Step OR n:Case OR n:ApprovalRequest DETACH DELETE n")


@pytest.fixture
def fake_llm(monkeypatch):
    settings = dataclasses.replace(get_settings(), llm_api_key="test-key", llm_model="fake-model")
    monkeypatch.setattr(agent, "get_settings", lambda: settings)

    def install(*turns) -> FakeLLM:
        llm = FakeLLM(*turns)
        monkeypatch.setattr("openai.AsyncOpenAI", lambda **_: llm)
        return llm

    return install


EXPLORE = (("explore_neighborhood", {"entity_id": s.NOVAPAY, "depth": 1}),
           ("explore_neighborhood", {"entity_id": s.NOVAPAY, "depth": 5}))


def test_fallback_investigates_novapay_and_routes_the_freeze_to_priya(scratch_key):
    events, final = run(s.NOVAPAY, scratch_key, mode="fallback")
    assert tools_called(events) == ["resolve_entity", "recall_memory", "explore_neighborhood", "explore_neighborhood",
                                    "trace_money_flow", "exposure_flow", "detect_ring", "find_brokers",
                                    "trace_governance", "freeze_impact", "request_action"]
    assert [e["args"]["depth"] for e in events if e.get("tool") == "explore_neighborhood" and e["type"] == "tool_call"] \
        == [1, 6]
    assert (final["verdict"], final["answer_state"]) == ("SUSPICIOUS", "answered_with_path")
    basis = fact(final, final["verdict_basis"][0])
    assert (basis["predicate"], basis["object"], basis["value"]) == ("eligible_route_hops", s.VORTEX, 5)
    assert cited(final) and final["badge"] == f"{len(final['statements'])} statements, each rendered from a cited tool result"
    assert final["coverage"]["eligible"]["complete"] and final["coverage"]["eligible"]["requested_depth"] == 6
    assert final["approval_request"]["approver"]["id"] == s.PRIYA
    assert final["approval_request"]["status"] == "PENDING_L3"
    assert "waiting for Priya Nair (emp-priya-nair, L3) to sign off" in final["summary"]
    assert set(final["freeze_targets"]) == {s.NOVAPAY, s.NOVA_OPS, s.VOLKOV, s.BALTIC, s.ORION}
    assert set(final["excluded"]) == {s.VIP, s.PROXY_IP}
    assert {"subject": s.ROHAN, "relation": "APPROVED", "object": s.WAIVER} in final["claims"]
    assert final["model_answer"] is None and isinstance(final["evidence_rev"], int)


def test_an_unknown_id_stops_after_one_call(scratch_key):
    events, final = run(s.UNKNOWN, scratch_key, mode="fallback")
    assert tools_called(events) == ["resolve_entity"]
    assert (final["verdict"], final["answer_state"]) == ("INSUFFICIENT", "unknown_entity")
    assert [st["text"] for st in final["statements"]] == [f"{s.UNKNOWN} is not in the graph, so nothing else was queried."]


def test_an_isolated_account_is_clear_with_the_caveat(scratch_key):
    _, final = run(s.ISOLATED, scratch_key, mode="fallback")
    assert (final["verdict"], final["answer_state"]) == ("CLEAR", "not_in_graph")
    assert final["coverage"]["eligible"]["exhausted"]
    assert final["summary"].endswith("That is not proof the account is safe.")


def test_sharing_only_a_proxy_ip_with_a_sanctioned_route_is_association_not_exposure(scratch_key):
    events, final = run(s.VIP, scratch_key, mode="fallback")
    assert len(tools_called(events)) == 4 and final["approval_request"] is None
    assert final["verdict"] == "CLEAR" and cited(final)
    assert "within 6 hops" in final["summary"] and "That is not proof the account is safe." in final["summary"]
    assert "ip-185-220-101-7 is a shared IP, not a shared device" in final["summary"]


def test_a_valid_route_is_found_despite_a_shorter_ip_path(fresh_key):
    get_db().select_graph(fresh_key).query("""
        MATCH (a:Entity {id: $a}), (b:Entity {id: $b})
        CREATE (ip:Entity:RiskNode:IP {id: 'ip-203-0-113-9', address: '203.0.113.9', kind: 'residential'})
        CREATE (a)-[:LOGGED_FROM {first_seen: 0, last_seen: 1, weight: 1}]->(ip),
               (b)-[:LOGGED_FROM {first_seen: 0, last_seen: 1, weight: 1}]->(ip)
    """, {"a": s.NOVAPAY, "b": s.BALTIC})
    _, final = run(s.NOVAPAY, fresh_key, mode="fallback")
    nearest = min((f for f in final["facts"] if f["predicate"] == "sanctioned_within_reach"), key=lambda f: f["value"])
    assert nearest["value"] == 3 and "ip-203-0-113-9" in nearest["scope"]["path"]["nodes"]
    assert final["verdict"] == "SUSPICIOUS"
    route = fact(final, final["verdict_basis"][0])
    assert route["value"] == 5 and s.SHARED_DEVICE in route["scope"]["path"]["nodes"]
    assert "ip-203-0-113-9" not in route["scope"]["path"]["nodes"]


def test_a_device_bridge_costs_two_hops_so_four_hops_clears_nobody(scratch_key, fake_llm):
    fake_llm(calls(("explore_neighborhood", {"entity_id": s.NOVAPAY, "depth": 4})),
             answer(verdict="CLEAR", statements=[], rationale="Nothing sanctioned within 4 hops."))
    _, final = run(s.NOVAPAY, scratch_key)
    assert final["verdict"] == "INSUFFICIENT"
    assert final["model_answer"]["proposed_verdict"] == "CLEAR" and not final["model_answer"]["agrees"]
    assert "covered 4 of the 6 hops required to clear an account" in final["summary"]


@pytest.mark.parametrize("budget", [{"SEARCH_NODE_BUDGET": 50}, {"SEARCH_TIME_BUDGET_S": 1e-9}],
                         ids=["node-budget", "time-budget"])
def test_a_capped_or_timed_out_search_is_insufficient_not_clear(scratch_key, monkeypatch, budget):
    for name, value in budget.items():
        monkeypatch.setattr(q, name, value)
    _, final = run(s.VIP, scratch_key, mode="fallback")
    assert final["verdict"] == "INSUFFICIENT" and cited(final)
    eligible = final["coverage"]["eligible"]
    assert not eligible["complete"] and (eligible["truncated"] or eligible["timed_out"])
    assert "does not clear it" in final["summary"]


def test_a_sanctioned_account_beyond_six_hops_gets_a_qualified_clear(fresh_key):
    graph = get_db().select_graph(fresh_key)
    graph.query("""
        MATCH (m:Entity {id: $m})
        CREATE (a:Entity:RiskNode:Account {id: 'acct-test-far-1', name: 'Far Account One', status: 'ACTIVE'}),
               (b:Entity:RiskNode:Account {id: 'acct-test-far-2', name: 'Far Account Two', status: 'ACTIVE'})
        CREATE (a)-[:TRANSFERRED {tx_id: 'test-far-1', amount: 1000000, ts: 1, channel: 'wire', weight: 1}]->(b),
               (a)-[:FLOWS_TO {amount: 1000000, transfers: 1, first_ts: 1, last_ts: 1, capacity: 10000, weight: 1}]->(b),
               (b)-[:TRANSFERRED {tx_id: 'test-far-2', amount: 1000000, ts: 2, channel: 'wire', weight: 1}]->(m),
               (b)-[:FLOWS_TO {amount: 1000000, transfers: 1, first_ts: 2, last_ts: 2, capacity: 10000, weight: 1}]->(m)
    """, {"m": s.NOVAPAY})
    _, final = run("acct-test-far-1", fresh_key, mode="fallback")
    assert final["verdict"] == "CLEAR"
    assert final["summary"].startswith("No eligible route from Far Account One (acct-test-far-1) to a sanctioned "
                                       "entity was found within 6 hops")
    beyond = q.eligible_routes(graph, q.internal_id(graph, "acct-test-far-1"), 7)
    assert [(r["entity"]["id"], r["hops"]) for r in beyond["routes"]] == [(s.VORTEX, 7)]


def test_a_route_the_model_found_from_another_account_does_not_decide_the_subject(scratch_key, fake_llm):
    fake_llm(calls(("explore_neighborhood", {"entity_id": s.VOLKOV, "depth": 6})),
             answer(verdict="SUSPICIOUS", statements=[], rationale="Volkov reaches Vortex."))
    _, final = run(s.VIP, scratch_key)
    assert final["verdict"] == "INSUFFICIENT" and final["coverage"] is None and cited(final)
    assert not final["model_answer"]["agrees"]
    assert f"That route starts at {s.VOLKOV}, not {s.VIP}, so it does not decide this verdict." in final["summary"]

    fake_llm(answer(verdict="SUSPICIOUS", statements=[], rationale="Memory shows a route."))
    _, again = run(s.VIP, scratch_key)
    remembered = [f for f in again["facts"] if f["scope"].get("memory") and f["predicate"] == "eligible_route_hops"]
    assert remembered and {f["subject"] for f in remembered} == {s.VOLKOV}
    assert again["verdict"] == "INSUFFICIENT"

    events, own = run(s.VIP, scratch_key, mode="fallback")
    assert [e["args"]["entity_id"] for e in events if e.get("tool") == "explore_neighborhood"
            and e["type"] == "tool_call"] == [s.VIP, s.VIP]
    assert own["verdict"] == "CLEAR" and own["coverage"]["eligible"]["complete"]


def test_a_complete_search_the_model_ran_from_another_account_does_not_clear_the_subject(scratch_key, fake_llm):
    fake_llm(calls(("explore_neighborhood", {"entity_id": s.ISOLATED, "depth": 1})),
             answer(verdict="CLEAR", statements=[], rationale="Nothing is reachable."))
    _, final = run(s.NOVAPAY, scratch_key)
    assert final["verdict"] == "INSUFFICIENT" and final["coverage"] is None and cited(final)
    assert (f"found no eligible route to a sanctioned entity. That is about {s.ISOLATED}, not {s.NOVAPAY}, so it "
            f"does not clear it.") in final["summary"]


def test_a_sanctioned_subject_is_suspicious_without_a_route_and_is_not_put_up_for_freezing(scratch_key):
    _, final = run(s.VORTEX, scratch_key, mode="fallback")
    assert (final["verdict"], final["answer_state"]) == ("SUSPICIOUS", "subject_sanctioned")
    basis = fact(final, final["verdict_basis"][0])
    assert (basis["predicate"], basis["subject"], basis["value"]) == ("subject_sanctioned", s.VORTEX, "OFAC-SDN")
    assert final["summary"].startswith(f"Vortex Holdings Ltd ({s.VORTEX}) is itself sanctioned (OFAC-SDN): that is "
                                       f"direct exposure")
    assert final["approval_request"] is None and s.VORTEX not in final["freeze_targets"] and cited(final)


def test_model_statements_are_bound_to_facts_or_rejected(scratch_key, fake_llm):
    statements = [
        {"subject": s.NOVAPAY, "predicate": "eligible_route_hops", "object": s.VORTEX, "value": 5},
        {"subject": s.NOVA_OPS, "predicate": "max_flow_capacity", "object": s.VORTEX, "value": 410_000},
        {"subject": s.LUMEN, "predicate": "eligible_route_hops", "object": s.VORTEX, "value": 5},
        {"subject": "apr-0000aaaa", "predicate": "action_status", "value": "APPROVED"},
    ]
    llm = fake_llm(calls(*EXPLORE),
                   calls(("exposure_flow", {"source_ids": [s.NOVA_OPS, s.VOLKOV], "target_id": s.VORTEX})),
                   answer(verdict="SUSPICIOUS", statements=statements, freeze_targets=[s.VOLKOV, "acct-made-up-77"],
                          rationale=f"{s.NOVAPAY} wired money straight to {s.VORTEX} via acct-made-up-77."))
    events, final = run(s.NOVAPAY, scratch_key)
    assert final["planner"] == "llm" and final["model"] == "fake-model"
    assert tools_called(events) == ["resolve_entity", "recall_memory", "explore_neighborhood", "explore_neighborhood",
                                    "exposure_flow"]
    bound, swapped, other_subject, other_request = final["model_answer"]["statements"]
    route = fact(final, bound["fact"])
    assert bound["status"] == "bound" and (route["predicate"], route["value"]) == ("eligible_route_hops", 5)
    assert swapped["status"] == "rejected"
    assert f"max_flow_capacity for {s.NOVA_OPS} → {s.VORTEX} is $132,000" in swapped["reason"]
    assert f"$410,000 is {s.NOVAPAY}'s monthly_volume" in swapped["reason"]
    assert other_subject["reason"].startswith(f"no tool result gives eligible_route_hops for {s.LUMEN}")
    assert other_request["reason"] == "no tool result gives action_status for apr-0000aaaa"

    assert final["model_answer"]["agrees"] and final["verdict"] == "SUSPICIOUS"
    assert final["model_answer"]["rationale_check"]["unknown_ids"] == ["acct-made-up-77"]
    assert "acct-made-up-77" not in final["summary"] and cited(final)
    assert final["freeze_targets"] == [s.VOLKOV]
    opening = llm.requests[0]["messages"]
    assert [m["role"] for m in opening] == ["system", "user"]
    assert "resolve_entity(" in opening[1]["content"] and "recall_memory(" in opening[1]["content"]
    assert llm.closed


def test_tool_calls_go_back_with_their_provider_signature(scratch_key, fake_llm):
    signed = calls(EXPLORE[0])
    signed.tool_calls[0].model_extra = {"extra_content": {"google": {"thought_signature": "sig-1"}}}
    llm = fake_llm(signed, answer(verdict="INSUFFICIENT", statements=[]))
    run(s.NOVAPAY, scratch_key)
    [replayed] = [m for m in llm.requests[1]["messages"] if m["role"] == "assistant"]
    assert replayed["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig-1"}}


def test_the_model_verdict_never_overrides_the_facts(scratch_key, fake_llm):
    fake_llm(calls(*EXPLORE), answer(verdict="CLEAR", statements=[], rationale="No direct transfers to sanctioned."))
    _, final = run(s.NOVAPAY, scratch_key)
    assert final["verdict"] == "SUSPICIOUS"
    assert final["model_answer"]["proposed_verdict"] == "CLEAR" and not final["model_answer"]["agrees"]
    assert final["model_answer"]["rationale"] == "No direct transfers to sanctioned."


def test_the_tool_budget_caps_a_model_that_never_stops(scratch_key, fake_llm):
    burst = calls(*EXPLORE, ("detect_ring", {"entity_id": s.VOLKOV}), ("trace_money_flow", {"entity_id": s.VOLKOV}),
                  ("find_brokers", {"entity_id": s.VOLKOV}))
    llm = fake_llm(lambda request: burst if "tools" in request else answer(verdict="SUSPICIOUS", statements=[]))
    events, final = run(s.NOVAPAY, scratch_key)
    assert final["tool_calls"] == agent.MAX_STEPS and final["budget_exhausted"]
    assert len(tools_called(events)) == agent.MAX_STEPS
    last = llm.requests[-1]
    assert "tools" not in last and f"All {agent.MAX_STEPS} tool calls are used" in last["messages"][-1]["content"]
    skipped = [m for m in last["messages"] if m["role"] == "tool" and "budget exhausted" in m["content"]]
    ran_of_last_burst = (agent.MAX_STEPS - 2) % len(burst.tool_calls)  # after resolve_entity and recall_memory
    assert ran_of_last_burst and len(skipped) == len(burst.tool_calls) - ran_of_last_burst


def test_an_llm_failure_hands_over_to_the_fallback_without_repeating_calls(scratch_key, fake_llm):
    fake_llm(calls(EXPLORE[0]), RuntimeError("provider down"))
    events, final = run(s.NOVAPAY, scratch_key)
    assert any("The LLM failed (RuntimeError: provider down)" in e["message"] for e in events if e["type"] == "notice")
    assert final["planner"] == "llm+fallback"
    explores = [e["args"] for e in events if e["type"] == "tool_call" and e["tool"] == "explore_neighborhood"]
    assert explores == [{"entity_id": s.NOVAPAY, "depth": 1}, {"entity_id": s.NOVAPAY, "depth": 6}]
    assert final["verdict"] == "SUSPICIOUS" and final["approval_request"]["approver"]["id"] == s.PRIYA


class RateLimited(Exception):
    """A provider 429 shaped like Gemini's: the exceeded quota and how long to wait."""

    status_code = 429

    def __init__(self, quota: str):
        super().__init__("Error code: 429 - You exceeded your current quota")
        self.body = [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"}]}}]


def test_a_per_minute_rate_limit_is_waited_out_but_a_daily_quota_falls_back(scratch_key, fake_llm, monkeypatch):
    per_minute = "GenerateContentInputTokensPerModelPerMinute-FreeTier"
    assert agent.rate_limit(RateLimited(per_minute)) == {"quotas": [per_minute], "daily": False, "retry_s": 22.0}
    monkeypatch.setattr(agent, "MAX_RATE_LIMIT_WAIT_S", 0.01)
    llm = fake_llm(RateLimited(per_minute), answer(verdict="CLEAR", statements=[]))
    _, final = run(s.ISOLATED, scratch_key)
    assert final["planner"] == "llm" and len(llm.requests) == 2 and final["llm_wait_ms"] >= 10

    daily = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    llm = fake_llm(RateLimited(daily))
    events, final = run(s.ISOLATED, scratch_key)
    assert final["planner"] == "llm+fallback" and len(llm.requests) == 1
    assert any(f"daily quota reached: {daily}" in e["message"] for e in events if e["type"] == "notice")
