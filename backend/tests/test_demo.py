"""Public demo guard rails: past the model cap an investigation runs on the deterministic planner, a reset waits
for running investigations and is spaced out, and hunters stay deterministic."""

from __future__ import annotations

import dataclasses
import json

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
