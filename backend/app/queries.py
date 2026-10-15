"""Graph queries behind the FinTrace tools. Every function returns plain, JSON-ready data.

Traversals expand one hop per query over internal node IDs and return collected lists in a
single row. FalkorDB's variable-length patterns enumerate every path (parallel transfers make
depth 4+ time out), `algo.SSpaths` enumerates paths too, and `algo.BFS` only follows outgoing
edges, so none of them can count a depth-5 undirected neighborhood within a second.
"""

from __future__ import annotations

import queue
import time
from contextlib import contextmanager
from typing import Iterator

from falkordb import Graph

RISK_REL_TYPES = ["FLOWS_TO", "USED_DEVICE", "LOGGED_FROM"]
RISK_RELS = "|".join(RISK_REL_TYPES)
KIND_NAMES = ("accounts", "devices", "ips")
BROKER_SCOPE_LABELS = ("BrokerScope_1", "BrokerScope_2", "BrokerScope_3", "BrokerScope_4")
HIDDEN_LABELS = {"Entity", "RiskNode", *BROKER_SCOPE_LABELS}
FAR_FUTURE = 2**62

_EXPAND = f"""
UNWIND $frontier AS nid
MATCH (a) WHERE ID(a) = nid
MATCH (a)-[:{RISK_RELS}]-(b)
WITH b, min(ID(a)) AS parent
RETURN collect(ID(b)), collect(parent),
       collect(CASE WHEN b:Device THEN 1 WHEN b:IP THEN 2 ELSE 0 END),
       collect(CASE WHEN b:Sanctioned OR b.risk_flags IS NOT NULL THEN ID(b) END)
"""

_FLOW_OUT = """
UNWIND $frontier AS f
MATCH (a) WHERE ID(a) = f[0]
MATCH (a)-[t:TRANSFERRED]->(b)
WHERE t.ts >= f[1] AND t.ts <= $until AND t.amount >= $min_amount
WITH b, collect([t.ts, ID(a), t.amount, t.tx_id]) AS options
RETURN collect(ID(b)),
       collect(reduce(best = head(options), o IN options | CASE WHEN o[0] < best[0] THEN o ELSE best END))
"""

_FLOW_IN = """
UNWIND $frontier AS f
MATCH (a) WHERE ID(a) = f[0]
MATCH (a)<-[t:TRANSFERRED]-(b)
WHERE t.ts <= f[1] AND t.ts >= $since AND t.amount >= $min_amount
WITH b, collect([t.ts, ID(a), t.amount, t.tx_id]) AS options
RETURN collect(ID(b)),
       collect(reduce(best = head(options), o IN options | CASE WHEN o[0] > best[0] THEN o ELSE best END))
"""

_broker_scopes: queue.Queue[str] = queue.Queue()
for _label in BROKER_SCOPE_LABELS:
    _broker_scopes.put(_label)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def internal_id(graph: Graph, entity_id: str) -> int | None:
    rows = graph.ro_query("MATCH (n:Entity {id: $id}) RETURN ID(n)", {"id": entity_id}).result_set
    return rows[0][0] if rows else None


def describe_nodes(graph: Graph, node_ids: list[int]) -> dict[int, dict]:
    rows = graph.ro_query("""
        UNWIND $ids AS nid
        MATCH (n) WHERE ID(n) = nid
        RETURN nid, n.id, labels(n), n.name
    """, {"ids": list(node_ids)}).result_set
    return {nid: {"id": eid, "labels": [l for l in labels if l not in HIDDEN_LABELS], "name": name}
            for nid, eid, labels, name in rows}


def describe_steps(graph: Graph, pairs: list[tuple[int, int]]) -> dict[tuple[int, int], dict]:
    """Relationship type and direction for consecutive node pairs on a path."""
    rows = graph.ro_query(f"""
        UNWIND $pairs AS p
        MATCH (u) WHERE ID(u) = p[0]
        MATCH (v) WHERE ID(v) = p[1]
        MATCH (u)-[r:{RISK_RELS}]-(v)
        RETURN p[0], p[1], type(r), ID(startNode(r)) = p[0]
    """, {"pairs": [list(p) for p in pairs]}).result_set
    steps: dict[tuple[int, int], dict] = {}
    for u, v, rel, forward in rows:
        steps.setdefault((u, v), {"rel": rel, "forward": forward})
    return steps


def resolve_entity(graph: Graph, entity_id: str) -> dict | None:
    rows = graph.ro_query("""
        MATCH (n:Entity {id: $id})
        RETURN n.id, labels(n), n.name, n.status, n.topology_updated_at
    """, {"id": entity_id}).result_set
    if not rows:
        return None
    eid, labels, name, status, topology_updated_at = rows[0]
    return {"id": eid, "labels": [l for l in labels if l not in HIDDEN_LABELS], "name": name,
            "status": status, "topology_updated_at": topology_updated_at}


def _render_path(graph: Graph, node_ids: list[int]) -> dict:
    nodes = describe_nodes(graph, node_ids)
    steps = describe_steps(graph, list(zip(node_ids, node_ids[1:])))
    return {
        "nodes": [nodes[n]["id"] for n in node_ids],
        "steps": [{"from": nodes[u]["id"], "to": nodes[v]["id"], **steps.get((u, v), {})}
                  for u, v in zip(node_ids, node_ids[1:])],
    }


def explore_neighborhood(graph: Graph, entity_id: str, depth: int) -> dict | None:
    """Per-depth counts of everything reached, plus the flagged or sanctioned entities and a path to each."""
    started = time.perf_counter()
    start = internal_id(graph, entity_id)
    if start is None:
        return None
    parents: dict[int, int | None] = {start: None}
    frontier = [start]
    levels, hits = [], []
    for d in range(1, depth + 1):
        if not frontier:
            break
        ids, via, kinds, risky = graph.ro_query(_EXPAND, {"frontier": frontier}).result_set[0]
        counts = [0, 0, 0]
        frontier = []
        for nid, parent, kind in zip(ids, via, kinds):
            if nid not in parents:
                parents[nid] = parent
                frontier.append(nid)
                counts[kind] += 1
        new = set(frontier)
        found = [nid for nid in risky if nid in new]
        hits.extend((nid, d) for nid in found)
        levels.append({"depth": d, "reached": len(frontier), **dict(zip(KIND_NAMES, counts)),
                       "flagged": len(found)})

    flagged = []
    for nid, d in hits:
        chain = [nid]
        while parents[chain[-1]] is not None:
            chain.append(parents[chain[-1]])
        flagged.append({"depth": d, "path": _render_path(graph, chain[::-1])})
    if flagged:
        info = describe_nodes(graph, [nid for nid, _ in hits])
        for item, (nid, _) in zip(flagged, hits):
            item["entity"] = info[nid]
    return {
        "entity_id": entity_id,
        "depth": depth,
        "levels": levels,
        "total_reached": sum(level["reached"] for level in levels),
        "flagged": flagged,
        "elapsed_ms": _ms(started),
    }


def trace_money_flow(graph: Graph, entity_id: str, direction: str = "out", max_hops: int = 6,
                     since_ts: int = 0, until_ts: int = FAR_FUTURE, min_amount: int = 0,
                     frontier_cap: int = 300) -> dict | None:
    """Time-respecting money paths, expanded hop by hop.

    Downstream ("out") keeps the earliest arrival time per account and only follows transfers made
    at or after it; upstream ("in") keeps the latest departure and follows transfers made before it.
    Each hop keeps the `frontier_cap` largest arrivals.
    """
    started = time.perf_counter()
    start = internal_id(graph, entity_id)
    if start is None:
        return None
    downstream = direction == "out"
    query = _FLOW_OUT if downstream else _FLOW_IN
    best: dict[int, int] = {start: since_ts if downstream else until_ts}
    via: dict[int, list] = {}
    frontier = [[start, best[start]]]
    hops, cycles = [], []
    for hop in range(1, max_hops + 1):
        if not frontier:
            break
        ids, firsts = graph.ro_query(query, {"frontier": frontier, "since": since_ts, "until": until_ts,
                                             "min_amount": min_amount}).result_set[0]
        improved = []
        for nid, (ts, prev, amount, tx_id) in zip(ids, firsts):
            if nid == start:
                cycles.append({"hop": hop, "via": prev, "tx_id": tx_id, "ts": ts, "amount": amount})
                continue
            if nid not in best or (ts < best[nid] if downstream else ts > best[nid]):
                best[nid] = ts
                via[nid] = [prev, tx_id, ts, amount]
                improved.append((nid, ts, amount))
        improved.sort(key=lambda x: -x[2])
        improved = improved[:frontier_cap]
        frontier = [[nid, ts] for nid, ts, _ in improved]
        hops.append({"hop": hop, "accounts": len(improved), "amount": sum(a for _, _, a in improved)})

    reached = list(via)
    info = describe_nodes(graph, reached + [start]) if reached else {}
    sanctioned = [nid for nid in reached if "Sanctioned" in info[nid]["labels"]]

    def trail(nid: int) -> list[dict]:
        steps = []
        while nid in via:
            prev, tx_id, ts, amount = via[nid]
            src, dst = (prev, nid) if downstream else (nid, prev)
            steps.append({"from": info[src]["id"], "to": info[dst]["id"], "tx_id": tx_id, "ts": ts,
                          "amount": amount})
            nid = prev
        return steps[::-1] if downstream else steps

    return {
        "entity_id": entity_id,
        "direction": direction,
        "hops": hops,
        "accounts_reached": len(reached),
        "sanctioned": [{"entity": info[n], "trail": trail(n)} for n in sanctioned],
        "round_trips": len(cycles),
        "elapsed_ms": _ms(started),
    }


def path_between(graph: Graph, from_id: str, to_id: str) -> dict | None:
    started = time.perf_counter()
    rows = graph.ro_query("""
        MATCH (a:Entity {id: $from_id}), (b:Entity {id: $to_id})
        CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: $rels, weightProp: 'weight',
                           relDirection: 'both', pathCount: 1})
        YIELD path, pathWeight
        RETURN [n IN nodes(path) | ID(n)], pathWeight
    """, {"from_id": from_id, "to_id": to_id, "rels": RISK_REL_TYPES}).result_set
    if not rows:
        return None
    node_ids, weight = rows[0]
    return {"hops": int(weight), "path": _render_path(graph, node_ids), "elapsed_ms": _ms(started)}


def detect_ring(graph: Graph, entity_id: str, runs: int = 1, member_limit: int = 200) -> dict | None:
    """Label-propagation community of `entity_id`. With runs > 1, members must appear in a majority of runs."""
    started = time.perf_counter()
    votes: dict[str, int] = {}
    sizes = []
    for _ in range(runs):
        rows = graph.ro_query("""
            CALL algo.labelPropagation({nodeLabels: ['RiskNode'], relationshipTypes: $rels, maxIterations: 10})
            YIELD node, communityId
            WITH communityId, collect(node.id) AS members
            WHERE $id IN members
            RETURN size(members), members[0..$limit]
        """, {"rels": RISK_REL_TYPES, "id": entity_id, "limit": member_limit}).result_set
        if not rows:
            return None
        size, members = rows[0]
        sizes.append(size)
        for m in members:
            votes[m] = votes.get(m, 0) + 1
    members = sorted(m for m, n in votes.items() if n * 2 > runs)
    return {"entity_id": entity_id, "members": members, "community_sizes": sizes, "runs": runs,
            "elapsed_ms": _ms(started)}


@contextmanager
def broker_scope(timeout_s: float = 10.0) -> Iterator[str]:
    """Check out one of a fixed set of labels used to scope `algo.betweenness` to a ring."""
    label = _broker_scopes.get(timeout=timeout_s)
    try:
        yield label
    finally:
        _broker_scopes.put(label)


def find_brokers(graph: Graph, member_ids: list[str]) -> dict:
    started = time.perf_counter()
    with broker_scope() as label:
        try:
            graph.query(f"MATCH (n:Entity) WHERE n.id IN $ids AND n:RiskNode SET n:{label}", {"ids": member_ids})
            rows = graph.ro_query(f"""
                CALL algo.betweenness({{nodeLabels: ['{label}'], relationshipTypes: $rels,
                                        samplingSize: 64, samplingSeed: 42}})
                YIELD node, score
                RETURN node.id, score ORDER BY score DESC
            """, {"rels": RISK_REL_TYPES}).result_set
        finally:
            graph.query(f"MATCH (n:{label}) REMOVE n:{label}")
    return {"scores": [{"id": eid, "score": score} for eid, score in rows], "elapsed_ms": _ms(started)}


def exposure_flow(graph: Graph, source_ids: list[str], target_id: str) -> dict | None:
    """Maximum dollars that could reach `target_id` from `source_ids` over FLOWS_TO capacities.

    Capacities are whole dollars (clamped, see seed_data.build_flow_summary) and ignore timing,
    so the result is an upper bound.
    """
    started = time.perf_counter()
    rows = graph.ro_query("""
        MATCH (s:Entity) WHERE s.id IN $sources AND s:Account
        WITH collect(s) AS sources
        MATCH (t:Entity {id: $target})
        CALL algo.maxFlow({sourceNodes: sources, targetNodes: [t], nodeLabels: ['Account'],
                           relationshipTypes: ['FLOWS_TO'], capacityProperty: 'capacity'})
        YIELD edges, edgeFlows, maxFlow
        RETURN maxFlow, [e IN edges | [startNode(e).id, endNode(e).id]], edgeFlows
    """, {"sources": source_ids, "target": target_id}).result_set
    if not rows:
        return None
    max_flow, edges, flows = rows[0]
    used = [{"from": a, "to": b, "dollars": int(f)} for (a, b), f in zip(edges, flows) if f > 0]
    return {"source_ids": source_ids, "target_id": target_id, "max_flow_dollars": int(max_flow),
            "edges": used, "elapsed_ms": _ms(started)}
