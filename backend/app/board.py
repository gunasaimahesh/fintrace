"""The evidence board: what hunters and investigations left in the graph, read back from persisted state only.

Queue ownership and attempts, cases with their merges, episodes with the findings they reused and the requests
they deferred to, facts with their status and supersession, and playbooks with where they came from and where
they were used.
"""

from __future__ import annotations

import json
from collections import Counter

from falkordb import Graph

from backend.app import coordination as co
from backend.app import memory
from backend.app import queries as q
from backend.app.contracts import current_rev


def board(graph: Graph, limit: int = 60) -> dict:
    return {"rev": current_rev(graph), "dataset": q.dataset_meta(graph), "queue": co.queue_state(graph),
            "cases": cases(graph), "episodes": episodes(graph, limit), "facts": facts(graph, limit),
            "playbooks": playbooks(graph), "approvals": q.list_approvals(graph, limit=limit)}


def cases(graph: Graph) -> list[dict]:
    rows = graph.ro_query("""
        MATCH (c:Case)
        OPTIONAL MATCH (c)-[m:MERGED_INTO]->(k:Case)
        OPTIONAL MATCH (c)-[:EVIDENCE]->(x:Entity)
        WITH c, m, k, collect(x.id) AS evidence
        OPTIONAL MATCH (ep:Episode)-[:FOR_CASE]->(c)
        WITH c, m, k, evidence, collect(ep.id) AS episodes
        OPTIONAL MATCH (r:ApprovalRequest)-[:FOR_CASE]->(c)
        WITH c, m, k, evidence, episodes, collect(r.id) AS requests
        OPTIONAL MATCH (al:Alert {case_id: c.id})
        RETURN c {.id, .status, .subject_id, .created_at}, k.id, m {.rule, .shared, .by, .at}, evidence, episodes,
               requests, collect(al.id)
    """).result_set
    listed = [{**dict(c), "merged_into": root, "merge": dict(m) if m else None, "evidence": sorted(evidence),
               "episodes": episodes, "requests": requests, "alerts": alerts}
              for c, root, m, evidence, episodes, requests, alerts in rows]
    return sorted(listed, key=lambda c: (c["created_at"] or 0, c["id"]))


def episodes(graph: Graph, limit: int) -> list[dict]:
    rows = graph.ro_query("""
        MATCH (ep:Episode)
        WITH ep ORDER BY ep.created_at DESC LIMIT $limit
        OPTIONAL MATCH (ep)-[:REUSED]->(f:SemanticFact)-[:FROM_EPISODE]->(src:Episode)
        WITH ep, collect(CASE WHEN f IS NULL THEN NULL ELSE
            {fact_id: f.id, episode_id: src.id, worker_id: src.worker_id, case_id: src.case_id,
             found_from: f.subject, target: f.object} END) AS reused
        OPTIONAL MATCH (ep)-[d:DEFERRED_TO]->(r:ApprovalRequest)
        WITH ep, reused, collect(CASE WHEN r IS NULL THEN NULL ELSE
            {request_id: r.id, targets: d.targets, status: r.status} END) AS deferred
        OPTIONAL MATCH (fact:SemanticFact)-[:FROM_EPISODE]->(ep)
        RETURN ep {.id, .case_id, .entity_id, .worker_id, .planner, .model, .verdict, .answer_state, .created_at,
                   .tool_calls, .elapsed_ms, .request_id, .playbook_id, .facts_cached, .cache_note, .evidence_rev},
               reused, deferred, count(fact)
    """, {"limit": limit}).result_set
    listed = [{**dict(ep), "reused": reused, "deferred_to": deferred, "facts_written": n}
              for ep, reused, deferred, n in rows]
    return sorted(listed, key=lambda ep: -ep["created_at"])


def facts(graph: Graph, limit: int) -> dict:
    """Every fact's status as of now, so a fact whose evidence changed reads STALE before anything recalls it."""
    rev = current_rev(graph)
    rows = graph.ro_query("""
        MATCH (f:SemanticFact)-[:FROM_EPISODE]->(ep:Episode)
        OPTIONAL MATCH (f)-[:SUPERSEDES]->(old:SemanticFact)
        RETURN f, ep.worker_id, ep.case_id, collect(old.id)
    """).result_set
    listed = []
    for node, worker, case, supersedes in rows:
        props = node.properties
        fact = memory.read_fact(props, rev, graph)
        listed.append({key: fact[key] for key in ("id", "kind", "subject", "predicate", "object", "value", "unit",
                                                  "status", "why_stale", "changed", "valid_rev", "claim",
                                                  "episode_id")}
                      | {"recorded_status": props["status"], "superseded_by": props.get("superseded_by"),
                         "created_at": props["created_at"], "worker_id": worker, "case_id": case,
                         "supersedes": supersedes, "shared": fact["scope"].get("shared")})
    counts = Counter(f["status"] for f in listed)
    return {"counts": {s: counts[s] for s in ("CURRENT", "STALE", "SUPERSEDED")},
            "recent": sorted(listed, key=lambda f: -f["created_at"])[:limit]}


def playbooks(graph: Graph) -> list[dict]:
    rows = graph.ro_query("""
        MATCH (p:Playbook)
        OPTIONAL MATCH (p)-[d:DERIVED_FROM]->(src:Episode)
        WITH p, collect(CASE WHEN src IS NULL THEN NULL ELSE
            {episode_id: src.id, subject_id: src.entity_id, case_id: src.case_id, request_id: d.request_id,
             approved_by: d.approved_by, at: d.at} END) AS learned_from
        OPTIONAL MATCH (u:Episode)-[:USED_PLAYBOOK]->(p)
        RETURN p {.id, .family, .uses, .created_at, .last_used_at, .required_patterns, .steps}, learned_from,
               collect(CASE WHEN u IS NULL THEN NULL ELSE
                   {episode_id: u.id, subject_id: u.entity_id, verdict: u.verdict, worker_id: u.worker_id} END)
    """).result_set
    listed = [{**dict(p), "steps": [s["tool"] for s in json.loads(p["steps"])], "learned_from": learned,
               "used_by": used} for p, learned, used in rows]
    return sorted(listed, key=lambda p: p["created_at"])
