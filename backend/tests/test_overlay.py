"""The challenge overlay the seeder applies, on private copies: NovaPay's neighborhood is untouched, and a reference
eligible-route search written here (not the overlay's own) agrees with every case's expected outcome."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

import pytest
from falkordb import Graph

from backend.app import overlay as ov
from backend.app import queries as q
from backend.app import scenario as s
from backend.app.contracts import PRESCRIBED_DEPTH, AlertStatus
from backend.app.db import get_db

pytestmark = pytest.mark.graph

FORBIDDEN = ("mule", "shell", "sdn", "launder", "ring", "fraud", "risk", "sanction", "suspicious", "clean",
             "positive", "negative", "control", "expected", "heldout", "test", "case", "layer", "exit")
NEUTRAL_RULES = {"large_outbound_wire", "new_counterparty_burst", "velocity_spike", "dormant_reactivation",
                 "cross_border_wire", "merchant_volume_jump"}
DEEP = 10
MAX_CAPACITY = 2**31 - 1

# The verdict contract's edges: money forwards along FLOWS_TO, identity only Account -> Device <- Account.
FORWARD = """
UNWIND $frontier AS nid
MATCH (a) WHERE ID(a) = nid
MATCH (a)-[r:FLOWS_TO|USED_DEVICE]->(b)
RETURN collect([nid, ID(b), type(r) = 'USED_DEVICE', b:Sanctioned])
"""
DEVICE_USERS = """
UNWIND $frontier AS nid
MATCH (d) WHERE ID(d) = nid
MATCH (d)<-[:USED_DEVICE]-(b)
RETURN collect([nid, ID(b), false, b:Sanctioned])
"""
UNDIRECTED = """
UNWIND $frontier AS nid
MATCH (a) WHERE ID(a) = nid
MATCH (a)-[:FLOWS_TO|USED_DEVICE|LOGGED_FROM]-(b)
RETURN collect(DISTINCT ID(b))
"""
NEW_NODES = """
UNWIND $ids AS eid
MATCH (n:Entity {id: eid})
RETURN collect([n.id, labels(n), n.name, n.rule, n.address, n.topology_updated_at])
"""


@dataclass
class Overlaid:
    graph: Graph
    cases: list[dict]
    meta: dict
    base_ids: set[str]
    new_ids: set[str]


def all_ids(graph: Graph) -> set[str]:
    return set(graph.ro_query("MATCH (n:Entity) RETURN collect(n.id)").result_set[0][0])


def novapay_levels(graph: Graph) -> list[dict]:
    # Generous budgets: a search cut short by a busy shared server would compare fewer levels.
    result = q.explore_neighborhood(graph, s.NOVAPAY, PRESCRIBED_DEPTH, node_budget=10**6, time_budget_s=120)
    assert result["scope"]["complete"] and result["scope"]["completed_depth"] == PRESCRIBED_DEPTH
    return result["levels"]


@pytest.fixture
def overlaid(fresh_key: str) -> Overlaid:
    """A copy of the seeded graph, which carries the overlay, and the cases from backend/eval/cases.json."""
    graph = get_db().select_graph(fresh_key)
    cases = json.loads(ov.CASES_PATH.read_text())["cases"]
    new = {i for case in cases for i in case["planted_ids"]} | {case["alert_id"] for case in cases}
    return Overlaid(graph, cases, q.dataset_meta(graph), all_ids(graph) - new, new)


def eligible_routes(graph: Graph, subject_id: str, max_hops: int) -> dict[str, list[tuple[str, bool]]]:
    """Unit-cost BFS from the subject; the route found to each sanctioned account, as (id, is_device) steps."""
    start = q.internal_id(graph, subject_id)
    parent: dict[int, int | None] = {start: None}
    device = {start: False}
    accounts, devices, hits = [start], [], []
    for _ in range(max_hops):
        steps = []
        if accounts:
            steps += graph.ro_query(FORWARD, {"frontier": accounts}).result_set[0][0]
        if devices:
            steps += graph.ro_query(DEVICE_USERS, {"frontier": devices}).result_set[0][0]
        accounts, devices = [], []
        for src, dst, to_device, sanctioned in steps:
            if dst in parent:
                continue
            parent[dst], device[dst] = src, to_device
            (devices if to_device else accounts).append(dst)
            if sanctioned:
                hits.append(dst)
        if not accounts and not devices:
            break
    chains = {}
    for hit in hits:
        chain = [hit]
        while parent[chain[-1]] is not None:
            chain.append(parent[chain[-1]])
        chains[hit] = chain[::-1]
    ids = dict(graph.ro_query("UNWIND $ids AS nid MATCH (n) WHERE ID(n) = nid RETURN collect([nid, n.id])",
                              {"ids": sorted({n for c in chains.values() for n in c})}).result_set[0][0])
    return {ids[hit]: [(ids[n], device[n]) for n in chain] for hit, chain in chains.items()}


def nearest(routes: dict[str, list]) -> tuple[int | None, set[str]]:
    if not routes:
        return None, set()
    hops = min(len(route) - 1 for route in routes.values())
    return hops, {target for target, route in routes.items() if len(route) - 1 == hops}


def assert_time_ordered(graph: Graph, route: list[tuple[str, bool]]) -> None:
    """Every money leg has a transfer at or after the previous leg's; device hops move no money."""
    clock = 0
    for (src, src_is_device), (dst, dst_is_device) in zip(route, route[1:]):
        if src_is_device or dst_is_device:
            continue
        times = graph.ro_query("MATCH (:Entity {id: $a})-[t:TRANSFERRED]->(:Entity {id: $b}) RETURN collect(t.ts)",
                               {"a": src, "b": dst}).result_set[0][0]
        later = [ts for ts in times if ts >= clock]
        assert later, f"no transfer {src} -> {dst} at or after {clock}"
        clock = min(later)


def within_undirected(graph: Graph, entity_id: str, depth: int) -> set[str]:
    start = q.internal_id(graph, entity_id)
    seen, frontier = {start}, [start]
    for _ in range(depth):
        frontier = [n for n in graph.ro_query(UNDIRECTED, {"frontier": frontier}).result_set[0][0] if n not in seen]
        seen.update(frontier)
    ids = graph.ro_query("UNWIND $ids AS nid MATCH (n) WHERE ID(n) = nid RETURN collect(n.id)",
                         {"ids": list(seen)}).result_set[0][0]
    return set(ids)


def test_novapay_is_untouched_and_the_overlay_is_versioned(overlaid):
    graph, meta = overlaid.graph, overlaid.meta
    doc = json.loads(ov.CASES_PATH.read_text())
    assert meta["fixture_version"] == f"{doc['base_fixture_version']}+{ov.OVERLAY_VERSION}"
    assert meta["rev"] == 0
    with pytest.raises(RuntimeError, match="already applied"):
        ov.apply_overlay(graph)

    anchors = [a for case in overlaid.cases for a in case["anchor_ids"]]
    assert len(anchors) == len(set(anchors)) and set(anchors) <= overlaid.base_ids
    assert not set(anchors) & within_undirected(graph, s.NOVAPAY, ov.MIN_ANCHOR_DISTANCE - 1)
    sources = graph.ro_query("UNWIND $ids AS eid MATCH (a:Account {id: eid}) RETURN collect(DISTINCT a.source)",
                             {"ids": anchors}).result_set[0][0]
    assert sources == ["ibm-aml"]
    touched = graph.ro_query("""
        UNWIND $ids AS eid
        MATCH (n:Entity {id: eid})-[r]-(m)
        RETURN collect(DISTINCT m.id)
    """, {"ids": sorted(overlaid.new_ids)}).result_set[0][0]
    assert set(touched) - overlaid.new_ids == set(anchors)

    assert (doc["overlay_version"], doc["prescribed_depth"]) == (ov.OVERLAY_VERSION, PRESCRIBED_DEPTH)
    _, rebuilt = ov.build_overlay(ov.find_anchors(graph, ov.ANCHOR_COUNT, ov.SEED),
                                  (meta["window_start"], meta["as_of_ts"]), meta["loaded_at"])
    assert rebuilt == overlaid.cases, "backend/eval/cases.json is stale; reseed to rewrite it"

    with_overlay = novapay_levels(graph)
    graph.query("UNWIND $ids AS eid MATCH (n:Entity {id: eid}) DETACH DELETE n", {"ids": sorted(overlaid.new_ids)})
    assert novapay_levels(graph) == with_overlay


def test_a_reference_eligible_search_confirms_every_case(overlaid):
    graph = overlaid.graph
    for case in overlaid.cases:
        expected, cid = case["expected"], case["case_id"]
        target = expected["sanctioned_target"]
        within = eligible_routes(graph, case["subject_id"], PRESCRIBED_DEPTH)
        if expected["verdict"] == "SUSPICIOUS":
            assert nearest(within) == (expected["eligible_hops"], {target}), cid
            assert_time_ordered(graph, within[target])
        else:
            assert within == {}, cid
        if case["category"] == "out_of_scope_positive":
            deep = eligible_routes(graph, case["subject_id"], DEEP)
            assert nearest(deep) == (expected["eligible_hops"], {target}), cid
            assert expected["eligible_hops"] > PRESCRIBED_DEPTH, cid
            assert_time_ordered(graph, deep[target])


def test_undirected_distances_and_the_ip_shortcut(overlaid):
    graph = overlaid.graph
    for case in overlaid.cases:
        expected, cid = case["expected"], case["case_id"]
        own = expected["sanctioned_target"] or expected["associated_sanctioned"]
        hops = expected["shortest_undirected_hops_to_sanctioned"]
        assert (own is None) == (hops is None), cid
        if own:
            assert q.path_between(graph, case["subject_id"], own)["hops"] == hops, cid
        seen = q.explore_neighborhood(graph, case["subject_id"], PRESCRIBED_DEPTH)
        sanctioned = {(f["entity"]["id"], f["depth"]) for f in seen["flagged"] if "Sanctioned" in f["entity"]["labels"]}
        assert sanctioned == ({(own, hops)} if own and hops <= PRESCRIBED_DEPTH else set()), cid
        if case["pattern"] == "isolated_new_account":
            assert seen["total_reached"] == 0

    [shortcut] = [c for c in overlaid.cases if c["pattern"] == "ip_shortcut_with_device_route"]
    path = q.path_between(graph, shortcut["subject_id"], shortcut["expected"]["sanctioned_target"])
    assert path["hops"] < shortcut["expected"]["eligible_hops"]
    assert any("IP" in entity["labels"] for entity in path["path"]["entities"])


def test_alerts_flow_summaries_and_neutral_data(overlaid):
    graph, cases, new = overlaid.graph, overlaid.cases, sorted(overlaid.new_ids)
    meta = overlaid.meta
    planted = [i for case in cases for i in case["planted_ids"]]
    assert len(planted) == len(set(planted)), "cases share planted entities"
    assert set(planted) | {case["alert_id"] for case in cases} == overlaid.new_ids

    alerts = graph.ro_query("""
        UNWIND $ids AS eid
        MATCH (al:Alert {id: eid})
        OPTIONAL MATCH (al)-[:ALERT_ON]->(x)
        RETURN collect([al.id, al.status, al.subject_id, al.rule, al.priority, al.created_at, al.fixture_version, x.id])
    """, {"ids": new}).result_set[0][0]
    by_id = {row[0]: row for row in alerts}
    assert len(alerts) == len(by_id) == len(cases)
    for case in cases:
        _, status, subject, rule, priority, created_at, version, on = by_id[case["alert_id"]]
        assert (status, subject, on, version) == (AlertStatus.PENDING, case["subject_id"], case["subject_id"],
                                                  ov.OVERLAY_VERSION)
        assert rule in NEUTRAL_RULES and 1 <= priority <= 5
        assert meta["window_start"] <= created_at <= meta["as_of_ts"]

    nodes = graph.ro_query(NEW_NODES, {"ids": new}).result_set[0][0]
    texts = [t for node_id, _, name, rule, *_ in nodes for t in (node_id, name, rule) if t]
    transfers = graph.ro_query("""
        UNWIND $ids AS eid
        MATCH (:Entity {id: eid})-[t:TRANSFERRED]-()
        WITH DISTINCT t
        RETURN collect([startNode(t).id, endNode(t).id, t.amount, t.ts, t.tx_id])
    """, {"ids": new}).result_set[0][0]
    texts += [tx_id for *_, tx_id in transfers]
    assert [t for t in texts if any(word in t.lower() for word in FORBIDDEN)] == []

    for node_id, labels, _, _, address, updated in nodes:
        if "Device" in labels:
            assert re.fullmatch(r"dev-[0-9a-f]{10}", node_id)
        elif "IP" in labels:
            assert node_id == "ip-" + address.replace(".", "-")
        elif "Account" in labels:
            assert re.fullmatch(r"(acct|merch)-[a-z0-9-]+", node_id)
        else:
            assert "Alert" in labels and re.fullmatch(r"alert-\d+", node_id)
        if "RiskNode" in labels:
            assert updated == meta["loaded_at"], node_id
    ips, addresses = graph.ro_query("MATCH (ip:IP) RETURN count(ip), count(DISTINCT ip.address)").result_set[0]
    assert ips == addresses

    totals: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for src, dst, amount, ts, tx_id in transfers:
        assert re.fullmatch(r"ov1-\d{5}", tx_id) and amount > 0
        assert meta["window_start"] <= ts <= meta["as_of_ts"]
        totals[(src, dst)].append((amount, ts))
    summaries = graph.ro_query("""
        UNWIND $ids AS eid
        MATCH (:Entity {id: eid})-[f:FLOWS_TO]-()
        WITH DISTINCT f
        RETURN collect([startNode(f).id, endNode(f).id, f.amount, f.transfers, f.first_ts, f.last_ts, f.capacity,
                        f.weight])
    """, {"ids": new}).result_set[0][0]
    pairs = [(src, dst) for src, dst, *_ in summaries]
    assert len(pairs) == len(set(pairs)) and set(pairs) == set(totals)
    for src, dst, amount, count, first_ts, last_ts, capacity, weight in summaries:
        legs = totals[(src, dst)]
        assert amount == sum(a for a, _ in legs) and count == len(legs)
        assert (first_ts, last_ts) == (min(t for _, t in legs), max(t for _, t in legs))
        assert capacity == min(max((amount + 50) // 100, 1), MAX_CAPACITY) and weight == 1


def test_build_overlay_is_deterministic_and_balanced():
    anchors = [f"acct-{n:09d}" for n in range(ov.ANCHOR_COUNT)]
    window = (1_790_208_000, 1_791_735_480)
    spec, cases = ov.build_overlay(anchors, window, 1_792_235_048)
    assert (spec, cases) == ov.build_overlay(anchors, window, 1_792_235_048)
    assert ov.build_overlay(anchors, window, 1_792_235_048, seed=7)[0] != spec

    assert len(cases) == len({c["alert_id"] for c in cases}) == 16
    assert {c["split"] for c in cases} == {"dev", "heldout"}
    heldout = [c for c in cases if c["split"] == "heldout"]
    assert Counter(c["category"] for c in heldout) == {"positive": 4, "negative_control": 2, "out_of_scope_positive": 1}
    assert sorted(c["pattern"] for c in heldout if c["category"] == "positive") == [
        "device_bridge_layering", "device_bridge_layering", "double_device_bridge", "ip_shortcut_with_device_route"]
    assert [c["split"] for c in cases if c["pattern"] == "device_bridge_layering"] == ["heldout", "heldout"]
    [five] = [c for c in cases if c["pattern"] == "device_bridge_layering" and c["expected"]["eligible_hops"] == 5]
    assert five["split"] == "heldout"
    [second] = [c for c in cases if c["category"] == "coordination"]
    assert second["related_case_id"] == five["case_id"]
    assert second["expected"]["sanctioned_target"] == five["expected"]["sanctioned_target"]
    assert second["subject_id"] == five["expected"]["route"][3]

    verdicts = {c["case_id"]: c["expected"]["verdict"] for c in cases}
    for rule in {c["rule"] for c in cases}:
        assert {verdicts[c["case_id"]] for c in cases if c["rule"] == rule} == {"SUSPICIOUS", "CLEAR"}, rule
    flagged = {props["id"] for labels, props in spec.nodes if props.get("risk_flags")}
    assert {verdicts[c["case_id"]] for c in cases if flagged & set(c["planted_ids"])} == {"SUSPICIOUS", "CLEAR"}
    lists = {props["id"]: props["list"] for labels, props in spec.nodes if labels.endswith(":Sanctioned")}
    targets = {c["expected"]["sanctioned_target"] for c in cases if c["category"] == "positive"}
    assert 4 <= len(targets) <= 6
    assert {lists[t] for t in targets} == {"OFAC-SDN", "UN-1267", "EU-CFSP", "UK-HMT"}
