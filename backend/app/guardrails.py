"""Answer states, verdicts and the edge-verified claim checker.

The model's summary is kept only if every entity ID it mentions was returned by a tool in this run
and every structured claim matches an edge a tool returned. Otherwise the summary and claims are
replaced by ones written from the tool results alone.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum


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
class Verification:
    verified: bool
    claims_checked: int
    ids_checked: int
    unknown_ids: list[str]
    rejected_claims: list[dict]
    rewritten: bool = False

    @property
    def badge(self) -> str:
        if self.verified:
            return (f"Verified against graph ({_count(self.claims_checked, 'claim')}, "
                    f"{_count(self.ids_checked, 'entity ID')} checked)")
        return (f"Rewritten from graph results ({_count(len(self.rejected_claims), 'unsupported claim')}, "
                f"{_count(len(self.unknown_ids), 'unknown ID')} in the model's answer)")

    def to_json(self) -> dict:
        return {**asdict(self), "badge": self.badge}


def verify(summary: str, claims: list, ledger: EvidenceLedger, given: list[str] | None = None) -> Verification:
    """`given` are IDs from the request itself (the alert entity), which may legitimately be absent from the graph."""
    normalized = [(raw, normalize_claim(raw)) for raw in claims or []]
    ids = mentioned_ids(summary)
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
    return Verification(verified=not unknown and not rejected, claims_checked=len(normalized), ids_checked=len(ids),
                        unknown_ids=unknown, rejected_claims=rejected)


def route_breaks(path: dict) -> list[str]:
    """Why money from the start of a path could not follow it to the end: a transfer pointing back toward
    the start, or an identity hop through a shared IP (proxies and carrier NAT are shared by strangers;
    a device fingerprint is not). An empty list means the path is an exposure route."""
    breaks = []
    start = path["nodes"][0] if path.get("nodes") else "the start"
    for step in path["steps"]:
        rel, forward = step.get("rel"), step.get("forward", True)
        if rel in MONEY_RELATIONS and not forward:
            breaks.append(f"{step['to']} paid {step['from']}, so that leg carries money toward {start}, not away from it")
        elif rel == "LOGGED_FROM":
            breaks.append(f"{step['to'] if forward else step['from']} is a shared IP, not a shared device")
    return list(dict.fromkeys(breaks))


def sanctioned_hits(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Sanctioned entities reached by explore_neighborhood, split into exposure routes and associations."""
    hits = [h for r in _ok(results, "explore_neighborhood") for h in r["data"]["flagged"]
            if "Sanctioned" in h["entity"]["labels"]]
    routes = [h for h in hits if not route_breaks(h["path"])]
    return routes, [h for h in hits if h not in routes]


def _ok(results: list[dict], tool: str) -> list[dict]:
    return [r for r in results if r["tool"] == tool and r["ok"]]


def _money(usd: float | int | None) -> str:
    return f"${usd:,.0f}" if usd is not None else "an unknown amount"


def _step_claim(step: dict) -> dict | None:
    if not step.get("rel"):
        return None
    src, dst = (step["from"], step["to"]) if step.get("forward", True) else (step["to"], step["from"])
    relation = "TRANSFERRED" if step["rel"] == "FLOWS_TO" else step["rel"]
    return {"subject": src, "relation": relation, "object": dst}


def summarize(entity_id: str, results: list[dict], state: AnswerState, ledger: EvidenceLedger) -> tuple[str, list[dict]]:
    """A summary and claims written only from tool results, so they verify by construction."""
    if state == AnswerState.UNKNOWN_ENTITY:
        return f"{entity_id} is not in the graph, so nothing else was queried.", []

    def label(eid: str) -> str:
        name = ledger.name(eid)
        return f"{name} ({eid})" if name and name != eid else eid

    sentences: list[str] = []
    claims: list[dict] = []
    subject = label(entity_id)
    memory = _ok(results, "recall_memory")
    facts = memory[-1]["data"]["facts"] if memory else []

    if state == AnswerState.ANSWERED_FROM_MEMORY:
        fact = next(f for f in facts if f["status"] == "CURRENT")
        evidence = ", ".join(fact["evidence_ids"]) or "none recorded"
        return (f"Answered from memory: {fact['claim']} (verdict {fact['verdict']}, recorded by episode "
                f"{fact['episode_id']}). None of the entities it rests on ({evidence}) has changed since.", [])
    for fact in facts:
        if fact["status"] == "STALE":
            sentences.append(f"Memory held an earlier {fact['verdict']} finding, but "
                             f"{', '.join(fact['changed_evidence'])} changed after it was recorded, so this was "
                             f"re-investigated from the graph.")

    explores = _ok(results, "explore_neighborhood")
    deepest = max(explores, key=lambda r: r["data"]["depth"]) if explores else None
    routes, associations = sanctioned_hits(results)

    if routes:
        hit = min(routes, key=lambda h: h["depth"])
        target = hit["entity"]["id"]
        sentences.append(f"{subject} is {hit['depth']} hops from sanctioned {label(target)}: "
                         f"{' → '.join(hit['path']['nodes'])}.")
        for hop in hit.get("identity_hops", []):
            sentences.append(f"The chain crosses shared {hop['type'].lower()} {hop['id']}, used by "
                             f"{' and '.join(hop['accounts_on_path'])}; that hop is an identity link, not money.")
        claims += [c for c in (_step_claim(s) for s in hit["path"]["steps"]) if c]
    elif associations:
        hit = min(associations, key=lambda h: h["depth"])
        sentences.append(f"{subject} is {hit['depth']} hops from sanctioned {label(hit['entity']['id'])} "
                         f"({' → '.join(hit['path']['nodes'])}), but money from it cannot follow that route: "
                         f"{'; '.join(route_breaks(hit['path']))}. That is an association, not exposure.")
        claims += [c for c in (_step_claim(s) for s in hit["path"]["steps"]) if c]
    elif deepest:
        data = deepest["data"]
        hops = "1 hop" if data["depth"] == 1 else f"{data['depth']} hops"
        sentences.append(f"Nothing sanctioned is linked to {subject} within {hops} "
                         f"({data['total_reached']:,} entities checked). No recorded link is not proof the account is safe.")
        for hit in data["flagged"]:
            sentences.append(f"It does reach flagged {label(hit['entity']['id'])} at depth {hit['depth']}.")

    for r in _ok(results, "trace_money_flow"):
        for hit in r["data"]["sanctioned"]:
            legs = [f"{t['from']} → {t['to']} {_money(t['amount_usd'])} at {t['time']}" for t in hit["trail"]]
            sentences.append(f"Money followed in time order: {'; '.join(legs)}.")
            claims += [{"subject": t["from"], "relation": "TRANSFERRED", "object": t["to"]} for t in hit["trail"]]

    for r in _ok(results, "exposure_flow"):
        d = r["data"]
        sentences.append(f"Up to {_money(d['max_flow_usd'])} could reach {d['target_id']} from "
                         f"{_and(d['source_ids'])} (max flow over all transfers, an upper bound).")

    for r in _ok(results, "detect_ring"):
        d = r["data"]
        if d["community_size"] <= 25:
            sentences.append(f"Label propagation puts {_and([m['id'] for m in d['members']])} in one "
                             f"community of {d['community_size']}.")
    for r in _ok(results, "find_brokers"):
        brokers = [b for b in r["data"]["brokers"] if b["score"] > 0]
        if brokers:
            sentences.append(f"{label(brokers[0]['id'])} ranks highest on betweenness, so money between "
                             f"the other members passes through it.")

    for r in _ok(results, "trace_governance"):
        for d in r["data"]["decisions"]:
            approver = d["approved_by"]
            if not approver:
                continue
            claims.append({"subject": approver["id"], "relation": "APPROVED", "object": d["id"]})
            claims.append({"subject": d["id"], "relation": "APPLIES_TO", "object": r["data"]["entity_id"]})
            sources = _and([s["id"] for s in d["sources"]]) if d["sources"] else ""
            sentence = (f"{d['id']} waived {d.get('control') or 'a control'} for {r['data']['entity_id']}, approved by "
                        f"{label(approver['id'])} with {approver.get('clearance')} clearance"
                        + (f" (sources: {sources})" if sources else "") + ".")
            for v in d["violations"]:
                sentence += f" That breaks {v['policy_id']}: {v['detail']}."
                claims.append({"subject": d["id"], "relation": "GOVERNED_BY", "object": v["policy_id"]})
            sentences.append(sentence)
        for other in r["data"]["approver_other_decisions"]:
            status = "also in breach" if other["violations"] else "within policy"
            sentences.append(f"{other['approved_by']} also approved {other['decision_id']} for "
                             f"{other['applies_to']['id']} ({status}).")

    for r in _ok(results, "freeze_impact"):
        for c in r["data"]["collateral"]:
            via = _and(c["shared_via"])
            sentences.append(f"A blanket block would also hit {label(c['id'])}, a {c['tier']} account moving "
                             f"{_money(c['monthly_volume_usd'])}/month through shared {via}; it is excluded and {via} stays open.")

    for r in _ok(results, "request_action"):
        d = r["data"]
        approver = d["approver"]
        sentences.append(f"Freeze request {d['request_id']} for {_count(len(d['target_ids']), 'account')} is waiting for "
                         f"{approver.get('name') or approver['id']} ({approver['id']}, {approver.get('clearance')}) "
                         f"to sign off under {d['policy_id']}; nothing is frozen yet.")
        claims.append({"subject": d["request_id"], "relation": "ROUTED_TO", "object": approver["id"]})
        claims.append({"subject": d["request_id"], "relation": "UNDER_POLICY", "object": d["policy_id"]})

    unique = list({(c["subject"], c["relation"], c["object"]): c for c in claims}.values())
    return " ".join(sentences) or f"No conclusion could be drawn about {subject}.", unique
