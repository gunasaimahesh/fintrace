"""Answer states, verdicts, typed facts and the analyst's card.

Every statement on the card is rendered from Facts, and every Fact comes from one tool result (tool,
step, result_key). The verdict follows the eligible-route rule, not the model:

- SUSPICIOUS: an eligible route to a sanctioned account was found (a positive witness stands even if
  other searches were incomplete).
- CLEAR: an eligible-route search completed to PRESCRIBED_DEPTH hops, or exhausted everything reachable,
  and found none. Worded as "no eligible route within the stated scope", never "safe".
- INSUFFICIENT: no such search completed (capped, timed out, too shallow, or never run).

The model may propose a verdict, structured statements and a rationale. Its statements are bound to
facts or rejected with a reason; its rationale is shown as unverified, with supplementary checks for IDs
no tool returned and edges no tool showed. Those checks reject; they don't verify.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from backend.app.contracts import PRESCRIBED_DEPTH, Fact, FactKind, Source, edge_key


class AnswerState(StrEnum):
    ANSWERED_WITH_PATH = "answered_with_path"
    ANSWERED_FROM_MEMORY = "answered_from_memory"
    MEMORY_STALE_REINVESTIGATED = "memory_stale_reinvestigated"
    NOT_IN_GRAPH = "not_in_graph"
    UNKNOWN_ENTITY = "unknown_entity"


class Verdict(StrEnum):
    SUSPICIOUS = "SUSPICIOUS"
    CLEAR = "CLEAR"
    INSUFFICIENT = "INSUFFICIENT"


CLAIM_RELATIONS = ("TRANSFERRED", "USED_DEVICE", "LOGGED_FROM", "APPROVED", "APPLIES_TO", "SOURCED_FROM",
                   "GOVERNED_BY", "MEMBER_OF", "AUTHORED", "OPENED", "ROUTED_TO", "UNDER_POLICY", "FOR_CASE",
                   "TARGETS", "EXCLUDES")
MONEY_RELATIONS = {"TRANSFERRED", "FLOWS_TO"}
RING_LIMIT = 25

MODEL_PREDICATES = {
    "eligible_route_hops": "subject: alert entity, object: sanctioned account, value: hops",
    "no_eligible_route": "subject: alert entity, value: hops a complete search covered without finding a route",
    "sanctioned_within_reach": "subject: alert entity, object: sanctioned account reached only by association, value: hops",
    "transfer_amount": "subject: sender, object: receiver, value: USD",
    "max_flow_capacity": "subject: a source account, object: target account, value: USD",
    "monthly_volume": "subject: account, value: USD per month",
    "policy_threshold": "subject: policy, value: USD per month",
    "ring_size": "subject: account, value: community members",
    "top_broker": "subject: account whose community was scored, object: broker account",
    "approved_by": "subject: decision, object: employee",
    "waiver_compliance": "subject: decision, object: policy, value: BREACH or COMPLIANT",
    "action_status": "subject: approval request, value: status",
}
FINDING_NAMES = {"eligible_route_hops": "route", "no_eligible_route": "finding of no route",
                 "sanctioned_within_reach": "association", "money_trail": "money trail",
                 "max_flow_capacity": "max-flow bound", "ring_size": "ring", "top_broker": "broker ranking",
                 "waiver_compliance": "waiver check"}
UNIT_FAMILY = {"eligible_route_hops": "hops", "no_eligible_route": "hops", "sanctioned_within_reach": "hops",
               "transfer_amount": "USD",
               "max_flow_capacity": "USD", "monthly_volume": "USD", "policy_threshold": "USD", "ring_size": "members"}

_ID_TOKEN = re.compile(
    r"(?<![\w-])(?:acct|merch|dev|ip|emp|dec|pol|tick|msg|team|case|apr|alert)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
)


def mentioned_ids(text: str) -> list[str]:
    """Entity IDs in free text. Tokens need a digit or two segments, so prose like "team-based" is ignored."""
    found = []
    for match in _ID_TOKEN.finditer(text or ""):
        token = match.group(0)
        rest = token.split("-", 1)[1]
        if any(c.isdigit() for c in rest) or "-" in rest:
            found.append(token)
    return list(dict.fromkeys(found))


@dataclass
class EvidenceLedger:
    """Every entity and edge returned by the tools during one run."""

    nodes: dict[str, dict] = field(default_factory=dict)
    edges: set[tuple[str, str, str]] = field(default_factory=set)

    def add(self, evidence: dict | None) -> None:
        if not evidence:
            return
        for eid, info in (evidence.get("nodes") or {}).items():
            entry = self.nodes.setdefault(eid, {"labels": [], "name": None})
            entry["labels"] = entry["labels"] or list(info.get("labels") or [])
            entry["name"] = entry["name"] or info.get("name")
        for src, rel, dst in evidence.get("edges") or []:
            self.edges.add((src, rel, dst))

    def has_edge(self, subject: str, relation: str, obj: str) -> bool:
        relations = MONEY_RELATIONS if relation in MONEY_RELATIONS else {relation}
        return any((subject, r, obj) in self.edges for r in relations)

    def labels(self, eid: str) -> list[str]:
        return self.nodes.get(eid, {}).get("labels") or []

    def name(self, eid: str) -> str | None:
        return self.nodes.get(eid, {}).get("name")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _money(usd: float | int | None) -> str:
    return f"${usd:,.0f}" if usd is not None else "an unknown amount"


def normalize_claim(raw: object) -> tuple[str, str, str] | None:
    if isinstance(raw, dict):
        parts = (raw.get("subject"), raw.get("relation"), raw.get("object"))
    elif isinstance(raw, (list, tuple)) and len(raw) == 3:
        parts = tuple(raw)
    else:
        return None
    if not all(isinstance(p, str) and p.strip() for p in parts):
        return None
    subject, relation, obj = (p.strip() for p in parts)
    return subject, relation.upper().replace(" ", "_"), obj


@dataclass
class TextCheck:
    """Supplementary rejection checks on model text: IDs no tool returned, edges no tool showed."""

    ids_checked: int
    unknown_ids: list[str]
    rejected_claims: list[dict]

    @property
    def passed(self) -> bool:
        return not self.unknown_ids and not self.rejected_claims

    def to_json(self) -> dict:
        return {**asdict(self), "passed": self.passed}


def check_text(text: str, ledger: EvidenceLedger, claims: list | None = None,
               given: list[str] | None = None) -> TextCheck:
    """`given` are IDs from the request itself (the alert entity), which may legitimately be absent from the graph."""
    normalized = [(raw, normalize_claim(raw)) for raw in claims or []]
    ids = mentioned_ids(text)
    for _, claim in normalized:
        if claim:
            ids += [claim[0], claim[2]]
    ids = list(dict.fromkeys(ids))
    unknown = [i for i in ids if i not in ledger.nodes and i not in (given or [])]
    rejected = []
    for raw, claim in normalized:
        if claim is None:
            rejected.append({"claim": raw, "reason": "malformed claim"})
        elif claim[1] not in CLAIM_RELATIONS and claim[1] != "FLOWS_TO":
            rejected.append({"claim": raw, "reason": f"unknown relation {claim[1]}"})
        elif not ledger.has_edge(*claim):
            rejected.append({"claim": raw, "reason": "no such edge in this run's tool results"})
    return TextCheck(ids_checked=len(ids), unknown_ids=unknown, rejected_claims=rejected)


def route_breaks(path: dict) -> list[str]:
    """Why money from the start of a path could not follow it to the end: a transfer pointing back toward
    the start, or an identity hop through a shared IP (proxies and carrier NAT are shared by strangers;
    a device fingerprint is not). An empty list means the path follows the eligible-route rule."""
    breaks = []
    start = path["nodes"][0] if path.get("nodes") else "the start"
    for step in path["steps"]:
        rel, forward = step.get("rel"), step.get("forward", True)
        if rel in MONEY_RELATIONS and not forward:
            breaks.append(f"{step['to']} paid {step['from']}, so that leg carries money toward {start}, not away from it")
        elif rel == "LOGGED_FROM":
            breaks.append(f"{step['to'] if forward else step['from']} is a shared IP, not a shared device")
    return list(dict.fromkeys(breaks))


def _path_dependencies(path: dict) -> list[str]:
    deps = list(path["nodes"])
    for step in path["steps"]:
        src, dst = (step["from"], step["to"]) if step.get("forward", True) else (step["to"], step["from"])
        deps.append(edge_key(src, step["rel"], dst))
    return deps


def extract_facts(results: list[dict], rev: int | None = None) -> list[Fact]:
    """Typed facts from successful tool results, numbered F1, F2... in the order the tools ran."""
    facts: list[Fact] = []

    def add(kind: FactKind, subject: str, predicate: str, obj: str | None, value: Any, unit: str | None,
            source: Source, scope: dict | None = None, depends_on: list[str] | None = None,
            aggregate: bool = False, evidence_rev: int | None = None) -> Fact:
        fact = Fact(id=f"F{len(facts) + 1}", kind=kind.value, subject=subject, predicate=predicate, object=obj,
                    value=value, unit=unit, source=source, scope=scope or {},
                    evidence_rev=rev if evidence_rev is None else evidence_rev,
                    depends_on=list(dict.fromkeys(depends_on or [subject])), aggregate=aggregate)
        facts.append(fact)
        return fact

    for step, r in enumerate(results, start=1):
        if not r["ok"]:
            continue
        tool, d = r["tool"], r["data"]

        def src(key: str) -> Source:
            return Source(tool=tool, step=step, result_key=key)

        if tool == "resolve_entity":
            if not d.get("found"):
                add(FactKind.PROFILE, d["entity_id"], "not_in_graph", None, True, None, src("data.found"))
                continue
            add(FactKind.PROFILE, d["id"], "status", None, d.get("status"), None, src("data.status"))
            if (volume := d["profile"].get("monthly_volume_usd")) is not None:
                add(FactKind.PROFILE, d["id"], "monthly_volume", None, volume, "USD/month",
                    src("data.profile.monthly_volume_usd"))

        elif tool == "recall_memory":
            for i, mf in enumerate(d["facts"]):
                if mf["status"] == "CURRENT":
                    memo = {"fact_id": mf["id"], "episode_id": mf["episode_id"], "valid_rev": mf["valid_rev"]}
                    add(FactKind(mf["kind"]), mf["subject"], mf["predicate"], mf["object"], mf["value"], mf["unit"],
                        src(f"data.facts[{i}]"), scope={**mf["scope"], "memory": memo},
                        depends_on=mf["depends_on"], aggregate=mf["aggregate"], evidence_rev=mf["valid_rev"])
                else:
                    add(FactKind(mf["kind"]), d["entity_id"], "memory_stale", mf["id"], mf["predicate"], None,
                        src(f"data.facts[{i}]"), evidence_rev=mf["valid_rev"],
                        scope={"claim": mf["claim"], "why": mf["why_stale"], "rerun": mf["rerun"],
                               "episode_id": mf["episode_id"], "valid_rev": mf["valid_rev"]})
            for i, request in enumerate(d["approvals"]):
                if request["approver"]:
                    add(FactKind.ACTION, request["request_id"], "action_status", request["approver"]["id"],
                        request["status"], None, src(f"data.approvals[{i}]"), scope=_action_scope(request))

        elif tool == "explore_neighborhood":
            subject, eligible = d["entity_id"], d["eligible_routes"]
            scope = eligible["scope"]
            for i, route in enumerate(eligible["routes"]):
                add(FactKind.SANCTIONS_EXPOSURE, subject, "eligible_route_hops", route["entity"]["id"], route["hops"],
                    "hops", src(f"data.eligible_routes.routes[{i}]"),
                    scope={"path": route["path"], "identity_hops": route["identity_hops"], "search": scope},
                    depends_on=_path_dependencies(route["path"]))
            if not eligible["routes"]:
                predicate = "no_eligible_route" if scope["complete"] else "eligible_search_incomplete"
                add(FactKind.SANCTIONS_EXPOSURE, subject, predicate, None, scope["completed_depth"], "hops",
                    src("data.eligible_routes.scope"), scope=scope, aggregate=True)
            for i, hit in enumerate(d["flagged"]):
                sanctioned = "Sanctioned" in hit["entity"]["labels"]
                add(FactKind.SANCTIONS_EXPOSURE, subject,
                    "sanctioned_within_reach" if sanctioned else "risk_flag_within_reach",
                    hit["entity"]["id"], hit["depth"], "hops", src(f"data.flagged[{i}]"),
                    scope={"path": hit["path"], "breaks": route_breaks(hit["path"])},
                    depends_on=_path_dependencies(hit["path"]))

        elif tool == "trace_money_flow":
            for i, hit in enumerate(d["sanctioned"]):
                legs = [{k: leg[k] for k in ("from", "to", "amount_usd", "time", "tx_id")} for leg in hit["trail"]]
                trail = add(FactKind.SANCTIONS_EXPOSURE, d["entity_id"], "money_trail", hit["entity"]["id"],
                            len(hit["trail"]), "transfers", src(f"data.sanctioned[{i}]"),
                            scope={"direction": d["direction"], "legs": legs},
                            depends_on=[n for leg in hit["trail"] for n in (leg["from"], leg["to"])])
                for j, leg in enumerate(hit["trail"]):
                    add(FactKind.SANCTIONS_EXPOSURE, leg["from"], "transfer_amount", leg["to"], leg["amount_usd"],
                        "USD", src(f"data.sanctioned[{i}].trail[{j}]"),
                        scope={"tx_id": leg["tx_id"], "time": leg["time"], "trail": trail.id},
                        depends_on=[leg["from"], leg["to"], edge_key(leg["from"], "TRANSFERRED", leg["to"])])

        elif tool == "exposure_flow":
            sources = d["source_ids"]
            add(FactKind.SANCTIONS_EXPOSURE, sources[0] if len(sources) == 1 else "+".join(sources),
                "max_flow_capacity", d["target_id"], d["max_flow_usd"], "USD", src("data.max_flow_usd"),
                scope={"sources": sources, "added_via_shared_device": d["added_via_shared_device"]},
                depends_on=[*sources, d["target_id"]], aggregate=True)

        elif tool == "detect_ring":
            size = d["community_size"]
            members = [m["id"] for m in d["members"]] if size <= RING_LIMIT else None
            add(FactKind.SANCTIONS_EXPOSURE, d["entity_id"], "ring_size", None, size, "members",
                src("data.community_size"), scope={"members": members, "runs": d["runs"]}, aggregate=True)

        elif tool == "find_brokers":
            ranked = [b for b in d["brokers"] if b["score"] > 0]
            if ranked:
                add(FactKind.SANCTIONS_EXPOSURE, d["entity_id"], "top_broker", ranked[0]["id"], ranked[0]["score"],
                    "betweenness", src("data.brokers[0]"), scope={"scored": d["scored"]},
                    depends_on=[d["entity_id"], ranked[0]["id"]], aggregate=True)

        elif tool == "trace_governance":
            entity = d["entity_id"]
            for i, dec in enumerate(d["decisions"]):
                key = f"data.decisions[{i}]"
                approver, policy = dec["approved_by"], dec["policy"]
                deps = [dec["id"], entity] + ([approver["id"]] if approver else []) + ([policy["id"]] if policy else [])
                add(FactKind.WAIVER_COMPLIANCE, dec["id"], "waiver_applies_to", entity, dec.get("control"), None,
                    src(key), scope={"sources": [s["id"] for s in dec["sources"]]}, depends_on=[dec["id"], entity])
                if approver:
                    add(FactKind.WAIVER_COMPLIANCE, dec["id"], "approved_by", approver["id"], approver.get("clearance"),
                        None, src(f"{key}.approved_by"), depends_on=[dec["id"], approver["id"]])
                if dec.get("merchant_monthly_volume_usd") is not None:
                    add(FactKind.WAIVER_COMPLIANCE, entity, "monthly_volume", None, dec["merchant_monthly_volume_usd"],
                        "USD/month", src(f"{key}.merchant_monthly_volume_usd"), scope={"recorded_on": dec["id"]},
                        depends_on=[dec["id"]])
                if policy:
                    if policy.get("waiver_threshold_monthly_usd") is not None:
                        add(FactKind.WAIVER_COMPLIANCE, policy["id"], "policy_threshold", None,
                            policy["waiver_threshold_monthly_usd"], "USD/month",
                            src(f"{key}.policy.waiver_threshold_monthly_usd"),
                            scope={"version": policy.get("version"), "required_clearance": policy.get("required_clearance")})
                    add(FactKind.WAIVER_COMPLIANCE, dec["id"], "waiver_compliance", policy["id"],
                        "BREACH" if dec["violations"] else "COMPLIANT", None, src(f"{key}.violations"),
                        scope={"details": [v["detail"] for v in dec["violations"]],
                               "policy_version": policy.get("version"), "control": dec.get("control"),
                               "applies_to": entity, "approved_by": approver["id"] if approver else None,
                               "approver_clearance": approver.get("clearance") if approver else None,
                               "sources": [s["id"] for s in dec["sources"]]},
                        depends_on=deps)
            for i, other in enumerate(d["approver_other_decisions"]):
                add(FactKind.WAIVER_COMPLIANCE, other["decision_id"], "waiver_compliance", other["policy_id"],
                    "BREACH" if other["violations"] else "COMPLIANT", None, src(f"data.approver_other_decisions[{i}]"),
                    scope={"approved_by": other["approved_by"], "applies_to": other["applies_to"]["id"], "other": True},
                    depends_on=[other["decision_id"], other["approved_by"], other["applies_to"]["id"]])
            if policy := d["freeze_policy"]:
                for i, person in enumerate(d["on_call_approvers"]):
                    add(FactKind.ACTION, policy["id"], "on_call_approver", person["id"], person["clearance"], None,
                        src(f"data.on_call_approvers[{i}]"), depends_on=[policy["id"], person["id"]])

        elif tool == "freeze_impact":
            for i, c in enumerate(d["collateral"]):
                add(FactKind.ACTION, c["id"], "collateral", "+".join(c["shared_via"]), c["tier"], None,
                    src(f"data.collateral[{i}]"),
                    scope={"shared_via": c["shared_via"], "monthly_volume_usd": c["monthly_volume_usd"]},
                    depends_on=[c["id"], *c["shared_via"]])
            for i, a in enumerate(d.get("also_exposed", [])):
                add(FactKind.ACTION, a["id"], "also_exposed", a["sanctioned"], a["route_hops"], "hops",
                    src(f"data.also_exposed[{i}]"), scope={"shared_via": a["shared_via"]},
                    depends_on=[a["id"], *a["shared_via"]])

        elif tool == "request_action":
            add(FactKind.ACTION, d["request_id"], "action_status", d["approver"]["id"], d["status"], None,
                src("data.status"), scope=_action_scope(d))

        elif tool == "path_between" and d.get("connected"):
            add(FactKind.SANCTIONS_EXPOSURE, d["from_id"], "shortest_connection_hops", d["to_id"], d["hops"], "hops",
                src("data.hops"), scope={"path": d["path"], "breaks": route_breaks(d["path"])},
                depends_on=_path_dependencies(d["path"]))
    return facts


def _action_scope(request: dict) -> dict:
    return {"targets": request["target_ids"], "excluded": request["excluded_ids"], "policy_id": request["policy_id"],
            "case_id": request["case_id"], "approver_clearance": request["approver"].get("clearance")}


def status_read_facts(facts: list[Fact], statuses: dict[str, str], rev: int | None = None) -> list[Fact]:
    """Approval status re-read from the graph when the card is rendered (statuses never come from memory)."""
    fresh = []
    for f in facts:
        if f.predicate == "action_status" and f.subject in statuses:
            fresh.append(Fact(id=f"F{len(facts) + len(fresh) + 1}", kind=f.kind, subject=f.subject,
                              predicate="action_status", object=f.object, value=statuses[f.subject], unit=None,
                              source=Source(tool="approval_status_read", step=0,
                                            result_key=f"ApprovalRequest[{f.subject}].status"),
                              scope=f.scope, evidence_rev=rev, depends_on=[f.subject]))
    return fresh


def _by(facts: list[Fact], predicate: str) -> list[Fact]:
    return [f for f in facts if f.predicate == predicate]


def _first_per(facts: list[Fact], key=lambda f: (f.subject, f.object)) -> list[Fact]:
    seen, out = set(), []
    for f in facts:
        if key(f) not in seen:
            seen.add(key(f))
            out.append(f)
    return out


def search_clears(scope: dict) -> bool:
    return bool(scope["complete"] and (scope["exhausted"] or scope["requested_depth"] >= PRESCRIBED_DEPTH))


def clears(fact: Fact) -> bool:
    return fact.predicate == "no_eligible_route" and search_clears(fact.scope)


def _remembered(fact: Fact) -> bool:
    return bool(fact.scope.get("memory"))


def derive(facts: list[Fact], unknown: bool) -> tuple[AnswerState, Verdict, list[str]]:
    """Answer state, verdict and the facts the verdict rests on. Remembered CURRENT facts count like fresh ones;
    a fresh fact is preferred when both support the verdict."""
    if unknown:
        return AnswerState.UNKNOWN_ENTITY, Verdict.INSUFFICIENT, [f.id for f in _by(facts, "not_in_graph")]
    routes = _by(facts, "eligible_route_hops")
    cleared = [f for f in facts if clears(f)]
    if routes:
        verdict, basis = Verdict.SUSPICIOUS, [min(routes, key=lambda f: (f.value, _remembered(f)))]
    elif cleared:
        verdict, basis = Verdict.CLEAR, [max(cleared, key=lambda f: (f.scope["requested_depth"], not _remembered(f)))]
    else:
        verdict, basis = Verdict.INSUFFICIENT, _by(facts, "no_eligible_route") + _by(facts, "eligible_search_incomplete")
    ids = [f.id for f in basis]
    if verdict != Verdict.INSUFFICIENT and all(_remembered(f) for f in basis):
        return AnswerState.ANSWERED_FROM_MEMORY, verdict, ids
    if _by(facts, "memory_stale"):
        return AnswerState.MEMORY_STALE_REINVESTIGATED, verdict, ids
    return (AnswerState.ANSWERED_WITH_PATH if verdict == Verdict.SUSPICIOUS else AnswerState.NOT_IN_GRAPH), verdict, ids


@dataclass
class Statement:
    text: str
    facts: list[str]

    def to_json(self) -> dict:
        return {"text": self.text, "facts": self.facts}


def render(entity_id: str, state: AnswerState, verdict: Verdict, facts: list[Fact],
           ledger: EvidenceLedger) -> list[Statement]:
    """The analyst's card: fixed sentences filled only from facts, each citing the facts it uses."""
    out: list[Statement] = []

    def label(eid: str) -> str:
        name = ledger.name(eid)
        return f"{name} ({eid})" if name and name != eid else eid

    def say(text: str, *cited: Fact | None) -> None:
        out.append(Statement(text, [f.id for f in cited if f is not None]))

    if state == AnswerState.UNKNOWN_ENTITY:
        say(f"{entity_id} is not in the graph, so nothing else was queried.", *_by(facts, "not_in_graph"))
        return out

    remembered = [f for f in facts if _remembered(f)]
    if remembered:
        episodes = list(dict.fromkeys(f.scope["memory"]["episode_id"] for f in remembered))
        revision = max(f.scope["memory"]["valid_rev"] for f in remembered)
        one = len(remembered) == 1
        say(f"From memory: {_count(len(remembered), 'finding')} recorded by {_and(episodes)} still "
            f"{'holds' if one else 'hold'}, because nothing {'it depends' if one else 'they depend'} on has changed "
            f"since graph revision {revision}.", *remembered)
    stale: dict[str, list[Fact]] = {}
    for f in _by(facts, "memory_stale"):
        stale.setdefault(f.scope["why"], []).append(f)
    for why, group in stale.items():
        names = _and(list(dict.fromkeys(FINDING_NAMES.get(f.value, f.value) for f in group)))
        say(f"The remembered {names} went stale ({why}), so {'it' if len(group) == 1 else 'they'} could not "
            f"be reused.", *group)

    routes = _first_per(sorted(_by(facts, "eligible_route_hops"), key=lambda f: f.value), key=lambda f: f.object)
    for n, f in enumerate(routes):
        if n == 0:
            say(f"{label(f.subject)} has an eligible route to sanctioned {label(f.object)} in {f.value} hops: "
                f"{' → '.join(f.scope['path']['nodes'])}.", f)
            for hop in f.scope["identity_hops"]:
                say(f"The route crosses shared {hop['type'].lower()} {hop['id']}, used by "
                    f"{' and '.join(hop['accounts_on_path'])}; that hop is an identity link, not a transfer.", f)
        else:
            say(f"It also has an eligible route to sanctioned {label(f.object)} in {f.value} hops.", f)

    if not routes:
        cleared = [f for f in facts if clears(f)]
        if verdict == Verdict.CLEAR and cleared:
            f = max(cleared, key=lambda f: f.scope["requested_depth"])
            if f.scope["exhausted"] and f.scope["reached"] == 0:
                text = (f"No transfer leaves {label(f.subject)} and it shares no device, so no eligible route to a "
                        f"sanctioned entity exists.")
            elif f.scope["exhausted"]:
                text = (f"Every entity reachable from {label(f.subject)} over eligible routes was checked "
                        f"({f.scope['reached']:,}); none is sanctioned.")
            else:
                text = (f"No eligible route from {label(f.subject)} to a sanctioned entity was found within "
                        f"{f.scope['requested_depth']} hops ({f.scope['reached']:,} entities reached).")
            say(text + " That is not proof the account is safe.", f)
        else:
            searches = _by(facts, "no_eligible_route") + _by(facts, "eligible_search_incomplete")
            if searches:
                f = max(searches, key=lambda f: (f.scope["completed_depth"], f.scope["requested_depth"]))
                scope = f.scope
                why = ("it hit the node budget" if scope["truncated"] else "it ran out of time" if scope["timed_out"]
                       else f"it was only asked to search {scope['requested_depth']} hops")
                say(f"The eligible-route search from {label(f.subject)} covered {scope['completed_depth']} of the "
                    f"{PRESCRIBED_DEPTH} hops required to clear an account because {why}, so finding no route "
                    f"there does not clear it.", f)
            else:
                say(f"No eligible-route search was run from {label(entity_id)}, so nothing can be concluded about "
                    f"its exposure.", *_by(facts, "status")[:1])

    route_targets = {f.object for f in routes}
    for f in _first_per(_by(facts, "sanctioned_within_reach"), key=lambda f: f.object):
        if f.object in route_targets:
            continue
        reasons = "; ".join(f.scope["breaks"]) or "no eligible route reaches it in the searched scope"
        say(f"Sanctioned {label(f.object)} is {f.value} hops away if direction is ignored "
            f"({' → '.join(f.scope['path']['nodes'])}), but money from {label(f.subject)} cannot follow that path: "
            f"{reasons}. That is an association, not exposure.", f)
    for f in _first_per(_by(facts, "risk_flag_within_reach"), key=lambda f: f.object):
        say(f"It reaches flagged {label(f.object)} at depth {f.value}.", f)

    transfers = _by(facts, "transfer_amount")
    for f in _first_per(_by(facts, "money_trail")):
        legs = [t for t in transfers if t.scope.get("trail") == f.id]
        say(f"Money followed in time order from {label(f.subject)}: "
            + "; ".join(f"{leg['from']} → {leg['to']} {_money(leg['amount_usd'])} at {leg['time']}"
                        for leg in f.scope["legs"]) + ".", f, *legs)

    for f in _first_per(_by(facts, "max_flow_capacity")):
        if f.value:
            say(f"Up to {_money(f.value)} could reach {label(f.object)} from {_and(f.scope['sources'])} "
                f"(max flow over aggregated transfers: an upper bound that ignores timing).", f)
        else:
            say(f"No chain of transfers carries money from {_and(f.scope['sources'])} to {label(f.object)} "
                f"(max flow $0 over aggregated transfers).", f)

    for f in _first_per(_by(facts, "ring_size")):
        if f.scope["members"]:
            say(f"Label propagation puts {_and(f.scope['members'])} in one community of {f.value}.", f)
        else:
            say(f"{label(f.subject)} sits in a community of {f.value:,}, too large to treat as a ring.", f)
    for f in _first_per(_by(facts, "top_broker")):
        say(f"{label(f.object)} ranks highest on betweenness among the {f.scope['scored']} accounts scored, "
            f"so money between the others passes through it.", f)

    compliance = _by(facts, "waiver_compliance")
    own = [c for c in compliance if not c.scope.get("other")]
    for f in _first_per(own, key=lambda f: f.subject):
        scope = f.scope
        applies = next((a for a in _by(facts, "waiver_applies_to") if a.subject == f.subject), None)
        approval = next((a for a in _by(facts, "approved_by") if a.subject == f.subject), None)
        approver, sources = scope.get("approved_by"), scope.get("sources") or []
        say(f"{f.subject} waived {scope.get('control') or 'a control'} for {label(scope['applies_to'])}"
            + (f", approved by {label(approver)} with {scope.get('approver_clearance')} clearance" if approver else "")
            + (f" (sources: {_and(sources)})" if sources else "") + ".", f, applies, approval)
        threshold = next((t for t in _by(facts, "policy_threshold") if t.subject == f.object), None)
        volume = next((v for v in _by(facts, "monthly_volume") if v.scope.get("recorded_on") == f.subject), None)
        if f.value == "BREACH":
            say(f"That breaks {f.object}: {'; '.join(scope['details'])}.", f, threshold, volume)
        else:
            say(f"That waiver is within {f.object} (version {scope.get('policy_version')}).", f, threshold, volume)
    checked = {f.subject for f in own}
    for f in _first_per(_by(facts, "waiver_applies_to")):
        if f.subject in checked:
            continue
        approval = next((a for a in _by(facts, "approved_by") if a.subject == f.subject), None)
        sources = f.scope["sources"]
        say(f"{f.subject} waived {f.value or 'a control'} for {label(f.object)}"
            + (f", approved by {label(approval.object)} with {approval.value} clearance" if approval else "")
            + (f" (sources: {_and(sources)})" if sources else "") + ".", f, approval)
    for f in _first_per([c for c in compliance if c.scope.get("other")]):
        status = "also in breach" if f.value == "BREACH" else "within policy"
        say(f"{f.scope['approved_by']} also approved {f.subject} for {label(f.scope['applies_to'])}, {status}.", f)

    latest: dict[str, Fact] = {}
    for f in _by(facts, "action_status"):
        latest[f.subject] = f
    requested = {t for f in latest.values() for t in f.scope["targets"]}

    for f in _first_per(_by(facts, "collateral"), key=lambda f: f.subject):
        via = _and(f.scope["shared_via"])
        say(f"A blanket block would also hit {label(f.subject)}, a {f.value} account moving "
            f"{_money(f.scope['monthly_volume_usd'])}/month through shared {via}, so {via} should stay open.", f)
    for f in _first_per(_by(facts, "also_exposed"), key=lambda f: f.subject):
        left_out = " The freeze request leaves it out." if latest and f.subject not in requested else ""
        say(f"{label(f.subject)} shares {_and(f.scope['shared_via'])} with the accounts to be frozen and has its own "
            f"eligible route to sanctioned {label(f.object)} in {f.value} hops, so it is not a customer to "
            f"protect.{left_out}", f)

    for f in latest.values():
        targets = _count(len(f.scope["targets"]), "account")
        name = ledger.name(f.object)
        approver = f"{name} ({f.object}, {f.scope['approver_clearance']})" if name else f.object
        if f.value == "PENDING_L3":
            say(f"Freeze request {f.subject} for {targets} is waiting for {approver} to sign off under "
                f"{f.scope['policy_id']}; nothing is frozen yet.", f)
        elif f.value == "APPROVED":
            say(f"Freeze request {f.subject} was signed off by {approver}; {targets} frozen.", f)
        else:
            say(f"Freeze request {f.subject} is {f.value}.", f)
    return out


def claims_from_facts(facts: list[Fact]) -> list[dict]:
    """Graph edges behind the card, for highlighting."""
    claims: list[dict] = []

    def edge(src: str, rel: str, dst: str) -> None:
        claims.append({"subject": src, "relation": "TRANSFERRED" if rel == "FLOWS_TO" else rel, "object": dst})

    for f in facts:
        if f.predicate in ("eligible_route_hops", "sanctioned_within_reach"):
            for step in f.scope["path"]["steps"]:
                src, dst = (step["from"], step["to"]) if step.get("forward", True) else (step["to"], step["from"])
                edge(src, step["rel"], dst)
        elif f.predicate == "transfer_amount":
            edge(f.subject, "TRANSFERRED", f.object)
        elif f.predicate == "approved_by":
            edge(f.object, "APPROVED", f.subject)
        elif f.predicate == "waiver_applies_to":
            edge(f.subject, "APPLIES_TO", f.object)
        elif f.predicate == "waiver_compliance" and not f.scope.get("other") and f.object:
            edge(f.subject, "GOVERNED_BY", f.object)
        elif f.predicate == "action_status":
            edge(f.subject, "ROUTED_TO", f.object)
            edge(f.subject, "UNDER_POLICY", f.scope["policy_id"])
    return list({(c["subject"], c["relation"], c["object"]): c for c in claims}.values())


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("$", "").replace(",", "").strip())
        except ValueError:
            return None
    return None


def _same(claimed: Any, actual: Any) -> bool:
    a, b = _number(claimed), _number(actual)
    if a is not None and b is not None:
        return abs(a - b) < 0.5
    return str(claimed).strip().upper() == str(actual).strip().upper()


def _show(value: Any, unit: str | None) -> str:
    if unit and unit.startswith("USD") and _number(value) is not None:
        return _money(_number(value))
    return str(value)


def bind(statements: Any, facts: list[Fact]) -> list[dict]:
    """Match each statement the model proposed to the fact it restates, or reject it with the reason."""
    out = []
    for raw in statements if isinstance(statements, list) else []:
        if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and raw[k].strip()
                                                for k in ("subject", "predicate")):
            out.append({"statement": raw, "status": "rejected", "reason": "needs a subject and a predicate"})
            continue
        subject, predicate = raw["subject"].strip(), raw["predicate"].strip().lower()
        obj = raw.get("object").strip() if isinstance(raw.get("object"), str) and raw["object"].strip() else None
        value = raw.get("value")
        if predicate not in MODEL_PREDICATES:
            out.append({"statement": raw, "status": "rejected", "reason": f"unknown predicate {predicate}"})
            continue
        candidates = [f for f in facts if f.predicate == predicate
                      and (f.subject == subject or subject in f.scope.get("sources", []))
                      and (obj is None or f.object == obj)]
        target = f"{subject} → {obj}" if obj else subject
        family = UNIT_FAMILY.get(predicate)
        elsewhere = next((f for f in facts if family and value is not None and f not in candidates
                          and (f.unit or "").split("/")[0] == family and _same(value, f.value)), None)
        hint = f"; {_show(value, elsewhere.unit)} is {elsewhere.subject}'s {elsewhere.predicate} ({elsewhere.id})" \
            if elsewhere else ""
        if not candidates:
            out.append({"statement": raw, "status": "rejected",
                        "reason": f"no tool result gives {predicate} for {target}{hint}"})
            continue
        match = candidates[0] if value is None else next((f for f in candidates if _same(value, f.value)), None)
        if match is None:
            actual = candidates[-1]
            out.append({"statement": raw, "status": "rejected",
                        "reason": f"{predicate} for {target} is {_show(actual.value, actual.unit)} ({actual.id}), "
                                  f"not {_show(value, actual.unit)}{hint}"})
            continue
        out.append({"statement": raw, "status": "bound", "fact": match.id})
    return out
