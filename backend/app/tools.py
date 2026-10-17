"""The 11 FinTrace agent tools: validated arguments in, one JSON envelope out.

Every tool returns:
- `data`: what the model reads (amounts in dollars, times in UTC),
- `evidence`: every entity and edge the result shows; guardrails.py checks the model's claims against it,
- `meta`: latency, a row count and the parameterized Cypher that ran, for the trace inspector.

Bad arguments and requests the graph can't satisfy come back as `ok: false` with an `error` the
model can act on, instead of raising.
"""

from __future__ import annotations

import re
import textwrap
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from falkordb import Graph

from backend.app import memory
from backend.app import queries as q

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DAY = 86400
MAX_DEPTH = 6
MAX_IDS = 50
MAX_COMMUNITY = 200
NEIGHBOR_SAMPLE = 40
MEMBER_SAMPLE = 60
IDENTITY_LABELS = {"Device", "IP"}


class ToolError(ValueError):
    """Bad arguments or a request the graph can't satisfy; the message goes back to the model."""


@dataclass
class ToolContext:
    graph: Graph
    worker_id: str = "analyst"


class RecordingGraph:
    """Wraps a FalkorDB graph and records every query run through it."""

    def __init__(self, graph: Graph):
        self._graph = graph
        self.calls: list[dict] = []

    def query(self, query: str, params: dict | None = None, timeout: int | None = None):
        return self._run(self._graph.query, query, params, timeout)

    def ro_query(self, query: str, params: dict | None = None, timeout: int | None = None):
        return self._run(self._graph.ro_query, query, params, timeout)

    def _run(self, fn: Callable, query: str, params: dict | None, timeout: int | None):
        started = time.perf_counter()
        result = fn(query, params, timeout=timeout) if timeout else fn(query, params)
        self.calls.append({"cypher": textwrap.dedent(query).strip(), "params": sorted(params or {}),
                           "ms": round((time.perf_counter() - started) * 1000, 1)})
        return result

    def summary(self) -> list[dict]:
        grouped: dict[str, dict] = {}
        for call in self.calls:
            entry = grouped.setdefault(call["cypher"], {**call, "calls": 0, "ms": 0.0})
            entry["calls"] += 1
            entry["ms"] = round(entry["ms"] + call["ms"], 1)
        return list(grouped.values())


class Evidence:
    """Entities and directed edges a tool result shows."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: set[tuple[str, str, str]] = set()

    def node(self, info: dict | None, labels: list[str] | None = None, name: str | None = None) -> None:
        if not info or not info.get("id"):
            return
        entry = self.nodes.setdefault(info["id"], {"labels": [], "name": None})
        entry["labels"] = entry["labels"] or list(info.get("labels") or labels or [])
        entry["name"] = entry["name"] or info.get("name") or name

    def edge(self, src: str | None, rel: str | None, dst: str | None) -> None:
        if src and rel and dst:
            self.edges.add((src, rel, dst))

    def path(self, rendered: dict) -> None:
        for info in rendered.get("entities", []):
            self.node(info)
        for step in rendered["steps"]:
            if step.get("forward", True):
                self.edge(step["from"], step.get("rel"), step["to"])
            else:
                self.edge(step["to"], step.get("rel"), step["from"])

    def to_json(self) -> dict:
        return {"nodes": self.nodes, "edges": [list(e) for e in sorted(self.edges)]}


TOOLS: dict[str, Callable[..., dict]] = {}


def tool(fn: Callable) -> Callable[..., dict]:
    name = fn.__name__

    def run(ctx: ToolContext, **kwargs: Any) -> dict:
        started = time.perf_counter()
        graph = RecordingGraph(ctx.graph)
        evidence = Evidence()
        args = {k: v for k, v in kwargs.items() if v is not None}
        data, rows, stop, error = None, 0, None, None
        try:
            data, rows, stop = fn(ctx, graph, evidence, **kwargs)
        except ToolError as exc:
            error = str(exc)
        except Exception as exc:  # FalkorDB errors and timeouts go back to the caller as data
            error = f"{type(exc).__name__}: {exc}"
        return {
            "tool": name,
            "ok": error is None,
            "args": args,
            "data": data,
            "evidence": evidence.to_json(),
            "error": error,
            "stop": stop,
            "meta": {"elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "rows": rows,
                     "queries": graph.summary()},
        }

    run.__name__ = name
    run.__doc__ = fn.__doc__
    TOOLS[name] = run
    return run


def run_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    if name not in TOOLS:
        raise KeyError(name)
    return TOOLS[name](ctx, **args)


def _id(value: Any, name: str = "entity_id") -> str:
    value = str(value or "").strip()
    if not ID_RE.match(value):
        raise ToolError(f"{name} {value!r} is not a valid entity ID")
    return value


def _ids(values: Any, name: str, limit: int = MAX_IDS, required: bool = True) -> list[str]:
    if values is None:
        values = []
    if isinstance(values, str):
        values = [v for v in re.split(r"[,\s]+", values) if v]
    ids = list(dict.fromkeys(_id(v, name) for v in values))
    if required and not ids:
        raise ToolError(f"{name} needs at least one ID")
    if len(ids) > limit:
        raise ToolError(f"{name} accepts at most {limit} IDs")
    return ids


def _int(value: Any, name: str, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer") from None
    if not lo <= number <= hi:
        raise ToolError(f"{name} must be between {lo} and {hi}")
    return number


def _utc(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _require(graph: RecordingGraph, ids: list[str]) -> dict[str, dict]:
    known = q.describe_entities(graph, ids)
    if missing := [i for i in ids if i not in known]:
        raise ToolError(f"not in the graph: {missing}")
    return known


def _compact_path(rendered: dict) -> dict:
    return {"nodes": rendered["nodes"], "steps": rendered["steps"]}


def _identity_hops(rendered: dict) -> list[dict]:
    """Devices or IPs on a path, with the accounts on either side (identity links carry no money)."""
    entities = rendered.get("entities", [])
    hops = []
    for i, info in enumerate(entities):
        if IDENTITY_LABELS & set(info["labels"]):
            side = [entities[j]["id"] for j in (i - 1, i + 1) if 0 <= j < len(entities)]
            hops.append({"id": info["id"], "type": info["labels"][0], "accounts_on_path": side})
    return hops


def _transfer(step: dict) -> dict:
    return {"from": step["from"], "to": step["to"], "tx_id": step["tx_id"], "amount_usd": step["amount"] / 100,
            "time": _utc(step["ts"])}


@tool
def resolve_entity(ctx: ToolContext, g: RecordingGraph, ev: Evidence, entity_id: str):
    """Look up an entity by ID."""
    entity_id = _id(entity_id)
    result = q.resolve_entity(g, entity_id)
    if result is None:
        return {"entity_id": entity_id, "found": False}, 0, "unknown_entity"
    ev.node(result)
    return {"found": True, **result}, 1, None


@tool
def recall_memory(ctx: ToolContext, g: RecordingGraph, ev: Evidence, entity_id: str, facts: bool = True):
    """Remembered facts (CURRENT, or STALE with the reason), recent episodes, approval requests and playbooks."""
    entity_id = _id(entity_id)
    result = memory.recall(g, entity_id, facts=facts)
    if result is None:
        raise ToolError(f"{entity_id} is not in the graph")
    nodes = {d for fact in result["facts"] for d in fact["depends_on"] if "|" not in d} | {entity_id}
    nodes |= {t for request in result["approvals"] for t in request["target_ids"]}
    for info in q.describe_entities(g, sorted(nodes)).values():
        ev.node(info)
    for fact in result["facts"]:
        if fact["status"] == "CURRENT" and "path" in fact["scope"]:
            for step in fact["scope"]["path"]["steps"]:
                forward = step.get("forward", True)
                ev.edge(step["from"] if forward else step["to"], step["rel"], step["to"] if forward else step["from"])
    for request in result["approvals"]:
        if request["approver"]:
            ev.node({**request["approver"], "labels": ["Employee"]})
    rows = len(result["facts"]) + len(result["episodes"]) + len(result["playbooks"]) + len(result["approvals"])
    return result, rows, None


@tool
def explore_neighborhood(ctx: ToolContext, g: RecordingGraph, ev: Evidence, entity_id: str, depth: int = 2):
    """Counts per depth of everything connected, plus flagged or sanctioned entities and a path to each."""
    entity_id = _id(entity_id)
    depth = _int(depth, "depth", 1, MAX_DEPTH)
    result = q.explore_neighborhood(g, entity_id, depth, neighbor_limit=NEIGHBOR_SAMPLE if depth == 1 else 0)
    if result is None:
        raise ToolError(f"{entity_id} is not in the graph")
    for info in q.describe_entities(g, [entity_id]).values():
        ev.node(info)
    flagged = []
    for hit in result["flagged"]:
        ev.path(hit["path"])
        ev.node(hit["entity"])
        flagged.append({"entity": hit["entity"], "depth": hit["depth"], "path": _compact_path(hit["path"]),
                        "identity_hops": _identity_hops(hit["path"])})
    neighbors = []
    for nb in result.get("neighbors", []):
        info = nb["entity"]
        ev.node(info)
        if nb.get("forward", True):
            ev.edge(entity_id, nb.get("rel"), info["id"])
        else:
            ev.edge(info["id"], nb.get("rel"), entity_id)
        neighbors.append({"id": info["id"], "name": info["name"], "labels": info["labels"], "rel": nb.get("rel"),
                          "direction": "out" if nb.get("forward", True) else "in"})
    eligible = result["eligible"]
    routes = []
    for route in eligible["routes"]:
        ev.path(route["path"])
        ev.node(route["entity"])
        routes.append({"entity": route["entity"], "hops": route["hops"], "path": _compact_path(route["path"]),
                       "identity_hops": _identity_hops(route["path"])})
    explored, searched = result["scope"], eligible["scope"]
    data = {
        "entity_id": entity_id,
        "depth": depth,
        "levels": result["levels"],
        "total_reached": result["total_reached"],
        "flagged": flagged,
        "scope": explored,
        "eligible_routes": {"rule": eligible["rule"], "scope": searched, "routes": routes},
        "coverage": (f"Exploration: {explored['reached']:,} entities, {explored['completed_depth']} of {depth} "
                     f"hop(s){', everything reachable' if explored['exhausted'] else ''}. Eligible-route search: "
                     f"{searched['reached']:,} entities, {searched['completed_depth']} of {depth} hop(s), "
                     + ("complete" if searched["complete"] else "INCOMPLETE ("
                        + ("node budget hit" if searched["truncated"] else "timed out") + ")")
                     + f"; {len(routes)} route(s) to a sanctioned account."),
    }
    if neighbors:
        data["neighbors"] = neighbors
        data["neighbors_shown"] = f"{len(neighbors)} of {result['levels'][0]['reached']}"
    return data, result["total_reached"], None


@tool
def trace_money_flow(ctx: ToolContext, g: RecordingGraph, ev: Evidence, entity_id: str, direction: str = "out",
                     max_hops: int = 4, window_days: int | None = None):
    """Time-ordered money flow downstream ("out") or upstream ("in") over individual transfers."""
    entity_id = _id(entity_id)
    direction = str(direction).lower()
    if direction not in ("out", "in"):
        raise ToolError("direction must be 'out' or 'in'")
    max_hops = _int(max_hops, "max_hops", 1, MAX_DEPTH)
    since, until = 0, q.FAR_FUTURE
    if window_days is not None:
        window_days = _int(window_days, "window_days", 1, 3650)
        as_of = q.dataset_meta(g).get("as_of_ts") or int(time.time())
        since, until = as_of - window_days * DAY, as_of
    result = q.trace_money_flow(g, entity_id, direction, max_hops, since_ts=since, until_ts=until)
    if result is None:
        raise ToolError(f"{entity_id} is not in the graph")

    trail_ids = {entity_id}
    for hit in result["sanctioned"]:
        for step in hit["trail"]:
            trail_ids.update((step["from"], step["to"]))
            ev.edge(step["from"], "TRANSFERRED", step["to"])
    for hop in result["hops"]:
        for item in hop["largest"]:
            t = item["transfer"]
            trail_ids.update((t["from"], t["to"]))
            ev.edge(t["from"], "TRANSFERRED", t["to"])
    for rt in result["round_trips"]:
        trail_ids.add(rt["from"])
        ev.edge(*((rt["from"], "TRANSFERRED", entity_id) if direction == "out" else (entity_id, "TRANSFERRED", rt["from"])))
    for info in q.describe_entities(g, sorted(trail_ids)).values():
        ev.node(info)

    data = {
        "entity_id": entity_id,
        "direction": direction,
        "window": None if window_days is None else {"days": window_days, "from": _utc(since), "to": _utc(until)},
        "accounts_reached": result["accounts_reached"],
        "hops": [{"hop": h["hop"], "accounts": h["accounts"], "amount_usd": h["amount"] / 100,
                  "largest": [{"to" if direction == "out" else "from": i["entity"]["id"],
                               "name": i["entity"]["name"], "transfer": _transfer(i["transfer"])}
                              for i in h["largest"]]}
                 for h in result["hops"]],
        "sanctioned": [{"entity": hit["entity"], "trail": [_transfer(s) for s in hit["trail"]]}
                       for hit in result["sanctioned"]],
        "round_trips": [{"hop": rt["hop"], "via": rt["from"], "tx_id": rt["tx_id"], "amount_usd": rt["amount"] / 100,
                         "time": _utc(rt["ts"])} for rt in result["round_trips"]],
        "capped_hops": result["capped_hops"],
    }
    if result["capped_hops"]:
        data["note"] = ("Some hops kept only the largest arrivals (capped_hops), so accounts beyond them may be "
                        "missing: absence of a sanctioned account here is not a complete negative.")
    return data, result["accounts_reached"], None


@tool
def path_between(ctx: ToolContext, g: RecordingGraph, ev: Evidence, from_id: str, to_id: str):
    """Shortest connection between two entities across money and shared devices/IPs, in either direction."""
    from_id, to_id = _id(from_id, "from_id"), _id(to_id, "to_id")
    for info in _require(g, [from_id, to_id]).values():
        ev.node(info)
    result = q.path_between(g, from_id, to_id)
    if result is None:
        return {"from_id": from_id, "to_id": to_id, "connected": False}, 0, None
    ev.path(result["path"])
    data = {"from_id": from_id, "to_id": to_id, "connected": True, "hops": result["hops"],
            "path": _compact_path(result["path"]), "identity_hops": _identity_hops(result["path"])}
    return data, len(result["path"]["nodes"]), None


@tool
def detect_ring(ctx: ToolContext, g: RecordingGraph, ev: Evidence, entity_id: str, runs: int = 1):
    """Community (label propagation) containing a risk-layer entity."""
    entity_id = _id(entity_id)
    runs = _int(runs, "runs", 1, 5)
    _require(g, [entity_id])
    result = q.detect_ring(g, entity_id, runs=runs, member_limit=MAX_COMMUNITY)
    if result is None:
        raise ToolError(f"{entity_id} is not an account, device or IP, so it has no community")
    shown = result["members"][:MEMBER_SAMPLE]
    info = q.describe_entities(g, shown)
    for member in info.values():
        ev.node(member)
    size = result["community_sizes"][-1]
    data = {"entity_id": entity_id, "community_size": size, "runs": runs,
            "members": [info[m] for m in shown if m in info],
            "members_shown": f"{len(shown)} of {size}"}
    return data, size, None


@tool
def find_brokers(ctx: ToolContext, g: RecordingGraph, ev: Evidence, entity_id: str,
                 community_ids: list[str] | None = None):
    """Rank members of a ring by betweenness (who the money has to pass through)."""
    entity_id = _id(entity_id)
    members = _ids(community_ids, "community_ids", limit=MAX_COMMUNITY, required=False)
    scope = "community_ids"
    if not members:
        ring = q.detect_ring(g, entity_id, member_limit=MAX_COMMUNITY)
        if ring is None:
            raise ToolError(f"{entity_id} has no community; pass community_ids")
        members, scope = ring["members"], "detected community"
    members = list(dict.fromkeys([entity_id, *members]))
    result = q.find_brokers(g, members)
    info = q.describe_entities(g, [s["id"] for s in result["scores"]])
    for member in info.values():
        ev.node(member)
    ranked = [{**info[s["id"]], "score": round(s["score"], 3)} for s in result["scores"] if s["id"] in info]
    data = {"entity_id": entity_id, "scope": scope, "scored": len(ranked), "brokers": ranked[:10],
            "note": "betweenness follows edge direction: a high score means money between other members passes through it"}
    return data, len(ranked), None


@tool
def exposure_flow(ctx: ToolContext, g: RecordingGraph, ev: Evidence, source_ids: list[str], target_id: str,
                  include_shared_device_accounts: bool = True):
    """Maximum dollars that could move from the source accounts to a target (multi-source max flow)."""
    sources = _ids(source_ids, "source_ids")
    target_id = _id(target_id, "target_id")
    for info in _require(g, [*sources, target_id]).values():
        ev.node(info)
    added: dict[str, dict] = {}
    if include_shared_device_accounts:
        added = q.accounts_sharing_devices(g, sources)
        for device, link in added.items():
            ev.node({"id": device, "labels": ["Device"]})
            for account in link["used_by"] + link["also_used_by"]:
                ev.edge(account, "USED_DEVICE", device)
            sources += [a for a in link["also_used_by"] if a not in sources]
    result = q.exposure_flow(g, sources, target_id)
    if result is None:
        raise ToolError("none of the sources is an account")
    flow_nodes = {e["from"] for e in result["edges"]} | {e["to"] for e in result["edges"]} | set(sources)
    for info in q.describe_entities(g, sorted(flow_nodes)).values():
        ev.node(info)
    for e in result["edges"]:
        ev.edge(e["from"], "FLOWS_TO", e["to"])
    data = {
        "source_ids": sources,
        "added_via_shared_device": {d: link["also_used_by"] for d, link in added.items()},
        "target_id": target_id,
        "max_flow_usd": result["max_flow_dollars"],
        "edges": [{"from": e["from"], "to": e["to"], "usd": e["dollars"]} for e in result["edges"]],
        "note": "upper bound over aggregated transfers; ignores timing",
    }
    return data, len(result["edges"]), None


@tool
def trace_governance(ctx: ToolContext, g: RecordingGraph, ev: Evidence, entity_id: str):
    """Decisions applying to an entity, their approvers, sources and policy, and the on-call approvers for a freeze."""
    entity_id = _id(entity_id)
    result = q.trace_governance(g, entity_id)
    if result is None:
        raise ToolError(f"{entity_id} is not in the graph")
    for info in q.describe_entities(g, [entity_id]).values():
        ev.node(info)

    def employee(e: dict | None) -> None:
        if e:
            ev.node(e, labels=["Employee"])

    for d in result["decisions"]:
        ev.node(d, labels=["Decision"], name=d.get("summary"))
        ev.edge(d["id"], "APPLIES_TO", entity_id)
        if approver := d["approved_by"]:
            employee(approver)
            ev.edge(approver["id"], "APPROVED", d["id"])
            if team := d["team"]:
                ev.node(team, labels=["Team"])
                ev.edge(approver["id"], "MEMBER_OF", team["id"])
        if policy := d["policy"]:
            ev.node(policy, labels=["PolicyDoc"], name=policy.get("title"))
            ev.edge(d["id"], "GOVERNED_BY", policy["id"])
        for src in d["sources"]:
            ev.node(src, labels=[src["type"]], name=src.get("title") or (src.get("text") or "")[:60])
            ev.edge(d["id"], "SOURCED_FROM", src["id"])
            if by := src.get("by"):
                employee(by)
                ev.edge(by["id"], by["relation"], src["id"])
    for other in result["approver_other_decisions"]:
        ev.node({"id": other["decision_id"]}, labels=["Decision"], name=other.get("control"))
        ev.node(other["applies_to"])
        ev.edge(other["approved_by"], "APPROVED", other["decision_id"])
        ev.edge(other["decision_id"], "APPLIES_TO", other["applies_to"]["id"])
    if policy := result["freeze_policy"]:
        ev.node(policy, labels=["PolicyDoc"], name=policy.get("title"))
    for person in result["on_call_approvers"]:
        employee(person)
        if person["team"]["id"]:
            ev.node(person["team"], labels=["Team"])
            ev.edge(person["id"], "MEMBER_OF", person["team"]["id"])
    rows = len(result["decisions"]) + len(result["approver_other_decisions"]) + len(result["on_call_approvers"])
    return result, rows, None


@tool
def freeze_impact(ctx: ToolContext, g: RecordingGraph, ev: Evidence, target_ids: list[str]):
    """Legitimate customers who share a device or IP with accounts you plan to freeze."""
    targets = _ids(target_ids, "target_ids")
    result = q.freeze_impact(g, targets)
    outsiders = sorted({o["id"] for inf in result["infrastructure"] for o in inf["outside_accounts"]})
    for info in [*result["targets"], *q.describe_entities(g, outsiders).values()]:
        ev.node(info)
    for inf in result["infrastructure"]:
        ev.node(inf, labels=[inf["type"]], name=inf.get("kind"))
        for user in inf["used_by_targets"] + inf["outside_accounts"]:
            ev.edge(user["id"], user["rel"], inf["id"])
    blocked = [inf["id"] for inf in result["infrastructure"] if inf["safe_to_block"]]
    keep_open = [inf["id"] for inf in result["infrastructure"] if not inf["safe_to_block"]]
    advice = ["Freeze the target accounts individually."]
    if keep_open:
        shared_with = sorted({c["id"] for c in result["collateral"]})
        advice.append(f"Do not block {', '.join(keep_open)}: shared with legitimate accounts {', '.join(shared_with)}.")
    if blocked:
        advice.append(f"Blocking {', '.join(blocked)} affects no legitimate customer.")
    if result["also_exposed"]:
        advice.append("Not customers to protect: " + "; ".join(
            f"{a['id']} has its own eligible route to {a['sanctioned']} in {a['route_hops']} hops"
            for a in result["also_exposed"]) + ". Add them to the freeze.")
    data = {**{k: v for k, v in result.items() if k != "elapsed_ms"}, "recommendation": " ".join(advice)}
    return data, len(result["collateral"]) + len(result["also_exposed"]) + len(result["infrastructure"]), None


@tool
def request_action(ctx: ToolContext, g: RecordingGraph, ev: Evidence, target_ids: list[str], policy_id: str,
                   approver_id: str, reason: str, case_id: str | None = None,
                   excluded_ids: list[str] | None = None, action_type: str = "FREEZE"):
    """Ask the on-call approver to sign off a freeze. Nothing is frozen until they approve."""
    targets = _ids(target_ids, "target_ids")
    excluded = _ids(excluded_ids, "excluded_ids", required=False)
    case_id = _id(case_id, "case_id") if case_id else f"case-{uuid.uuid4().hex[:8]}"
    policy_id, approver_id = _id(policy_id, "policy_id"), _id(approver_id, "approver_id")
    reason = str(reason or "").strip()[:1000]
    if not reason:
        raise ToolError("reason is required")
    try:
        result = q.request_action(g, case_id, targets, excluded, action_type, policy_id, approver_id, reason,
                                  requested_by=ctx.worker_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    rid = result["request_id"]
    ev.node({"id": rid, "labels": ["ApprovalRequest"], "name": f"{action_type.upper()} awaiting sign-off"})
    ev.node({"id": case_id, "labels": ["Case"], "name": case_id})
    ev.node({"id": policy_id, "labels": ["PolicyDoc"]})
    ev.node({**result["approver"], "labels": ["Employee"]})
    ev.edge(rid, "FOR_CASE", case_id)
    ev.edge(rid, "UNDER_POLICY", policy_id)
    ev.edge(rid, "ROUTED_TO", approver_id)
    for info in result["entities"].values():
        ev.node(info)
    for t in targets:
        ev.edge(rid, "TARGETS", t)
    for x in excluded:
        ev.edge(rid, "EXCLUDES", x)
    data = {k: v for k, v in result.items() if k != "entities"}
    data["note"] = "Nothing is frozen until the approver signs off in the approval inbox."
    return data, 1, None
