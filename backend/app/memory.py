"""Memory: cases, episodes with their steps, and typed facts that know when they stop holding.

Every investigation opens its Case first. When it ends, its Episode and Steps are always written, and its
facts are written CURRENT only if the graph is still at the revision the investigation started from (one
conditional write). A newer fact SUPERSEDES the older ones in its slot, in that same write. A hunter's run
also needs its claim on the alert to hold for that write, for case merges and for pinning its freeze request;
a run that lost its claim keeps its Episode as a record, marked claim_lost, and publishes nothing else.

Staleness is worked out on every recall:
- Selective: a route, money trail, association or waiver-compliance fact rests on specific evidence. It goes
  STALE when an entity or edge it depends on is gone, or an entity was touched after the fact's revision.
- Graph-wide: a CLEAR finding (no route) and aggregates (max flow, ring, broker rank) depend on the whole
  graph, so they go STALE on any later revision.

Procedural memory is separate: a run that ends in a freeze request keeps its parameterized steps, and they
become a Playbook only once a human approves that request.
"""

from __future__ import annotations

import json
import time
import uuid

from falkordb import Graph

from backend.app import playbook as pb
from backend.app import queries as q
from backend.app.contracts import (PERSISTED_KINDS, VERDICT_PREDICATES, Claim, claim_fence, claim_holds,
                                   current_rev)
from backend.app.guardrails import path_dependencies, search_clears

PERSISTED = {"subject_sanctioned", "eligible_route_hops", "no_eligible_route", "sanctioned_within_reach",
             "money_trail", "max_flow_capacity", "ring_size", "top_broker", "waiver_compliance"}


def slot(fact: dict) -> str:
    """Facts in one slot answer the same question, so a newer one supersedes the older."""
    if fact["predicate"] in VERDICT_PREDICATES:
        return f"exposure|{fact['subject']}"
    return f"{fact['predicate']}|{fact['subject']}|{fact.get('object') or ''}"


def graph_wide(fact: dict) -> bool:
    return bool(fact.get("aggregate")) or fact["predicate"] == "no_eligible_route"


def open_case(graph: Graph, case_id: str, entity_id: str, worker_id: str, now: float | None = None,
              claim: Claim | None = None) -> bool:
    """Open the case (or find it open); False, writing nothing, if `claim` no longer holds."""
    fence, owned = claim_fence(claim)
    return bool(graph.query(fence + """
        MERGE (c:Entity:Case {id: $case})
        ON CREATE SET c.status = 'OPEN', c.created_at = $now, c.subject_id = $subject, c.opened_by = $worker
        WITH c
        OPTIONAL MATCH (x:Entity {id: $subject}) WHERE NOT x:Case
        FOREACH (_ IN CASE WHEN x IS NULL THEN [] ELSE [1] END | MERGE (c)-[:ABOUT]->(x))
        RETURN count(c)
    """, {"case": case_id, "now": now or time.time(), "subject": entity_id, "worker": worker_id,
          **owned}).result_set[0][0])


def read_fact(props: dict, rev: int, graph: Graph) -> dict:
    """A persisted fact with its status as of now: a CURRENT fact whose evidence changed reads as STALE."""
    fact = {
        "id": props["id"], "kind": props["kind"], "subject": props["subject"], "predicate": props["predicate"],
        "object": props.get("object") or None, "value": json.loads(props["value"]), "unit": props.get("unit") or None,
        "scope": json.loads(props["scope"]), "depends_on": list(props.get("depends_on") or []),
        "aggregate": bool(props.get("aggregate")), "valid_rev": props["valid_rev"],
        "episode_id": props["episode_id"], "claim": props.get("claim") or "",
        "rerun": {"tool": props["source_tool"], "args": json.loads(props["source_args"])},
        "status": props["status"], "why_stale": props.get("stale_reason"), "changed": [],
    }
    if fact["status"] == "CURRENT":
        if graph_wide(fact):
            if rev > fact["valid_rev"]:
                fact["status"] = "STALE"
                fact["why_stale"] = (f"the graph moved from revision {fact['valid_rev']} to {rev}, and "
                                     + ("a finding of no route depends" if fact["predicate"] == "no_eligible_route"
                                        else "aggregate results depend") + " on all of it")
        else:
            missing, touched = q.changed_dependencies(graph, fact["depends_on"], fact["valid_rev"])
            if missing or touched:
                fact["status"] = "STALE"
                fact["why_stale"] = q.describe_change(missing, touched, fact["valid_rev"])
                fact["changed"] = missing + touched
    return fact


def shared_routes(graph: Graph, entity_id: str) -> list[dict]:
    """Routes that other investigations found and that pass through this entity. From the entity onward, each is
    an eligible route of its own, and it still holds if no node or edge on that part changed since it was found."""
    rows = graph.ro_query("""
        MATCH (f:SemanticFact {predicate: 'eligible_route_hops'})
        WHERE f.status = 'CURRENT' AND f.subject <> $id AND $id IN f.depends_on
        MATCH (f)-[:FROM_EPISODE]->(ep:Episode)
        RETURN f, ep.worker_id, ep.case_id
        ORDER BY f.created_at
    """, {"id": entity_id}).result_set
    routes, seen = [], set()
    for node, worker, case in rows:
        props = node.properties
        scope = json.loads(props["scope"])
        nodes, identity = scope["path"]["nodes"], scope.get("identity_hops") or []
        if entity_id not in nodes or entity_id in {h["id"] for h in identity}:
            continue
        i = nodes.index(entity_id)
        part = {"nodes": nodes[i:], "steps": scope["path"]["steps"][i:]}
        key = (props["object"], tuple(part["nodes"]))
        if len(part["nodes"]) < 2 or key in seen:
            continue
        seen.add(key)
        depends_on = path_dependencies(part)
        missing, touched = q.changed_dependencies(graph, depends_on, props["valid_rev"])
        if missing or touched:
            continue
        routes.append({"fact_id": props["id"], "episode_id": props["episode_id"], "case_id": case,
                       "worker_id": worker, "found_from": props["subject"], "target": props["object"],
                       "hops": len(part["nodes"]) - 1, "path": part,
                       "identity_hops": [h for h in identity if h["id"] in part["nodes"][1:]],
                       "valid_rev": props["valid_rev"], "depends_on": depends_on})
    return routes


def recall(graph: Graph, entity_id: str, facts: bool = True) -> dict | None:
    """What memory holds about an entity: facts (CURRENT, or STALE with the reason and the call that would
    refresh them), routes other investigations found through it, recent episodes, approval requests with their
    status read now, and matching playbooks. With `facts` false, only the playbooks: procedural memory without
    remembered findings."""
    started = time.perf_counter()
    fingerprint = q.topology_fingerprint(graph, entity_id)
    if fingerprint is None:
        return None
    rev = current_rev(graph)
    if not facts:
        return {"entity_id": entity_id, "rev": rev, "facts": [], "shared": [], "episodes": [], "approvals": [],
                "fingerprint": fingerprint, "playbooks": q.playbooks_for(graph, fingerprint["patterns"]),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
    rows = graph.ro_query("""
        MATCH (f:SemanticFact)-[:ABOUT]->(:Entity {id: $id})
        WHERE f.status IN ['CURRENT', 'STALE']
        RETURN f ORDER BY f.created_at DESC, f.source_step
    """, {"id": entity_id}).result_set
    remembered = [read_fact(node.properties, rev, graph) for (node,) in rows]
    episodes = graph.ro_query("""
        MATCH (ep:Episode)-[:ABOUT]->(:Entity {id: $id})
        WHERE NOT coalesce(ep.claim_lost, false)
        OPTIONAL MATCH (ep)-[:HAS_STEP]->(s:Step)
        WITH ep, s ORDER BY s.seq
        WITH ep, collect(s.tool) AS tools
        RETURN ep.id, ep.case_id, ep.created_at, ep.verdict, ep.answer_state, ep.planner, ep.evidence_rev,
               coalesce(ep.facts_cached, true), tools
        ORDER BY ep.created_at DESC LIMIT 3
    """, {"id": entity_id}).result_set
    approvals = graph.ro_query("""
        MATCH (c:Case)-[:ABOUT]->(:Entity {id: $id})
        MATCH (r:ApprovalRequest)-[:FOR_CASE]->(c)
        OPTIONAL MATCH (r)-[:UNDER_POLICY]->(p:PolicyDoc)
        OPTIONAL MATCH (r)-[:ROUTED_TO]->(e:Employee)
        RETURN r.id, r.status, c.id, r.target_ids, r.excluded_ids, p.id, e.id, e.name, e.clearance, r.created_at
        ORDER BY r.created_at DESC LIMIT 3
    """, {"id": entity_id}).result_set
    return {
        "entity_id": entity_id,
        "rev": rev,
        "facts": remembered,
        "shared": shared_routes(graph, entity_id),
        "episodes": [{"id": eid, "case_id": case, "created_at": created, "verdict": verdict, "answer_state": state,
                      "planner": planner, "evidence_rev": ev_rev, "facts_cached": cached, "tools": tools}
                     for eid, case, created, verdict, state, planner, ev_rev, cached, tools in episodes],
        "approvals": [{"request_id": rid, "status": status, "case_id": case, "target_ids": targets or [],
                       "excluded_ids": excluded or [], "policy_id": policy,
                       "approver": {"id": aid, "name": name, "clearance": clearance} if aid else None}
                      for rid, status, case, targets, excluded, policy, aid, name, clearance, _ in approvals],
        "fingerprint": fingerprint,
        "playbooks": q.playbooks_for(graph, fingerprint["patterns"]),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def procedure_of(graph: Graph, entity_id: str, case_id: str, results: list[dict], final: dict) -> dict | None:
    """The parameterized steps of a fresh SUSPICIOUS run that crossed a shared device and ended in a freeze
    request, with the subject's topology fingerprint; None if the run does not fit the playbook family."""
    if (final["verdict"], final["answer_state"]) != ("SUSPICIOUS", "answered_with_path") or not final["approval_request"]:
        return None
    if "route.after_device" not in pb.roles(results, entity_id, case_id):
        return None
    fingerprint = q.topology_fingerprint(graph, entity_id)
    steps = pb.parameterize(results, entity_id, case_id)
    if not steps or not fingerprint or not pb.required_patterns(fingerprint):
        return None
    return {"steps": steps, "fingerprint": fingerprint}


def persist_run(graph: Graph, *, case_id: str, entity_id: str, worker_id: str, planner: str, model: str | None,
                start_rev: int, results: list[dict], final: dict, playbook_id: str | None = None,
                now: float | None = None, claim: Claim | None = None) -> dict:
    """Write the episode and its steps, then the run's facts if the graph is still at `start_rev` and `claim`, if
    any, still holds."""
    now = now or time.time()
    episode = f"ep-{uuid.uuid4().hex[:10]}"
    steps = [{"seq": i, "tool": r["tool"], "args": json.dumps(r["args"], default=str), "ok": r["ok"],
              "ms": r["meta"]["elapsed_ms"], "rows": r["meta"]["rows"], "error": r["error"] or ""}
             for i, r in enumerate(results, start=1)]
    procedure = procedure_of(graph, entity_id, case_id, results, final)
    graph.query("""
        MERGE (c:Entity:Case {id: $case})
        ON CREATE SET c.status = 'OPEN', c.created_at = $now, c.subject_id = $entity
        CREATE (ep:Episode {id: $ep, case_id: $case, entity_id: $entity, created_at: $now, worker_id: $worker,
                            planner: $planner, model: $model, verdict: $verdict, answer_state: $state,
                            evidence_rev: $rev, tool_calls: $calls, elapsed_ms: $ms})
        SET ep.request_id = $request, ep.procedure = $procedure, ep.playbook_id = $playbook
        CREATE (ep)-[:FOR_CASE]->(c)
        WITH ep
        OPTIONAL MATCH (x:Entity {id: $entity}) WHERE NOT x:Case
        FOREACH (_ IN CASE WHEN x IS NULL THEN [] ELSE [1] END | CREATE (ep)-[:ABOUT]->(x))
        WITH ep
        OPTIONAL MATCH (p:Playbook {id: $playbook})
        FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END |
            CREATE (ep)-[:USED_PLAYBOOK]->(p)
            SET p.uses = coalesce(p.uses, 0) + 1, p.last_used_at = $now)
        WITH ep
        UNWIND $steps AS s
        CREATE (ep)-[:HAS_STEP]->(:Step {seq: s.seq, tool: s.tool, args: s.args, ok: s.ok, elapsed_ms: s.ms,
                                         rows: s.rows, error: s.error})
    """, {"case": case_id, "now": now, "entity": entity_id, "ep": episode, "worker": worker_id, "planner": planner,
          "model": model or "", "verdict": final["verdict"], "state": final["answer_state"], "rev": start_rev,
          "calls": final["tool_calls"], "ms": final["elapsed_ms"], "steps": steps,
          "request": (final["approval_request"] or {}).get("request_id"),
          "procedure": json.dumps(procedure) if procedure else None, "playbook": playbook_id})

    def stated(fact_id: str) -> str:
        return next((s["text"] for s in final["statements"] if fact_id in s["facts"]), "")

    def args(step: int) -> dict:
        return results[step - 1]["args"] if 0 < step <= len(results) else {}

    keep = [f for f in final["facts"] if f["predicate"] in PERSISTED and f["kind"] in PERSISTED_KINDS
            and not f["scope"].get("memory")
            and (f["predicate"] != "no_eligible_route" or search_clears(f["scope"]))]
    rows = [{"id": f"fact-{uuid.uuid4().hex[:10]}", "kind": f["kind"], "subject": f["subject"],
             "predicate": f["predicate"], "object": f["object"] or "", "value": json.dumps(f["value"], default=str),
             "unit": f["unit"] or "", "scope": json.dumps(f["scope"], default=str), "depends_on": f["depends_on"],
             "aggregate": f["aggregate"], "slot": slot(f), "claim": stated(f["id"]),
             "source_tool": f["source"]["tool"], "source_step": f["source"]["step"],
             "source_key": f["source"]["result_key"], "source_args": json.dumps(args(f["source"]["step"]))}
            for f in keep]
    written, note, superseded = 0, None, []
    if rows:
        fence, owned = claim_fence(claim)
        written, superseded = graph.query(fence + """
            MATCH (meta:Meta {key: 'dataset'}) WHERE coalesce(meta.rev, 0) = $rev
            MATCH (ep:Episode {id: $ep}), (x:Entity {id: $entity})
            UNWIND $facts AS f
            CREATE (n:SemanticFact {id: f.id, kind: f.kind, subject: f.subject, predicate: f.predicate,
                                    object: f.object, value: f.value, unit: f.unit, scope: f.scope,
                                    depends_on: f.depends_on, aggregate: f.aggregate, slot: f.slot, claim: f.claim,
                                    source_tool: f.source_tool, source_step: f.source_step,
                                    source_key: f.source_key, source_args: f.source_args, status: 'CURRENT',
                                    valid_rev: $rev, created_at: $now, episode_id: $ep, entity_id: $entity})
            CREATE (n)-[:ABOUT]->(x), (n)-[:FROM_EPISODE]->(ep)
            WITH n, x
            OPTIONAL MATCH (old:SemanticFact)-[:ABOUT]->(x)
            WHERE old.slot = n.slot AND old.episode_id <> $ep AND old.status IN ['CURRENT', 'STALE']
            FOREACH (_ IN CASE WHEN old IS NULL THEN [] ELSE [1] END |
                SET old.status = 'SUPERSEDED', old.superseded_at = $now, old.superseded_by = n.id
                CREATE (n)-[:SUPERSEDES]->(old))
            RETURN count(DISTINCT n), collect(DISTINCT old.id)
        """, {"rev": start_rev, "ep": episode, "entity": entity_id, "facts": rows, "now": now,
              **owned}).result_set[0]
        if written:
            graph.query("""
                UNWIND $pairs AS p
                MATCH (n:SemanticFact {id: p[0]}), (d:Entity {id: p[1]})
                CREATE (n)-[:DERIVED_FROM]->(d)
            """, {"pairs": [[r["id"], d] for r in rows for d in dict.fromkeys(r["depends_on"]) if "|" not in d]})
        else:
            note = (f"{worker_id} no longer held its claim on {claim.alert_id} (the lease ran out or another hunter "
                    f"took the alert over), so its facts were not cached" if claim and not claim_holds(graph, claim)
                    else f"the graph moved from revision {start_rev} to {current_rev(graph)} during the "
                         f"investigation, so its facts were not cached")
            graph.query("MATCH (ep:Episode {id: $ep}) SET ep.facts_cached = false, ep.cache_note = $note",
                        {"ep": episode, "note": note})

    recalled = next((r["data"] for r in results if r["tool"] == "recall_memory" and r["ok"]), None)
    stale = [[f["id"], f["why_stale"]] for f in (recalled or {}).get("facts", [])
             if f["status"] == "STALE" and f["id"] not in superseded]
    if stale:
        graph.query("""
            UNWIND $stale AS s
            MATCH (f:SemanticFact {id: s[0]}) WHERE f.status = 'CURRENT'
            SET f.status = 'STALE', f.stale_reason = s[1]
        """, {"stale": stale})

    reused = list(dict.fromkeys(f["scope"]["shared"]["fact_id"] for f in final["facts"] if f["scope"].get("shared")))
    if reused:
        graph.query("""
            MATCH (ep:Episode {id: $ep})
            UNWIND $ids AS fid
            MATCH (f:SemanticFact {id: fid})
            MERGE (ep)-[:REUSED]->(f)
        """, {"ep": episode, "ids": reused})
    deferred = [[f["subject"], f["value"]] for f in final["facts"] if f["predicate"] == "already_requested"]
    if deferred:
        graph.query("""
            MATCH (ep:Episode {id: $ep})
            UNWIND $deferred AS d
            MATCH (r:ApprovalRequest {id: d[0]})
            MERGE (ep)-[w:DEFERRED_TO]->(r)
            ON CREATE SET w.targets = d[1]
        """, {"ep": episode, "deferred": deferred})
    case = None
    if final["verdict"] == "SUSPICIOUS":
        basis = [f for f in final["facts"] if f["id"] in final["verdict_basis"]]
        targets = (final["approval_request"] or {}).get("target_ids") or []
        case = link_case(graph, case_id, [d for f in basis for d in f["depends_on"] if "|" not in d] + targets,
                         worker_id, now, claim=claim)
    lost = claim is not None and not claim_holds(graph, claim)
    if lost:
        graph.query("MATCH (ep:Episode {id: $ep}) SET ep.claim_lost = true", {"ep": episode})
    return {"episode_id": episode, "case_id": case_id, "facts_written": written, "cached": note is None,
            "note": note, "superseded": sorted(superseded), "marked_stale": [s[0] for s in stale],
            "reused": reused, "deferred_to": [d[0] for d in deferred], "case": case, "claim_lost": lost}


HIGH_RISK = "x:Sanctioned OR x.risk_flags IS NOT NULL"


def link_case(graph: Graph, case_id: str, evidence: list[str], worker_id: str, now: float | None = None,
              claim: Claim | None = None) -> dict | None:
    """Attach evidence to a case and merge it with every case that shares a high-risk entity (a sanctioned account
    or a flagged device) or at least two entities with it. The case of the earliest alert stays canonical (the
    earliest opened, without alerts), so concurrent hunters converge on the same one. None, attaching and merging
    nothing, if the case is missing or `claim` no longer holds."""
    now = now or time.time()
    fence, owned = claim_fence(claim)
    if not graph.query(fence + """
        MATCH (c:Case {id: $case})
        UNWIND CASE WHEN size($ids) = 0 THEN [NULL] ELSE $ids END AS eid
        OPTIONAL MATCH (x:Entity {id: eid}) WHERE NOT x:Case
        FOREACH (_ IN CASE WHEN x IS NULL THEN [] ELSE [1] END | MERGE (c)-[:EVIDENCE]->(x))
        RETURN count(DISTINCT c)
    """, {"case": case_id, "ids": list(dict.fromkeys(evidence)), **owned}).result_set[0][0]:
        return None
    overlaps = graph.ro_query(f"""
        MATCH (c:Case {{id: $case}})-[:EVIDENCE]->(x:Entity)<-[:EVIDENCE]-(o:Case)
        WHERE o <> c
        WITH o, collect(x.id) AS shared, sum(CASE WHEN {HIGH_RISK} THEN 1 ELSE 0 END) AS risky
        WHERE risky > 0 OR size(shared) >= 2
        OPTIONAL MATCH (o)-[:MERGED_INTO]->(root:Case)
        RETURN o.id, coalesce(root.id, o.id), shared, risky > 0
    """, {"case": case_id}).result_set
    own_root = graph.ro_query("MATCH (:Case {id: $case})-[:MERGED_INTO]->(r:Case) RETURN r.id",
                              {"case": case_id}).result_set
    group = {own_root[0][0] if own_root else case_id} | {root for _, root, _, _ in overlaps}
    order = graph.ro_query("""
        UNWIND $ids AS cid
        MATCH (c:Case {id: cid})
        OPTIONAL MATCH (al:Alert {case_id: cid})
        RETURN cid, min(al.created_at), c.created_at
    """, {"ids": sorted(group)}).result_set
    canonical = min(order, key=lambda r: (r[1] is None, r[1] if r[1] is not None else r[2], r[0]))[0]
    shared = sorted({x for _, _, xs, _ in overlaps for x in xs})
    rule = ("shares a high-risk entity" if any(risky for *_, risky in overlaps)
            else "shares two or more entities") if overlaps else None
    merging = sorted(group - {canonical})
    if merging:
        params = {"canonical": canonical, "merging": merging, "now": now, "shared": shared, "rule": rule,
                  "worker": worker_id, **claim_fence(claim)[1]}
        graph.query(fence + """
            MATCH (k:Case {id: $canonical})
            UNWIND $merging AS mid
            MATCH (child:Case)-[old:MERGED_INTO]->(:Case {id: mid})
            MERGE (child)-[moved:MERGED_INTO]->(k)
            ON CREATE SET moved.at = old.at, moved.shared = old.shared, moved.rule = old.rule, moved.by = old.by
            DELETE old
        """, params)
        if not graph.query(fence + """
            MATCH (k:Case {id: $canonical})
            UNWIND $merging AS mid
            MATCH (m:Case {id: mid})
            MERGE (m)-[e:MERGED_INTO]->(k)
            ON CREATE SET e.at = $now, e.shared = $shared, e.rule = $rule, e.by = $worker
            SET m.status = 'MERGED'
            RETURN count(m)
        """, params).result_set[0][0]:
            return None
    return {"case_id": case_id, "evidence": len(set(evidence)), "canonical": canonical,
            "merged_into": canonical if canonical != case_id else None,
            "merged": [m for m in merging if m != case_id], "shared": shared, "rule": rule}


def learn_playbook(graph: Graph, request_id: str, now: float | None = None) -> dict | None:
    """Promote the procedure of the run behind an approved freeze request to a playbook, or add that run to the
    playbook's support if the same procedure was learned before. None if the run left no procedure."""
    rows = graph.ro_query("""
        MATCH (r:ApprovalRequest {id: $rid}) WHERE r.status = 'APPROVED'
        MATCH (ep:Episode {request_id: $rid}) WHERE ep.procedure IS NOT NULL AND NOT coalesce(ep.claim_lost, false)
        RETURN ep.id, ep.procedure, r.approved_by ORDER BY ep.created_at DESC LIMIT 1
    """, {"rid": request_id}).result_set
    if not rows:
        return None
    episode_id, procedure, approver = rows[0]
    procedure = json.loads(procedure)
    required = pb.required_patterns(procedure["fingerprint"])
    playbook_id = f"pb-{pb.signature(procedure['steps'], required)}"
    graph.query("""
        MATCH (ep:Episode {id: $ep})
        MERGE (p:Playbook {id: $id})
        ON CREATE SET p.family = $family, p.steps = $steps, p.required_patterns = $required,
                      p.fingerprint = $fingerprint, p.created_at = $now, p.uses = 0
        MERGE (p)-[d:DERIVED_FROM]->(ep)
        ON CREATE SET d.request_id = $rid, d.approved_by = $approver, d.at = $now
    """, {"ep": episode_id, "id": playbook_id, "family": pb.FAMILY, "steps": json.dumps(procedure["steps"]),
          "required": required, "fingerprint": json.dumps(procedure["fingerprint"]), "now": now or time.time(),
          "rid": request_id, "approver": approver})
    return next(p for p in q.playbooks_for(graph, required) if p["id"] == playbook_id)
