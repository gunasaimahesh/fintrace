"""Thursday checkpoint: the seeded graph is correct and every core query answers within a second.

Needs a local FalkorDB loaded with `python -m backend.app.seed_data --reset`. Measured latencies
and memory are written to backend/tests/checkpoint_report.json.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.app import queries as q
from backend.app import scenario as s
from backend.app.config import DATA_DIR, get_settings
from backend.app.db import get_db, get_graph, graph_key, memory_usage_mb

pytestmark = pytest.mark.checkpoint

BUDGET_MS = 1000
STABILITY_RUNS = 20
BIGGEST_HUB = "acct-100428660"
REPORT_PATH = Path(__file__).with_name("checkpoint_report.json")
REPORT: dict = {"budget_ms": BUDGET_MS, "latency_ms": {}}


@pytest.fixture(scope="module")
def graph():
    key = graph_key(get_settings().default_tenant)
    if key not in get_db().list_graphs():
        pytest.skip(f"{key} is not loaded; run python -m backend.app.seed_data --reset")
    yield get_graph()
    REPORT_PATH.write_text(json.dumps(REPORT, indent=2) + "\n")


def timed(name: str, fn, *args, **kwargs):
    started = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed = (time.perf_counter() - started) * 1000
    REPORT["latency_ms"][name] = round(elapsed, 1)
    assert elapsed <= BUDGET_MS, f"{name} took {elapsed:.0f} ms (budget {BUDGET_MS} ms)"
    return result


def test_entity_lookup_uses_index(graph):
    plan = str(graph.explain("MATCH (e:Entity {id: $id}) RETURN e", {"id": s.NOVAPAY}))
    assert "Node By Index Scan | (e:Entity)" in plan, plan


def test_graph_matches_slice_manifest(graph):
    counts = json.loads((DATA_DIR / "slice_manifest.json").read_text())["counts"]
    accounts = graph.ro_query("MATCH (a:Account) WHERE a.source = 'ibm-aml' RETURN count(a)").result_set[0][0]
    transfers = graph.ro_query(
        "MATCH ()-[t:TRANSFERRED]->() WHERE t.tx_id STARTS WITH 'ibm-' RETURN count(t)").result_set[0][0]
    assert accounts == counts["accounts_total"]
    assert transfers == counts["transfers"]


def test_no_ground_truth_in_graph(graph):
    keys = {row[0] for row in graph.ro_query("CALL db.propertyKeys()").result_set}
    leaked = keys & {"pattern", "pattern_hint", "pattern_type", "is_laundering", "laundering", "instance_id"}
    assert not leaked, f"label-derived properties in the graph: {leaked}"


def test_novapay_is_exactly_five_hops_from_vortex(graph):
    result = timed("path_between(novapay, vortex)", q.path_between, graph, s.NOVAPAY, s.VORTEX)
    assert result["hops"] == 5
    assert result["path"]["nodes"] == [s.NOVAPAY, s.NOVA_OPS, s.SHARED_DEVICE, s.VOLKOV, s.BALTIC, s.VORTEX]


def test_depth_one_is_clean_and_depth_five_reaches_one_sanctioned(graph):
    shallow = timed("explore(novapay, depth=1)", q.explore_neighborhood, graph, s.NOVAPAY, 1)
    assert shallow["flagged"] == []
    assert shallow["levels"][0]["accounts"] >= 20

    deep = timed("explore(novapay, depth=5)", q.explore_neighborhood, graph, s.NOVAPAY, 5)
    sanctioned = [f for f in deep["flagged"] if "Sanctioned" in f["entity"]["labels"]]
    assert [(f["entity"]["id"], f["depth"]) for f in sanctioned] == [(s.VORTEX, 5)]
    REPORT["novapay_depth5"] = {"reached": deep["total_reached"], "sanctioned": len(sanctioned),
                                "levels": deep["levels"]}


def test_worst_case_neighborhood_within_budget(graph):
    result = timed("explore(biggest hub, depth=5)", q.explore_neighborhood, graph, BIGGEST_HUB, 5)
    REPORT["hub_depth5_reached"] = result["total_reached"]


def test_money_flow_is_time_ordered(graph):
    result = timed("trace_money_flow(volkov, out, 6)", q.trace_money_flow, graph, s.VOLKOV, "out", 6)
    [hit] = result["sanctioned"]
    assert hit["entity"]["id"] == s.VORTEX
    times = [step["ts"] for step in hit["trail"]]
    assert times == sorted(times)
    assert [step["from"] for step in hit["trail"]][0] == s.VOLKOV


def test_money_flow_stops_at_identity_hops(graph):
    result = timed("trace_money_flow(novapay, out, 6)", q.trace_money_flow, graph, s.NOVAPAY, "out", 6)
    assert result["sanctioned"] == []


def test_exposure_counts_every_account_behind_the_device(graph):
    multi = timed("exposure_flow(nova-ops + volkov)", q.exposure_flow, graph, [s.NOVA_OPS, s.VOLKOV], s.VORTEX)
    assert multi["max_flow_dollars"] == 132_000
    assert q.exposure_flow(graph, [s.NOVAPAY], s.VORTEX)["max_flow_dollars"] == 0


def test_ring_detection_is_stable(graph):
    timed("detect_ring(volkov)", q.detect_ring, graph, s.VOLKOV)
    outcomes = [frozenset(q.detect_ring(graph, s.VOLKOV)["members"]) for _ in range(STABILITY_RUNS)]
    stable = sum(1 for members in outcomes if members == frozenset(s.RING_CORE))
    REPORT["label_propagation_stability"] = f"{stable}/{STABILITY_RUNS}"
    assert stable == STABILITY_RUNS, f"ring membership matched in {stable}/{STABILITY_RUNS} runs"


def test_brokers_use_and_release_scope_labels(graph):
    result = timed("find_brokers(ring)", q.find_brokers, graph, [*s.RING_CORE, s.NOVA_OPS, s.SHARED_DEVICE])
    assert result["scores"][0]["id"] == s.BALTIC
    for label in q.BROKER_SCOPE_LABELS:
        assert graph.ro_query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0] == 0


def test_graph_copy_within_budget(graph):
    db = get_db()
    source = graph_key(get_settings().default_tenant)
    target = f"{source}:checkpoint-copy"
    if target in db.list_graphs():
        db.select_graph(target).delete()
    try:
        timed("GRAPH.COPY", db.connection.execute_command, "GRAPH.COPY", source, target)
    finally:
        db.select_graph(target).delete()


def test_unknown_and_isolated_entities(graph):
    assert q.resolve_entity(graph, s.UNKNOWN) is None
    isolated = q.explore_neighborhood(graph, s.ISOLATED, 3)
    assert isolated["total_reached"] == 0


def test_record_memory_usage(graph):
    usage = memory_usage_mb(graph_key(get_settings().default_tenant))
    REPORT["memory_mb"] = usage
    assert usage["total_graph_sz_mb"] > 0
