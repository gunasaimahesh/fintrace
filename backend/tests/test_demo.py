"""Public demo guard rails: past the model cap an investigation runs on the deterministic planner, a reset waits
for running investigations and is spaced out, and hunters stay deterministic."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import demo, main
from backend.app.config import get_settings


@pytest.fixture
def public(monkeypatch) -> demo.Guard:
    settings = dataclasses.replace(get_settings(), public_demo=True, llm_api_key="demo-test-key",
                                   demo_llm_runs_per_hour=2, demo_llm_runs_per_visitor_per_hour=1,
                                   demo_max_investigations=1, demo_reset_cooldown_s=60.0)
    for module in (demo, main):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    guard = demo.Guard()
    monkeypatch.setattr(main, "guard", guard)
    return guard


def test_past_the_model_cap_an_investigation_runs_on_the_deterministic_planner(public):
    assert public.planner("visitor-a", "auto") == ("auto", None)
    mode, notice = public.planner("visitor-a", "auto")
    assert mode == "fallback" and "1 run per visitor" in notice
    assert public.planner("visitor-b", "llm") == ("llm", None)
    mode, notice = public.planner("visitor-c", "auto")
    assert mode == "fallback" and "2 runs across all visitors" in notice
    assert public.planner("visitor-d", "fallback") == ("fallback", None)
    assert public.status()["llm_runs_left"] == 0


def test_without_public_demo_nothing_is_capped():
    guard = demo.Guard()
    assert not get_settings().public_demo and guard.status() is None
    assert all(guard.planner("visitor-a", "auto") == ("auto", None) for _ in range(50))
    assert not guard.busy()


def test_an_investigation_and_a_reset_cannot_both_be_admitted():
    guard = demo.Guard()
    with guard.investigation("g"):
        assert guard.refuse_reset("g")[0] == 409
    with guard.reset("g"):
        with pytest.raises(demo.Resetting):
            with guard.investigation("g"):
                pass
        assert guard.refuse_reset("g") == (409, "this tenant's graph is already being reset")
    assert guard.refuse_admission("g") is None and guard.refuse_reset("g") is None and not guard.running


def investigate_unknown(client: TestClient, tenant: str) -> list[dict]:
    with client.stream("GET", "/api/investigate", params={"tenant": tenant, "entity_id": "acct-ghost-999"}) as response:
        return [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]


@pytest.mark.graph
def test_a_reset_behind_a_mutation_holds_off_new_work_until_it_ends(small_tenant, monkeypatch):
    guard, key, params = demo.Guard(), f"fintrace:{small_tenant}", {"tenant": small_tenant}
    monkeypatch.setattr(main, "guard", guard)
    monkeypatch.setattr(main, "_locks", {})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://fintrace") as client:
            mutation = main._lock(key, "mutation")
            await mutation.acquire()
            reset = asyncio.create_task(client.post("/api/reset", params=params))
            while key not in guard.resetting:
                await asyncio.sleep(0.01)
            refused = [await client.get("/api/investigate", params={**params, "entity_id": "acct-ghost-999"}),
                       await client.get("/api/queue/drain", params=params),
                       await client.post("/api/queue/reset", params=params),
                       await client.post("/api/mutations", params=params,
                                         json={"kind": "set_sanctioned", "account": "acct-ind-20118", "sanctioned": True}),
                       await client.post("/api/approvals/apr-00000000/approve", params=params,
                                         json={"approver_id": "emp-priya-nair"}),
                       await client.post("/api/reset", params=params)]
            assert not reset.done(), "the reset did not wait for the mutation"
            mutation.release()
            return refused, await reset

    refused, done = asyncio.run(scenario())
    assert [r.status_code for r in refused] == [409] * 6
    assert all("being reset" in r.json()["detail"] for r in refused)
    assert done.status_code == 200 and not guard.resetting
    assert investigate_unknown(TestClient(main.app), small_tenant)[-1]["answer_state"] == "unknown_entity"


@pytest.mark.graph
def test_a_failed_reset_lifts_its_hold(small_tenant, monkeypatch):
    guard = demo.Guard()
    monkeypatch.setattr(main, "guard", guard)

    def broken(_):
        raise RuntimeError("the copy failed")

    monkeypatch.setattr(main.tenants, "reset", broken)
    client = TestClient(main.app, raise_server_exceptions=False)
    assert client.post("/api/reset", params={"tenant": small_tenant}).status_code == 500
    assert not guard.resetting
    assert investigate_unknown(client, small_tenant)[-1]["answer_state"] == "unknown_entity"


@pytest.mark.graph
def test_a_reset_waits_for_running_investigations_and_is_spaced_out(public, small_tenant):
    client = TestClient(main.app)
    key = f"fintrace:{small_tenant}"
    with public.investigation(key):
        refused = client.post("/api/reset", params={"tenant": small_tenant})
        assert refused.status_code == 409 and "investigation" in refused.json()["detail"]
        busy = client.get("/api/investigate", params={"tenant": small_tenant, "entity_id": "acct-ghost-999"})
        assert busy.status_code == 429
    assert client.post("/api/reset", params={"tenant": small_tenant}).status_code == 200
    again = client.post("/api/reset", params={"tenant": small_tenant})
    assert again.status_code == 429 and "try again" in again.json()["detail"]

    health = client.get("/api/health", params={"tenant": small_tenant}).json()
    assert health["public_demo"]["hunter_planner"] == "fallback"
    with client.stream("GET", "/api/queue/drain", params={"tenant": small_tenant, "workers": 6,
                                                          "mode": "auto"}) as response:
        events = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]
    drained = events[-1]
    assert drained["type"] == "drained" and drained["workers"] == main.PUBLIC_HUNTERS
    assert {e["planner"] for e in events if e["type"] == "tool_call"} == {"fallback"}
