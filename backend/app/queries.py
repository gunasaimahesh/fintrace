"""Graph queries behind the FinTrace tools. Every function returns plain, JSON-ready data.

Traversals expand one hop per query over internal node IDs and return collected lists in a
single row. FalkorDB's variable-length patterns enumerate every path (parallel transfers make
depth 4+ time out), `algo.SSpaths` enumerates paths too, and `algo.BFS` only follows outgoing
edges, so none of them can count a depth-5 undirected neighborhood within a second.
"""

from __future__ import annotations

import json
import queue
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from falkordb import Graph

from backend.app.contracts import PRESCRIBED_DEPTH, Claim, claim_fence, current_rev

RISK_REL_TYPES = ["FLOWS_TO", "USED_DEVICE", "LOGGED_FROM"]
RISK_RELS = "|".join(RISK_REL_TYPES)
KIND_NAMES = ("accounts", "devices", "ips")
ELIGIBLE_RULE = ("transfers followed in their own direction; identity hops only through a shared device "
                 "(account -> device <- account, counted as 2 hops); shared IPs are never followed")
SEARCH_NODE_BUDGET = 100_000
SEARCH_TIME_BUDGET_S = 5.0
LEVEL_TIMEOUT_MS = 5_000
MAX_ROUTES = 10
BROKER_SCOPE_LABELS = ("BrokerScope_1", "BrokerScope_2", "BrokerScope_3", "BrokerScope_4")
HIDDEN_LABELS = {"Entity", "RiskNode", *BROKER_SCOPE_LABELS}
FAR_FUTURE = 2**62
LEGIT_TIERS = ("vip", "enterprise", "standard")
FREEZE_ACTIONS = ("FREEZE",)
APPROVAL_TRIES = 5
PROFILE_KEYS = ("kind", "tier", "bank", "list", "risk_flags", "os", "address", "title", "clearance",
                "on_call", "mcc", "source", "frozen_at", "frozen_by")
DISPLAY_NAME = "coalesce(n.name, n.title, n.summary, n.address, n.os)"

_EXPAND = f"""
UNWIND $frontier AS nid
MATCH (a) WHERE ID(a) = nid
MATCH (a)-[:{RISK_RELS}]-(b)
WITH b, min(ID(a)) AS parent
RETURN collect(ID(b)), collect(parent),
       collect(CASE WHEN b:Device THEN 1 WHEN b:IP THEN 2 ELSE 0 END),
       collect(CASE WHEN b:Sanctioned OR b.risk_flags IS NOT NULL THEN ID(b) END)
"""

_ELIGIBLE_FROM_ACCOUNTS = """
UNWIND $frontier AS nid
MATCH (a) WHERE ID(a) = nid
MATCH (a)-[:FLOWS_TO|USED_DEVICE]->(b)
WITH b, min(ID(a)) AS parent
RETURN collect(ID(b)), collect(parent),
       collect(CASE WHEN b:Device THEN 1 ELSE 0 END),
       collect(CASE WHEN b:Sanctioned THEN ID(b) END)
"""

_ELIGIBLE_FROM_DEVICES = """
UNWIND $frontier AS nid
MATCH (d) WHERE ID(d) = nid
MATCH (d)<-[:USED_DEVICE]-(b)
WITH b, min(ID(d)) AS parent
RETURN collect(ID(b)), collect(parent),
       collect(CASE WHEN b:Device THEN 1 ELSE 0 END),
       collect(CASE WHEN b:Sanctioned THEN ID(b) END)
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


def _visible(labels: list[str]) -> list[str]:
    return [label for label in labels if label not in HIDDEN_LABELS]


def _usd(cents: int | float | None) -> float | None:
    return None if cents is None else round(cents / 100, 2)


def dataset_meta(graph: Graph) -> dict:
    rows = graph.ro_query("MATCH (m:Meta {key: 'dataset'}) RETURN m {.*}").result_set
    return dict(rows[0][0]) if rows else {}


def internal_id(graph: Graph, entity_id: str) -> int | None:
    rows = graph.ro_query("MATCH (n:Entity {id: $id}) RETURN ID(n)", {"id": entity_id}).result_set
    return rows[0][0] if rows else None


def describe_nodes(graph: Graph, node_ids: list[int]) -> dict[int, dict]:
    rows = graph.ro_query(f"""
        UNWIND $ids AS nid
        MATCH (n) WHERE ID(n) = nid
        RETURN nid, n.id, labels(n), {DISPLAY_NAME}
    """, {"ids": list(node_ids)}).result_set
    return {nid: {"id": eid, "labels": _visible(labels), "name": name} for nid, eid, labels, name in rows}


def describe_entities(graph: Graph, entity_ids: list[str]) -> dict[str, dict]:
    rows = graph.ro_query(f"""
        UNWIND $ids AS eid
        MATCH (n:Entity {{id: eid}})
        RETURN n.id, labels(n), {DISPLAY_NAME}
    """, {"ids": list(entity_ids)}).result_set
    return {eid: {"id": eid, "labels": _visible(labels), "name": name} for eid, labels, name in rows}


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
        if (u, v) not in steps or (forward and not steps[(u, v)]["forward"]):
            steps[(u, v)] = {"rel": rel, "forward": forward}
    return steps


def resolve_entity(graph: Graph, entity_id: str) -> dict | None:
    rows = graph.ro_query("""
        MATCH (n:Entity {id: $id})
        OPTIONAL MATCH (n)-[o:FLOWS_TO]->()
        WITH n, count(o) AS sends_to
        OPTIONAL MATCH (n)<-[i:FLOWS_TO]-()
        WITH n, sends_to, count(i) AS receives_from
        OPTIONAL MATCH (n)-[x:USED_DEVICE|LOGGED_FROM]-()
        RETURN n {.*}, labels(n), sends_to, receives_from, count(x)
    """, {"id": entity_id}).result_set
    if not rows:
        return None
    props, labels, sends_to, receives_from, identity_links = rows[0]
    profile = {key: props[key] for key in PROFILE_KEYS if props.get(key) is not None}
    if props.get("monthly_volume") is not None:
        profile["monthly_volume_usd"] = _usd(props["monthly_volume"])
    return {
        "id": props["id"],
        "labels": _visible(labels),
        "name": props.get("name") or props.get("title"),
        "status": props.get("status"),
        "topology_updated_at": props.get("topology_updated_at"),
        "profile": profile,
        "counterparties": {"sends_to": sends_to, "receives_from": receives_from},
        "identity_links": identity_links,
    }


def _render_path(graph: Graph, node_ids: list[int]) -> dict:
    nodes = describe_nodes(graph, node_ids)
    steps = describe_steps(graph, list(zip(node_ids, node_ids[1:])))
    return {
        "nodes": [nodes[n]["id"] for n in node_ids],
        "steps": [{"from": nodes[u]["id"], "to": nodes[v]["id"], **steps.get((u, v), {})}
                  for u, v in zip(node_ids, node_ids[1:])],
        "entities": [nodes[n] for n in node_ids],
    }


def _is_timeout(exc: Exception) -> bool:
    return "timed out" in str(exc).lower()


def _chain(parents: dict[int, int | None], nid: int) -> list[int]:
    chain = [nid]
    while parents[chain[-1]] is not None:
        chain.append(parents[chain[-1]])
    return chain[::-1]


class _Frontier:
    """Breadth-first expansion with an explicit record of how much of the requested scope it covered."""

    def __init__(self, start: int, depth: int, node_budget: int, time_budget_s: float):
        self.started = time.perf_counter()
        self.depth, self.node_budget = depth, node_budget
        self.deadline = self.started + time_budget_s
        self.parents: dict[int, int | None] = {start: None}
        self.completed, self.exhausted, self.truncated, self.timed_out = 0, False, False, False

    def levels(self) -> Iterator[int]:
        for d in range(1, self.depth + 1):
            if time.perf_counter() > self.deadline:
                self.timed_out = True
                return
            yield d

    def finish_level(self, d: int, new: int) -> bool:
        """Record a fully expanded level; False when the search must stop here."""
        self.completed = d
        if new == 0:
            self.exhausted = True
            return False
        if d < self.depth and len(self.parents) > self.node_budget:
            self.truncated = True
            return False
        return True

    def scope(self) -> dict:
        return {"requested_depth": self.depth, "completed_depth": self.completed, "exhausted": self.exhausted,
                "reached": len(self.parents) - 1, "truncated": self.truncated, "timed_out": self.timed_out,
                "complete": self.exhausted or (self.completed == self.depth and not self.timed_out),
                "elapsed_ms": _ms(self.started)}


def eligible_routes(graph: Graph, start: int, depth: int, node_budget: int | None = None,
                    time_budget_s: float | None = None) -> dict:
    """Shortest eligible route to every sanctioned account within `depth` hops (see ELIGIBLE_RULE).

    Each edge costs one hop, so a device bridge (account -> device <- account) costs two. Steps are
    rebuilt from the search itself, so each keeps the relationship and direction the search used.
    """
    search = _Frontier(start, depth, node_budget or SEARCH_NODE_BUDGET, time_budget_s or SEARCH_TIME_BUDGET_S)
    start_kind = graph.ro_query("MATCH (n) WHERE ID(n) = $id RETURN n:Device", {"id": start}).result_set[0][0]
    devices: set[int] = {start} if start_kind else set()
    accounts, device_frontier = ([], [start]) if start_kind else ([start], [])
    hits: list[tuple[int, int]] = []
    for d in search.levels():
        new_accounts, new_devices = [], []
        try:
            for query, frontier in ((_ELIGIBLE_FROM_ACCOUNTS, accounts), (_ELIGIBLE_FROM_DEVICES, device_frontier)):
                if not frontier:
                    continue
                ids, via, is_device, sanctioned = graph.ro_query(query, {"frontier": frontier},
                                                                 timeout=LEVEL_TIMEOUT_MS).result_set[0]
                found = set(sanctioned)
                for nid, parent, dev in zip(ids, via, is_device):
                    if nid in search.parents:
                        continue
                    search.parents[nid] = parent
                    if dev:
                        devices.add(nid)
                        new_devices.append(nid)
                    else:
                        new_accounts.append(nid)
                    if nid in found:
                        hits.append((nid, d))
        except Exception as exc:
            if not _is_timeout(exc):
                raise
            search.timed_out = True
            break
        accounts, device_frontier = new_accounts, new_devices
        if not search.finish_level(d, len(new_accounts) + len(new_devices)):
            break

    routes = []
    hits = hits[:MAX_ROUTES]
    chains = [_chain(search.parents, nid) for nid, _ in hits]
    info = describe_nodes(graph, sorted({n for chain in chains for n in chain})) if chains else {}
    for (nid, d), chain in zip(hits, chains):
        steps = []
        for u, v in zip(chain, chain[1:]):
            rel, forward = ("USED_DEVICE", True) if v in devices else ("USED_DEVICE", False) if u in devices \
                else ("FLOWS_TO", True)
            steps.append({"from": info[u]["id"], "to": info[v]["id"], "rel": rel, "forward": forward})
        routes.append({"entity": info[nid], "hops": d,
                       "path": {"nodes": [info[n]["id"] for n in chain], "steps": steps,
                                "entities": [info[n] for n in chain]}})
    return {"rule": ELIGIBLE_RULE, "scope": search.scope(), "routes": routes}


def explore_neighborhood(graph: Graph, entity_id: str, depth: int, neighbor_limit: int = 0,
                         node_budget: int | None = None, time_budget_s: float | None = None) -> dict | None:
    """Per-depth counts of everything reached over any risk relationship, the flagged or sanctioned entities
    with a shortest path to each, and separately the eligible routes (see `eligible_routes`). Each search
    reports the scope it covered.

    With `neighbor_limit`, also lists up to that many direct neighbors and how each one connects.
    """
    started = time.perf_counter()
    start = internal_id(graph, entity_id)
    if start is None:
        return None
    search = _Frontier(start, depth, node_budget or SEARCH_NODE_BUDGET, time_budget_s or SEARCH_TIME_BUDGET_S)
    frontier = [start]
    first_ring: list[int] = []
    levels, hits = [], []
    for d in search.levels():
        try:
            ids, via, kinds, risky = graph.ro_query(_EXPAND, {"frontier": frontier},
                                                    timeout=LEVEL_TIMEOUT_MS).result_set[0]
        except Exception as exc:
            if not _is_timeout(exc):
                raise
            search.timed_out = True
            break
        counts = [0, 0, 0]
        frontier = []
        for nid, parent, kind in zip(ids, via, kinds):
            if nid not in search.parents:
                search.parents[nid] = parent
                frontier.append(nid)
                counts[kind] += 1
        if d == 1:
            first_ring = frontier[:neighbor_limit]
        new = set(frontier)
        found = [nid for nid in risky if nid in new]
        hits.extend((nid, d) for nid in found)
        levels.append({"depth": d, "reached": len(frontier), **dict(zip(KIND_NAMES, counts)),
                       "flagged": len(found)})
        if not search.finish_level(d, len(frontier)):
            break

    flagged = [{"depth": d, "path": _render_path(graph, _chain(search.parents, nid))} for nid, d in hits]
    if flagged:
        info = describe_nodes(graph, [nid for nid, _ in hits])
        for item, (nid, _) in zip(flagged, hits):
            item["entity"] = info[nid]
    result = {
        "entity_id": entity_id,
        "depth": depth,
        "levels": levels,
        "total_reached": sum(level["reached"] for level in levels),
        "flagged": flagged,
        "scope": search.scope(),
        "eligible": eligible_routes(graph, start, depth, node_budget, time_budget_s),
    }
    if first_ring:
        info = describe_nodes(graph, [start, *first_ring])
        steps = describe_steps(graph, [(start, n) for n in first_ring])
        result["neighbors"] = [{"entity": info[n], **steps.get((start, n), {})} for n in first_ring]
    result["elapsed_ms"] = _ms(started)
    return result


def trace_money_flow(graph: Graph, entity_id: str, direction: str = "out", max_hops: int = 6,
                     since_ts: int = 0, until_ts: int = FAR_FUTURE, min_amount: int = 0,
                     frontier_cap: int = 300, top_per_hop: int = 3) -> dict | None:
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
    hops, cycles, tops, capped = [], [], [], []
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
        if len(improved) > frontier_cap:
            capped.append({"hop": hop, "kept": frontier_cap, "dropped": len(improved) - frontier_cap})
        improved = improved[:frontier_cap]
        frontier = [[nid, ts] for nid, ts, _ in improved]
        tops.append([(nid, list(via[nid])) for nid, _, _ in improved[:top_per_hop]])
        hops.append({"hop": hop, "accounts": len(improved), "amount": sum(a for _, _, a in improved)})

    reached = list(via)
    info = describe_nodes(graph, reached + [start] + [c["via"] for c in cycles]) if reached or cycles else {}
    sanctioned = [nid for nid in reached if "Sanctioned" in info[nid]["labels"]]

    def render(nid: int, step: list) -> dict:
        prev, tx_id, ts, amount = step
        src, dst = (prev, nid) if downstream else (nid, prev)
        return {"from": info[src]["id"], "to": info[dst]["id"], "tx_id": tx_id, "ts": ts, "amount": amount}

    def trail(nid: int) -> list[dict]:
        steps = []
        while nid in via:
            steps.append(render(nid, via[nid]))
            nid = via[nid][0]
        return steps[::-1] if downstream else steps

    for hop, top in zip(hops, tops):
        hop["largest"] = [{"entity": info[nid], "transfer": render(nid, step)} for nid, step in top]
    return {
        "entity_id": entity_id,
        "direction": direction,
        "hops": hops,
        "accounts_reached": len(reached),
        "sanctioned": [{"entity": info[n], "trail": trail(n)} for n in sanctioned],
        "round_trips": [{"hop": c["hop"], "from": info[c["via"]]["id"], "tx_id": c["tx_id"], "ts": c["ts"],
                         "amount": c["amount"]} for c in cycles],
        "capped_hops": capped,
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


def accounts_sharing_devices(graph: Graph, account_ids: list[str]) -> dict[str, dict]:
    """Other accounts that used the same device as any of `account_ids`, keyed by device."""
    rows = graph.ro_query("""
        UNWIND $ids AS aid
        MATCH (a:Entity {id: aid})-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(b:Account)
        WHERE NOT b.id IN $ids
        RETURN d.id, collect(DISTINCT a.id), collect(DISTINCT b.id)
    """, {"ids": list(account_ids)}).result_set
    return {device: {"used_by": sorted(used), "also_used_by": sorted(others)} for device, used, others in rows}


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


def topology_fingerprint(graph: Graph, entity_id: str) -> dict | None:
    """Cheap structural features around an entity, used to match playbooks (never labels)."""
    rows = graph.ro_query("""
        MATCH (x:Entity {id: $id})
        OPTIONAL MATCH (x)-[:FLOWS_TO]->(o)
        WITH x, count(DISTINCT o) AS fan_out
        OPTIONAL MATCH (x)<-[:FLOWS_TO]-(i)
        WITH x, fan_out, count(DISTINCT i) AS fan_in
        OPTIONAL MATCH (x)-[:USED_DEVICE]->(:Device)<-[:USED_DEVICE]-(sd:Account)
        WHERE sd <> x
        WITH x, fan_out, fan_in, count(DISTINCT sd) AS shared_device
        OPTIONAL MATCH (x)-[:LOGGED_FROM]->(:IP)<-[:LOGGED_FROM]-(si:Account)
        WHERE si <> x
        WITH x, fan_out, fan_in, shared_device, count(DISTINCT si) AS shared_ip
        OPTIONAL MATCH (x)-[:FLOWS_TO]-(nb:Account)-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(far:Account)
        WHERE far <> nb AND far <> x
        WITH x, fan_out, fan_in, shared_device, shared_ip, count(DISTINCT d) AS device_bridges
        OPTIONAL MATCH (x)-[:FLOWS_TO]->(b)-[:FLOWS_TO]->(x)
        RETURN fan_out, fan_in, shared_device, shared_ip, device_bridges, count(DISTINCT b)
    """, {"id": entity_id}).result_set
    if not rows:
        return None
    fan_out, fan_in, shared_device, shared_ip, device_bridges, two_cycles = rows[0]
    fingerprint = {"fan_out": fan_out, "fan_in": fan_in, "accounts_sharing_device": shared_device,
                   "accounts_sharing_ip": shared_ip, "device_bridges_one_hop": device_bridges,
                   "two_step_cycles": two_cycles}
    patterns = []
    if shared_device:
        patterns.append("shares_device")
    if device_bridges:
        patterns.append("counterparty_shares_device")
    if fan_in >= 8 and fan_out <= 2:
        patterns.append("fan_in")
    if fan_out >= 8 and fan_in <= 2:
        patterns.append("fan_out")
    if two_cycles:
        patterns.append("two_step_cycle")
    return {"features": fingerprint, "patterns": patterns}


def changed_dependencies(graph: Graph, depends_on: list[str], rev: int) -> tuple[list[str], list[str]]:
    """Which of these node IDs and edge keys (src|REL|dst) are gone, and which nodes were touched after `rev`."""
    nodes = list(dict.fromkeys(d for d in depends_on if "|" not in d))
    edges = list(dict.fromkeys(d for d in depends_on if "|" in d))
    missing, touched = [], []
    if nodes:
        rows = graph.ro_query("""
            UNWIND $ids AS id
            OPTIONAL MATCH (n:Entity {id: id})
            RETURN id, n IS NOT NULL, coalesce(n.topology_rev, 0)
        """, {"ids": nodes}).result_set
        for nid, exists, node_rev in rows:
            if not exists:
                missing.append(nid)
            elif node_rev > rev:
                touched.append(nid)
    if edges:
        rows = graph.ro_query("""
            UNWIND range(0, size($edges) - 1) AS i
            WITH i, $edges[i] AS e
            OPTIONAL MATCH (a:Entity {id: e[0]})-[r]->(b:Entity {id: e[2]})
            WHERE type(r) = e[1]
            RETURN i, count(r) > 0
        """, {"edges": [e.split("|") for e in edges]}).result_set
        missing += [edges[i] for i, exists in rows if not exists]
    return missing, touched


def _listed(dependencies: list[str]) -> str:
    names = [f"{d.split('|')[0]} → {d.split('|')[2]} ({d.split('|')[1]})" if "|" in d else d for d in dependencies]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def describe_change(missing: list[str], touched: list[str], rev: int) -> str:
    parts = []
    if missing:
        parts.append(f"{_listed(missing)} no longer exist{'s' if len(missing) == 1 else ''}")
    if touched:
        parts.append(f"{_listed(touched)} changed after graph revision {rev}")
    return "; ".join(parts)


def playbooks_for(graph: Graph, patterns: list[str]) -> list[dict]:
    """Learned playbooks whose required topology patterns the entity has, best supported first."""
    rows = graph.ro_query("""
        MATCH (pb:Playbook)
        WHERE size(pb.required_patterns) > 0 AND all(p IN pb.required_patterns WHERE p IN $patterns)
        MATCH (pb)-[d:DERIVED_FROM]->(ep:Episode)
        WITH pb, collect({episode_id: ep.id, case_id: ep.case_id, subject_id: ep.entity_id,
                          request_id: d.request_id, approved_by: d.approved_by, at: d.at}) AS sources
        RETURN pb {.id, .family, .steps, .required_patterns, .uses, .created_at, .last_used_at}, sources
        ORDER BY size(sources) DESC, pb.created_at DESC
    """, {"patterns": patterns}).result_set
    return [{**dict(pb), "steps": json.loads(pb["steps"]), "derived_from": sources} for pb, sources in rows]


def _waiver_violations(decision: dict, approver: dict | None, policy: dict | None) -> list[dict]:
    if not approver or not policy or policy.get("waiver_threshold_monthly") is None:
        return []
    volume = decision.get("merchant_monthly_volume") or 0
    if volume <= policy["waiver_threshold_monthly"]:
        return []
    if (approver.get("clearance_level") or 0) >= (policy.get("required_clearance_level") or 0):
        return []
    return [{
        "policy_id": policy["id"],
        "rule": "waiver_clearance",
        "detail": (f"{approver['id']} ({approver.get('clearance')}) approved a waiver for a merchant processing "
                   f"${volume / 100:,.0f}/month; {policy['id']} requires {policy.get('required_clearance')} "
                   f"above ${policy['waiver_threshold_monthly'] / 100:,.0f}/month"),
    }]


def trace_governance(graph: Graph, entity_id: str) -> dict | None:
    """Decisions that apply to an entity, who approved them under which policy, and who can approve a freeze."""
    started = time.perf_counter()
    if internal_id(graph, entity_id) is None:
        return None
    rows = graph.ro_query("""
        MATCH (x:Entity {id: $id})<-[:APPLIES_TO]-(d:Decision)
        OPTIONAL MATCH (e:Employee)-[:APPROVED]->(d)
        OPTIONAL MATCH (e)-[:MEMBER_OF]->(t:Team)
        OPTIONAL MATCH (d)-[:GOVERNED_BY]->(p:PolicyDoc)
        RETURN d {.*}, e {.id, .name, .title, .clearance, .clearance_level}, t {.id, .name},
               p {.id, .title, .version, .text, .waiver_threshold_monthly, .required_clearance,
                  .required_clearance_level, .required_clearance_for_freeze}
    """, {"id": entity_id}).result_set
    decisions = []
    for d, approver, team, policy in rows:
        d, approver, team, policy = (dict(x) if x else None for x in (d, approver, team, policy))
        violations = _waiver_violations(d, approver, policy)
        if policy:
            policy["waiver_threshold_monthly_usd"] = _usd(policy.pop("waiver_threshold_monthly", None))
        decisions.append({
            "id": d["id"], "kind": d.get("kind"), "control": d.get("control"), "status": d.get("status"),
            "summary": d.get("summary"), "approved_at": d.get("approved_at"),
            "merchant_monthly_volume_usd": _usd(d.get("merchant_monthly_volume")),
            "approved_by": approver, "team": team, "policy": policy,
            "violations": violations, "sources": [],
        })

    by_id = {d["id"]: d for d in decisions}
    for did, src, labels, who, who_name, how in graph.ro_query("""
        UNWIND $ids AS did
        MATCH (d:Entity {id: did})-[:SOURCED_FROM]->(s:Entity)
        OPTIONAL MATCH (w:Employee)-[r:OPENED|AUTHORED]->(s)
        RETURN did, s {.id, .system, .title, .text, .status, .channel}, labels(s), w.id, w.name, type(r)
    """, {"ids": list(by_id)}).result_set:
        by_id[did]["sources"].append({**dict(src), "type": _visible(labels)[0],
                                      "by": {"id": who, "name": who_name, "relation": how} if who else None})

    approvers = sorted({d["approved_by"]["id"] for d in decisions if d["approved_by"]})
    other_decisions = []
    for aid, d, applies_to, labels, name, policy in graph.ro_query("""
        UNWIND $ids AS aid
        MATCH (e:Entity {id: aid})-[:APPROVED]->(d:Decision)-[:APPLIES_TO]->(m:Entity)
        WHERE NOT d.id IN $seen
        OPTIONAL MATCH (d)-[:GOVERNED_BY]->(p:PolicyDoc)
        RETURN aid, d {.*}, m.id, labels(m), m.name,
               p {.id, .waiver_threshold_monthly, .required_clearance, .required_clearance_level}
    """, {"ids": approvers, "seen": list(by_id)}).result_set:
        approver = next(x["approved_by"] for x in decisions if x["approved_by"] and x["approved_by"]["id"] == aid)
        d = dict(d)
        other_decisions.append({
            "approved_by": aid, "decision_id": d["id"], "control": d.get("control"),
            "applies_to": {"id": applies_to, "labels": _visible(labels), "name": name},
            "merchant_monthly_volume_usd": _usd(d.get("merchant_monthly_volume")),
            "policy_id": policy["id"] if policy else None,
            "violations": _waiver_violations(d, approver, dict(policy) if policy else None),
        })

    policy_rows = graph.ro_query("""
        MATCH (p:PolicyDoc) WHERE p.required_clearance_level_for_freeze IS NOT NULL
        OPTIONAL MATCH (e:Employee)
        WHERE e.on_call = true AND e.clearance_level >= p.required_clearance_level_for_freeze
        OPTIONAL MATCH (e)-[:MEMBER_OF]->(t:Team)
        RETURN p.id, p.title, p.required_clearance_for_freeze,
               collect(CASE WHEN e IS NULL THEN NULL
                            ELSE [e.id, e.name, e.title, e.clearance, t.id, t.name] END)
    """).result_set
    governing = {d["policy"]["id"] for d in decisions if d["policy"]}
    policy_rows.sort(key=lambda r: r[0] not in governing)
    freeze_policy, approvers_on_call = None, []
    if policy_rows:
        pid, title, clearance, people = policy_rows[0]
        freeze_policy = {"id": pid, "title": title, "required_clearance": clearance}
        approvers_on_call = [{"id": e, "name": n, "title": ti, "clearance": c, "team": {"id": tid, "name": tn}}
                             for e, n, ti, c, tid, tn in people]
    return {
        "entity_id": entity_id,
        "decisions": decisions,
        "approver_other_decisions": other_decisions,
        "freeze_policy": freeze_policy,
        "on_call_approvers": approvers_on_call,
        "elapsed_ms": _ms(started),
    }


def freeze_impact(graph: Graph, target_ids: list[str]) -> dict:
    """Accounts outside `target_ids` that share a device or IP with them: legitimate collateral, or also
    exposed when they have an eligible route of their own to a sanctioned account."""
    started = time.perf_counter()
    known = describe_entities(graph, target_ids)
    rows = graph.ro_query("""
        UNWIND $targets AS tid
        MATCH (t:Entity {id: tid})-[r:USED_DEVICE|LOGGED_FROM]->(inf:Entity)
        OPTIONAL MATCH (inf)<-[r2:USED_DEVICE|LOGGED_FROM]-(o:Account)
        WHERE NOT o.id IN $targets
        RETURN inf.id, labels(inf), inf.kind, collect(DISTINCT [t.id, type(r)]),
               collect(DISTINCT CASE WHEN o IS NULL THEN NULL
                                     ELSE [o.id, o.name, o.tier, o.status, o.monthly_volume, type(r2), ID(o)] END)
    """, {"targets": target_ids}).result_set
    routes: dict[str, dict | None] = {}

    def nearest_route(account: str, node: int) -> dict | None:
        if account not in routes:
            found = eligible_routes(graph, node, PRESCRIBED_DEPTH)["routes"]
            routes[account] = min(found, key=lambda r: r["hops"]) if found else None
        return routes[account]

    infrastructure, collateral, exposed = [], {}, {}
    for inf, labels, kind, users, outsiders in rows:
        active = [o for o in outsiders if o[3] == "ACTIVE" and o[2] in LEGIT_TIERS]
        legit = [o for o in active if not nearest_route(o[0], o[6])]
        infrastructure.append({
            "id": inf, "type": _visible(labels)[0], "kind": kind,
            "used_by_targets": [{"id": u, "rel": rel} for u, rel in users],
            "outside_accounts": [{"id": o[0], "rel": o[5]} for o in outsiders],
            "safe_to_block": not legit,
        })
        for oid, name, tier, status, volume, rel, _ in active:
            route = routes[oid]
            if route:
                entry = exposed.setdefault(oid, {"id": oid, "name": name, "sanctioned": route["entity"]["id"],
                                                 "route_hops": route["hops"], "shared_via": []})
            else:
                entry = collateral.setdefault(oid, {"id": oid, "name": name, "tier": tier, "status": status,
                                                    "monthly_volume_usd": _usd(volume), "shared_via": []})
            entry["shared_via"].append(inf)
    return {
        "target_ids": target_ids,
        "unknown_targets": [t for t in target_ids if t not in known],
        "targets": [known[t] for t in target_ids if t in known],
        "infrastructure": infrastructure,
        "collateral": sorted(collateral.values(), key=lambda c: -(c["monthly_volume_usd"] or 0)),
        "also_exposed": sorted(exposed.values(), key=lambda c: c["route_hops"]),
        "already_requested": pending_requests(graph, target_ids),
        "elapsed_ms": _ms(started),
    }


def pending_requests(graph: Graph, target_ids: list[str]) -> list[dict]:
    """Pending freeze requests that already target any of these accounts, oldest first, with the case and the
    episode that filed them (none yet if that investigation is still running)."""
    rows = graph.ro_query("""
        MATCH (r:ApprovalRequest)
        WHERE r.status = 'PENDING_L3' AND any(t IN r.target_ids WHERE t IN $targets)
        OPTIONAL MATCH (r)-[:FOR_CASE]->(c:Case)
        OPTIONAL MATCH (ep:Episode {request_id: r.id})
        RETURN r.id, c.id, r.requested_by, [t IN r.target_ids WHERE t IN $targets], r.created_at, collect(ep.id)
        ORDER BY r.created_at, r.id
    """, {"targets": target_ids}).result_set
    return [{"request_id": rid, "case_id": case, "requested_by": by, "target_ids": overlap, "created_at": created,
             "episode_id": episodes[0] if episodes else None}
            for rid, case, by, overlap, created, episodes in rows]


def request_action(graph: Graph, case_id: str, target_ids: list[str], excluded_ids: list[str], action_type: str,
                   policy_id: str, approver_id: str, reason: str, requested_by: str,
                   now: int | None = None, claim: Claim | None = None) -> dict:
    """Create a pending approval request. Raises ValueError with an explanation if it can't be routed, or if
    `claim` (the hunter's hold on its alert) no longer holds."""
    now = now or int(time.time())
    action_type = action_type.upper()
    if action_type not in FREEZE_ACTIONS:
        raise ValueError(f"action_type must be one of {list(FREEZE_ACTIONS)}")
    if overlap := sorted(set(target_ids) & set(excluded_ids)):
        raise ValueError(f"{overlap} are both targeted and excluded")
    entities = describe_entities(graph, [*target_ids, *excluded_ids])
    if missing := [e for e in [*target_ids, *excluded_ids] if e not in entities]:
        raise ValueError(f"unknown entities: {missing}")
    if not_accounts := [t for t in target_ids if "Account" not in entities[t]["labels"]]:
        raise ValueError(f"only accounts can be frozen; not accounts: {not_accounts}")
    if sanctioned := [t for t in target_ids if "Sanctioned" in entities[t]["labels"]]:
        raise ValueError(f"sanctioned accounts are already blocked, so they are not put up for a freeze: {sanctioned}")

    rows = graph.ro_query("""
        MATCH (p:Entity {id: $policy}) WHERE p:PolicyDoc
        MATCH (e:Entity {id: $approver}) WHERE e:Employee
        RETURN p.required_clearance_level_for_freeze, p.required_clearance_for_freeze,
               e.clearance_level, e.clearance, e.on_call, e.name
    """, {"policy": policy_id, "approver": approver_id}).result_set
    if not rows:
        raise ValueError(f"{policy_id} is not a policy or {approver_id} is not an employee")
    required_level, required, level, clearance, on_call, name = rows[0]
    if required_level is None:
        raise ValueError(f"{policy_id} does not govern freezes")
    if (level or 0) < required_level or not on_call:
        raise ValueError(f"{approver_id} ({clearance}, on call: {bool(on_call)}) cannot approve; {policy_id} "
                         f"requires an on-call approver with {required}. Use trace_governance to find one.")

    existing = graph.ro_query("""
        MATCH (r:ApprovalRequest)-[:FOR_CASE]->(c:Entity {id: $case})
        WHERE r.status = 'PENDING_L3'
        RETURN r.id, r.target_ids
    """, {"case": case_id}).result_set
    for rid, targets in existing:
        if sorted(targets) == sorted(target_ids):
            return {"request_id": rid, "status": "PENDING_L3", "created": False, "case_id": case_id,
                    "target_ids": target_ids, "excluded_ids": excluded_ids, "policy_id": policy_id,
                    "approver": {"id": approver_id, "name": name, "clearance": clearance}, "entities": entities}

    rid = f"apr-{uuid.uuid4().hex[:8]}"
    fence, owned = claim_fence(claim)
    # Checks and CREATE stay in one write query: FalkorDB runs writes one at a time, so two investigations
    # racing to freeze the same account cannot both file, and a hunter whose claim lapsed files nothing.
    rows = graph.query(fence + """
        MATCH (p:Entity {id: $policy}), (e:Entity {id: $approver})
        OPTIONAL MATCH (other:ApprovalRequest)
        WHERE other.status = 'PENDING_L3' AND any(t IN other.target_ids WHERE t IN $targets)
        OPTIONAL MATCH (other)-[:FOR_CASE]->(oc:Case)
        WITH p, e, collect(CASE WHEN other IS NULL THEN NULL
                                ELSE [other.id, oc.id, [t IN other.target_ids WHERE t IN $targets]] END) AS clashes
        FOREACH (_ IN CASE WHEN size(clashes) = 0 THEN [1] ELSE [] END |
            MERGE (c:Entity:Case {id: $case})
            ON CREATE SET c.status = 'OPEN', c.created_at = $now, c.subject_id = $subject
            CREATE (r:Entity:ApprovalRequest {id: $rid, status: 'PENDING_L3', action_type: $action, reason: $reason,
                                              created_at: $now, requested_by: $by, target_ids: $targets,
                                              excluded_ids: $excluded, required_clearance: $required,
                                              evidence_rev: $rev, depends_on: $targets})
            CREATE (r)-[:FOR_CASE]->(c), (r)-[:UNDER_POLICY]->(p), (r)-[:ROUTED_TO]->(e))
        RETURN clashes
    """, {"case": case_id, "now": now, "subject": target_ids[0], "policy": policy_id, "approver": approver_id,
          "rid": rid, "action": action_type, "reason": reason, "by": requested_by, "targets": target_ids,
          "excluded": excluded_ids, "required": required, "rev": current_rev(graph), **owned}).result_set
    if not rows:
        raise ValueError(f"{requested_by} no longer holds the claim on {claim.alert_id} (its lease ran out or another "
                         f"hunter took the alert over), so no request was filed" if claim
                         else f"{policy_id} or {approver_id} is no longer in the graph")
    clashes = rows[0][0]
    if clashes:
        listed = "; ".join(f"{', '.join(overlap)} in {other} ({case})" for other, case, overlap in clashes)
        raise ValueError(f"already awaiting sign-off: {listed}. An account can be in only one pending request: "
                         f"leave those out and request the rest, if any.")
    for rel, ids in (("TARGETS", target_ids), ("EXCLUDES", excluded_ids)):
        if ids:
            graph.query(f"""
                MATCH (r:Entity {{id: $rid}})
                UNWIND $ids AS eid
                MATCH (t:Entity {{id: eid}})
                CREATE (r)-[:{rel}]->(t)
            """, {"rid": rid, "ids": ids})
    return {"request_id": rid, "status": "PENDING_L3", "created": True, "case_id": case_id,
            "target_ids": target_ids, "excluded_ids": excluded_ids, "policy_id": policy_id,
            "approver": {"id": approver_id, "name": name, "clearance": clearance}, "entities": entities}


def approve_action(graph: Graph, request_id: str, approver_id: str, now: int | None = None) -> dict:
    """An approver signs off a pending request; its target accounts are frozen.

    A request whose evidence changed after it was raised becomes STALE instead. The evidence is checked at the
    current revision, and the sign-off commits in one write only if the graph is still at that revision, the request
    is still pending and the approver still has the clearance the policy requires; otherwise everything is read and
    checked again. Freezing changes status only, so it is not evidence and doesn't bump the revision.
    """
    now = now or int(time.time())
    for _ in range(APPROVAL_TRIES):
        rows = graph.ro_query("""
            MATCH (r:Entity {id: $rid})-[:UNDER_POLICY]->(p:Entity)
            MATCH (e:Entity {id: $approver}) WHERE e:Employee
            OPTIONAL MATCH (meta:Meta {key: 'dataset'})
            RETURN r:ApprovalRequest, r.status, p.required_clearance_level_for_freeze, p.required_clearance_for_freeze,
                   e.clearance_level, e.clearance, r.depends_on, r.evidence_rev, coalesce(meta.rev, 0)
        """, {"rid": request_id, "approver": approver_id}).result_set
        if not rows or not rows[0][0]:
            raise ValueError(f"no approval request {request_id} (or {approver_id} is not an employee)")
        _, status, required_level, required, level, clearance, depends_on, evidence_rev, rev = rows[0]
        if status != "PENDING_L3":
            raise ValueError(f"{request_id} is already {status}")
        if (level or 0) < (required_level or 0):
            raise ValueError(f"{approver_id} has {clearance}; this action requires {required}")
        if depends_on and evidence_rev is not None:
            missing, touched = changed_dependencies(graph, depends_on, evidence_rev)
            if missing or touched:
                reason = describe_change(missing, touched, evidence_rev)
                if graph.query("""
                    MATCH (r:Entity {id: $rid}) WHERE r.status = 'PENDING_L3'
                    SET r.status = 'STALE', r.stale_reason = $reason, r.stale_at = $now
                    RETURN r.id
                """, {"rid": request_id, "reason": reason, "now": now}).result_set:
                    raise ValueError(f"{request_id} is now STALE: {reason}. Re-investigate before freezing.")
                continue
        # The revision is filtered as a projected value: FalkorDB 4.22 moves `WITH meta WHERE <test on meta>`
        # inside the OPTIONAL MATCH, which then keeps the row with meta = NULL instead of dropping it.
        signed = graph.query("""
            OPTIONAL MATCH (meta:Meta {key: 'dataset'})
            WITH coalesce(meta.rev, 0) AS at WHERE at = $rev
            MATCH (r:Entity {id: $rid})-[:UNDER_POLICY]->(p:Entity)
            MATCH (e:Entity {id: $approver})
            WHERE r:ApprovalRequest AND r.status = 'PENDING_L3' AND e:Employee
              AND coalesce(e.clearance_level, 0) >= coalesce(p.required_clearance_level_for_freeze, 0)
            SET r.status = 'APPROVED', r.approved_by = $approver, r.approved_at = $now, r.approved_rev = $rev
            CREATE (e)-[:SIGNED_OFF {at: $now}]->(r)
            WITH r
            OPTIONAL MATCH (r)-[:TARGETS]->(t:Account)
            FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END |
                SET t.status = 'FROZEN', t.frozen_at = $now, t.frozen_by = $rid)
            RETURN r.id, collect(t.id)
        """, {"rid": request_id, "approver": approver_id, "now": now, "rev": rev}).result_set
        if signed:
            spared = graph.ro_query("MATCH (r:Entity {id: $rid})-[:EXCLUDES]->(x) RETURN collect(x.id)",
                                    {"rid": request_id}).result_set[0][0]
            return {"request_id": request_id, "status": "APPROVED", "approved_by": approver_id, "approved_at": now,
                    "frozen": sorted(signed[0][1]), "spared": sorted(spared)}
    raise ValueError(f"{request_id} was not signed off: the graph kept changing while its evidence was checked. "
                     f"Try again.")


def approval_statuses(graph: Graph, request_ids: list[str]) -> dict[str, str]:
    rows = graph.ro_query("""
        UNWIND $ids AS rid
        MATCH (r:Entity {id: rid}) WHERE r:ApprovalRequest
        RETURN r.id, r.status
    """, {"ids": list(request_ids)}).result_set
    return dict(rows)


def list_approvals(graph: Graph, status: str | None = None, limit: int = 50) -> list[dict]:
    rows = graph.ro_query("""
        MATCH (r:ApprovalRequest)
        WHERE $status IS NULL OR r.status = $status
        OPTIONAL MATCH (r)-[:ROUTED_TO]->(e:Employee)
        OPTIONAL MATCH (r)-[:UNDER_POLICY]->(p:PolicyDoc)
        OPTIONAL MATCH (r)-[:FOR_CASE]->(c:Case)
        RETURN r {.*}, e {.id, .name, .title, .clearance}, p {.id, .title}, c {.id, .status, .subject_id}
        ORDER BY r.created_at DESC LIMIT $limit
    """, {"status": status, "limit": limit}).result_set
    return [{**dict(r), "approver": dict(e) if e else None, "policy": dict(p) if p else None,
             "case": dict(c) if c else None} for r, e, p, c in rows]
