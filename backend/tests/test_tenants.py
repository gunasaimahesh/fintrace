"""Tenants through the API: memory, episodes, queue, approvals, revision and reset stay in the tenant whose graph
they were written to (demo data isolation, not access control)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app import scenario as s

pytestmark = pytest.mark.graph


@pytest.fixture
def client():
    return TestClient(main.app)


def stream(client: TestClient, path: str, **params) -> list[dict]:
    with client.stream("GET", path, params=params) as response:
        assert response.status_code == 200, response.read()
        return [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]


def state(client: TestClient, tenant: str) -> dict:
    get = lambda path: client.get(path, params={"tenant": tenant}).json()  # noqa: E731
    return {
        "facts": len(get(f"/api/memory/{s.NOVAPAY}")["facts"]),
        "approvals": [a["id"] for a in get("/api/approvals")],
        "queue": {a["id"]: a["status"] for a in get("/api/queue")},
        "rev": get("/api/health")["dataset"]["rev"],
    }


def test_memory_queue_approvals_revision_and_reset_stay_inside_their_tenant(fresh_key, small_tenant, client):
    acme, globex = fresh_key.removeprefix("fintrace:"), small_tenant
    before = state(client, acme)
    assert before["facts"] == 0 and before["approvals"] == [] and len(before["queue"]) == 16
    assert state(client, globex) == {"facts": 0, "approvals": [], "rev": 0,
                                     "queue": {"alert-2001": "PENDING", "alert-2002": "PENDING"}}

    first = stream(client, "/api/investigate", entity_id=s.NOVAPAY, tenant=acme, mode="fallback")[-1]
    assert first["verdict"] == "SUSPICIOUS" and first["approval_request"]["status"] == "PENDING_L3"
    assert state(client, globex)["facts"] == 0 and state(client, globex)["approvals"] == []

    events = stream(client, "/api/investigate", entity_id=s.NOVAPAY, tenant=globex, mode="fallback")
    second = events[-1]
    assert second["answer_state"] == "answered_with_path", "another tenant's memory answered"
    assert "explore_neighborhood" in [e["tool"] for e in events if e["type"] == "tool_call"]
    assert second["approval_request"]["status"] == "PENDING_L3", "another tenant's pending request blocked this one"

    events = stream(client, "/api/queue/drain", tenant=globex, workers=3)
    assert events[-1]["type"] == "drained"
    assert {e["alert_id"] for e in events if e["type"] == "alert_done"} == {"alert-2001", "alert-2002"}
    assert state(client, acme)["queue"] == before["queue"]

    injected = client.post("/api/mutations", params={"tenant": acme},
                           json={"kind": "add_transfer", "src": s.VOLKOV, "dst": s.BALTIC, "amount_usd": 9500})
    assert injected.status_code == 200 and injected.json()["rev"] == before["rev"] + 1
    assert state(client, globex)["rev"] == 0

    acme_now, globex_now = state(client, acme), state(client, globex)
    assert acme_now["facts"] and len(acme_now["approvals"]) == 1
    assert globex_now["facts"] and len(globex_now["approvals"]) == 1
    assert set(globex_now["queue"].values()) == {"COMPLETED"}

    assert client.post("/api/reset", params={"tenant": globex}).json()["rev"] == 0
    assert state(client, globex) == {"facts": 0, "approvals": [], "rev": 0,
                                     "queue": {"alert-2001": "PENDING", "alert-2002": "PENDING"}}
    assert state(client, acme) == acme_now

    assert client.post("/api/reset", params={"tenant": acme}).status_code == 409, "a copy without a snapshot"


def test_tenant_names_are_checked_before_any_graph_is_touched(client):
    assert client.get("/api/queue", params={"tenant": "Not A Tenant!"}).status_code == 422
    assert client.get("/api/queue", params={"tenant": "no-such-tenant"}).status_code == 404
    assert "acme-pay" in client.get("/api/tenants").json()["tenants"]
    assert not any(n.startswith("test-") for n in client.get("/api/tenants").json()["tenants"])
