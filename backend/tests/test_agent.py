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
from backend.app import scenario as s
from backend.app.config import get_settings

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


class FakeLLM:
    """Replays scripted assistant turns in place of openai.AsyncOpenAI; a turn may be a callable or an exception."""

    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        if isinstance(turn, Exception):
            raise turn
        return SimpleNamespace(choices=[SimpleNamespace(message=turn(request) if callable(turn) else turn)])


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
    assert (final["verdict"], final["answer_state"]) == ("SUSPICIOUS", "answered_with_path")
    assert final["verification"]["verified"] and final["verification"]["claims_checked"] >= 8
    assert final["approval_request"]["approver"]["id"] == s.PRIYA
    assert final["approval_request"]["status"] == "PENDING_L3"
    assert set(final["freeze_targets"]) == {s.NOVAPAY, s.NOVA_OPS, s.VOLKOV, s.BALTIC, s.ORION}
    assert set(final["excluded"]) == {s.VIP, s.PROXY_IP}
    assert {"subject": s.ROHAN, "relation": "APPROVED", "object": s.WAIVER} in final["claims"]


def test_an_unknown_id_stops_after_one_call(scratch_key):
    events, final = run(s.UNKNOWN, scratch_key, mode="fallback")
    assert tools_called(events) == ["resolve_entity"]
    assert (final["verdict"], final["answer_state"]) == ("INSUFFICIENT", "unknown_entity")
    assert final["verification"]["verified"]


def test_an_isolated_account_is_clear_with_the_caveat(scratch_key):
    _, final = run(s.ISOLATED, scratch_key, mode="fallback")
    assert (final["verdict"], final["answer_state"]) == ("CLEAR", "not_in_graph")
    assert "No recorded link is not proof the account is safe" in final["summary"]


def test_sharing_only_a_proxy_ip_with_a_mule_is_association_not_exposure(scratch_key):
    events, final = run(s.VIP, scratch_key, mode="fallback")
    assert len(tools_called(events)) == 4 and final["approval_request"] is None
    assert final["verdict"] == "CLEAR"
    assert "ip-185-220-101-7 is a shared IP, not a shared device" in final["summary"]


def test_an_llm_answer_with_an_invented_edge_is_rewritten(scratch_key, fake_llm):
    llm = fake_llm(calls(*EXPLORE), answer(
        verdict="SUSPICIOUS", summary=f"{s.NOVAPAY} wired money straight to {s.VORTEX} via acct-made-up-77.",
        claims=[{"subject": s.NOVAPAY, "relation": "TRANSFERRED", "object": s.VORTEX}]))
    events, final = run(s.NOVAPAY, scratch_key)
    assert final["planner"] == "llm" and final["model"] == "fake-model"
    assert tools_called(events) == ["resolve_entity", "recall_memory", "explore_neighborhood", "explore_neighborhood"]
    assert (final["verdict"], final["answer_state"]) == ("SUSPICIOUS", "answered_with_path")
    check = final["verification"]
    assert check["rewritten"] and check["unknown_ids"] == ["acct-made-up-77"]
    assert [r["claim"]["object"] for r in check["rejected_claims"]] == [s.VORTEX]
    assert "acct-made-up-77" not in final["summary"] and "5 hops from sanctioned" in final["summary"]
    opening = llm.requests[0]["messages"]
    assert [m.get("tool_call_id") for m in opening if m["role"] == "tool"] == ["call_harness_0", "call_harness_1"]


def test_an_llm_answer_citing_tool_edges_is_kept(scratch_key, fake_llm):
    summary = (f"{s.NOVAPAY} pays {s.NOVA_OPS}, which shares device {s.SHARED_DEVICE} with {s.VOLKOV}; "
               f"that chain reaches sanctioned {s.VORTEX} in 5 hops.")
    claims = [[s.NOVAPAY, "TRANSFERRED", s.NOVA_OPS], [s.NOVA_OPS, "USED_DEVICE", s.SHARED_DEVICE],
              [s.VOLKOV, "USED_DEVICE", s.SHARED_DEVICE]]
    fake_llm(calls(*EXPLORE), answer(verdict="SUSPICIOUS", summary=summary, claims=claims))
    _, final = run(s.NOVAPAY, scratch_key)
    assert final["summary"] == summary
    assert final["verification"]["verified"] and final["verification"]["claims_checked"] == 3


def test_the_tool_budget_caps_a_model_that_never_stops(scratch_key, fake_llm):
    burst = calls(*EXPLORE, ("detect_ring", {"entity_id": s.VOLKOV}))
    llm = fake_llm(lambda request: burst if "tools" in request else answer(verdict="SUSPICIOUS", summary=""))
    events, final = run(s.NOVAPAY, scratch_key)
    assert final["tool_calls"] == agent.MAX_STEPS and final["budget_exhausted"]
    assert len(tools_called(events)) == agent.MAX_STEPS
    last = llm.requests[-1]
    assert "tools" not in last and "All 12 tool calls are used" in last["messages"][-1]["content"]
    skipped = [m for m in last["messages"] if m["role"] == "tool" and "budget exhausted" in m["content"]]
    assert len(skipped) == 2


def test_an_llm_failure_hands_over_to_the_fallback_without_repeating_calls(scratch_key, fake_llm):
    fake_llm(calls(EXPLORE[0]), RuntimeError("provider down"))
    events, final = run(s.NOVAPAY, scratch_key)
    assert any("The LLM failed (RuntimeError: provider down)" in e["message"] for e in events if e["type"] == "notice")
    assert final["planner"] == "llm+fallback"
    explores = [e["args"] for e in events if e["type"] == "tool_call" and e["tool"] == "explore_neighborhood"]
    assert explores == [{"entity_id": s.NOVAPAY, "depth": 1}, {"entity_id": s.NOVAPAY, "depth": 5}]
    assert final["verdict"] == "SUSPICIOUS" and final["approval_request"]["approver"]["id"] == s.PRIYA
