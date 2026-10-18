"""Public demo guard rails: past the model cap an investigation runs on the deterministic planner, a reset waits
for running investigations and is spaced out, and hunters stay deterministic."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import coordination as co
from backend.app import demo, main
from backend.app.config import get_settings
from backend.app.db import get_db

ALERT = {"subject_id": "acct-ind-20118", "rule": "large_outbound_wire", "priority": 3}


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


def alerts_on(key: str, subject: str) -> list[str]:
    return [a["id"] for a in co.queue_state(get_db().select_graph(key)) if a["subject_id"] == subject]


def api() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://fintrace")


@pytest.mark.graph
def test_a_reset_behind_a_mutation_holds_off_new_work_until_it_ends(small_tenant, monkeypatch):
    guard, key, params = demo.Guard(), f"fintrace:{small_tenant}", {"tenant": small_tenant}
    monkeypatch.setattr(main, "guard", guard)
    monkeypatch.setattr(main, "_locks", {})
    before = alerts_on(key, ALERT["subject_id"])

    async def scenario():
        async with api() as client:
            write = main._lock(key, "write")
            await write.acquire()
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
                       await client.post("/api/alerts", params=params, json=ALERT),
                       await client.post("/api/reset", params=params)]
            assert not reset.done(), "the reset did not wait for the write under way"
            assert alerts_on(key, ALERT["subject_id"]) == before, "an alert was raised while the reset was waiting"
            write.release()
            return refused, await reset

    refused, done = asyncio.run(scenario())
    assert [r.status_code for r in refused] == [409] * 7
    assert all("being reset" in r.json()["detail"] for r in refused)
    assert done.status_code == 200 and not guard.resetting
    assert investigate_unknown(TestClient(main.app), small_tenant)[-1]["answer_state"] == "unknown_entity"


@pytest.mark.graph
def test_an_alert_raised_while_the_graph_is_restored_is_refused_and_lands_once_it_is_back(small_tenant, monkeypatch):
    guard, key, params = demo.Guard(), f"fintrace:{small_tenant}", {"tenant": small_tenant}
    monkeypatch.setattr(main, "guard", guard)
    monkeypatch.setattr(main, "_locks", {})
    restore, restoring, go = main.tenants.reset, threading.Event(), threading.Event()

    def slow_restore(tenant):
        restoring.set()
        go.wait(10)
        return restore(tenant)

    monkeypatch.setattr(main.tenants, "reset", slow_restore)

    async def scenario():
        async with api() as client:
            reset = asyncio.create_task(client.post("/api/reset", params=params))
            await asyncio.to_thread(restoring.wait, 10)
            during = await client.post("/api/alerts", params=params, json=ALERT)
            go.set()
            done = await reset
            return during, done, await client.post("/api/alerts", params=params, json=ALERT)

    during, done, after = asyncio.run(scenario())
    assert during.status_code == 409 and "being reset" in during.json()["detail"]
    assert done.status_code == 200 and after.status_code == 200
    assert after.json()["id"] in alerts_on(key, ALERT["subject_id"])


@pytest.mark.graph
@pytest.mark.parametrize(("path", "body", "module", "name", "status"), [
    ("/api/alerts", ALERT, co, "raise_alert", 200),
    ("/api/approvals/apr-00000000/approve", {"approver_id": "emp-priya-nair"}, main.q, "approve_action", 409),
    ("/api/queue/reset", None, co, "reset_queue", 200),
], ids=["alert", "approval", "queue-reset"])
def test_a_reset_waits_for_a_write_admitted_before_it(small_tenant, monkeypatch, path, body, module, name, status):
    guard, key, params = demo.Guard(), f"fintrace:{small_tenant}", {"tenant": small_tenant}
    monkeypatch.setattr(main, "guard", guard)
    monkeypatch.setattr(main, "_locks", {})
    write, restore = getattr(module, name), main.tenants.reset
    order, admitted, go = [], threading.Event(), threading.Event()

    def held(*args, **kwargs):
        admitted.set()
        go.wait(10)
        try:
            return write(*args, **kwargs)
        finally:
            order.append("write")

    def restore_after(tenant):
        order.append("restore")
        return restore(tenant)

    monkeypatch.setattr(module, name, held)
    monkeypatch.setattr(main.tenants, "reset", restore_after)

    async def scenario():
        async with api() as client:
            pending = asyncio.create_task(client.post(path, params=params, json=body))
            await asyncio.to_thread(admitted.wait, 10)
            reset = asyncio.create_task(client.post("/api/reset", params=params))
            while key not in guard.resetting:
                await asyncio.sleep(0.01)
            refused = await client.post("/api/alerts", params=params, json=ALERT)
            await asyncio.sleep(0.2)
            waited = not reset.done()
            go.set()
            return await pending, await reset, refused, waited

    written, reset, refused, waited = asyncio.run(scenario())
    assert written.status_code == status and reset.status_code == 200
    assert refused.status_code == 409 and waited, "the reset did not wait for the write admitted before it"
    assert order == ["write", "restore"]


@pytest.mark.graph
def test_a_failed_reset_lifts_its_hold(small_tenant, monkeypatch):
    guard = demo.Guard()
    monkeypatch.setattr(main, "guard", guard)
    monkeypatch.setattr(main, "_locks", {})

    def broken(_):
        raise RuntimeError("the copy failed")

    monkeypatch.setattr(main.tenants, "reset", broken)
    client = TestClient(main.app, raise_server_exceptions=False)
    assert client.post("/api/reset", params={"tenant": small_tenant}).status_code == 500
    assert not guard.resetting
    assert investigate_unknown(client, small_tenant)[-1]["answer_state"] == "unknown_entity"
    raised = client.post("/api/alerts", params={"tenant": small_tenant}, json=ALERT)
    assert raised.status_code == 200 and raised.json()["id"] in alerts_on(f"fintrace:{small_tenant}", ALERT["subject_id"])


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
