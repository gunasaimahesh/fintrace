"""Evidence-bearing writes.

Each one bumps :Meta.rev in the same query and stamps every entity it touched with topology_rev = rev
(before anything is deleted), so memory can tell which conclusions the write may have changed. A transfer
write keeps the raw TRANSFERRED edges and their FLOWS_TO summary consistent: the summary is MERGEd and
recomputed from the raw edges that remain, and removed when none do.
"""

from __future__ import annotations

import time
import uuid

from falkordb import Graph

from backend.app.contracts import MAX_CAPACITY, META_MATCH

_BUMP = META_MATCH + " SET meta.rev = coalesce(meta.rev, 0) + 1"

_RESUMMARIZE = """
    WITH a, b, rev
    OPTIONAL MATCH (a)-[t:TRANSFERRED]->(b)
    WITH a, b, rev, count(t) AS n, sum(t.amount) AS amount, min(t.ts) AS first_ts, max(t.ts) AS last_ts,
         toInteger(round(sum(t.amount) / 100.0)) AS dollars
    MERGE (a)-[f:FLOWS_TO]->(b)
    SET f.amount = amount, f.transfers = n, f.first_ts = first_ts, f.last_ts = last_ts, f.weight = 1,
        f.capacity = CASE WHEN dollars < 1 THEN 1 WHEN dollars > $max THEN $max ELSE dollars END
    WITH rev, n, f
    FOREACH (_ IN CASE WHEN n = 0 THEN [1] ELSE [] END | DELETE f)
    RETURN rev, n
"""


def _one(rows: list, missing: str) -> list:
    if not rows:
        raise ValueError(missing)
    return rows[0]


def add_transfer(graph: Graph, src: str, dst: str, amount_cents: int, ts: int | None = None,
                 tx_id: str | None = None, channel: str = "wire") -> dict:
    tx_id = tx_id or f"tx-{uuid.uuid4().hex[:10]}"
    rev, n = _one(graph.query(f"""
        MATCH (a:Account {{id: $src}}), (b:Account {{id: $dst}})
        {_BUMP}
        WITH a, b, meta.rev AS rev
        CREATE (a)-[:TRANSFERRED {{tx_id: $tx, amount: $amount, ts: $ts, channel: $channel, weight: 1}}]->(b)
        SET a.topology_rev = rev, b.topology_rev = rev
        {_RESUMMARIZE}
    """, {"src": src, "dst": dst, "tx": tx_id, "amount": int(amount_cents), "ts": ts or int(time.time()),
          "channel": channel, "max": MAX_CAPACITY}).result_set, f"no account {src} or {dst}")
    return {"tx_id": tx_id, "rev": rev, "transfers_between": n}


def update_transfer(graph: Graph, tx_id: str, amount_cents: int) -> dict:
    rev, n = _one(graph.query(f"""
        MATCH (a:Account)-[t:TRANSFERRED {{tx_id: $tx}}]->(b:Account)
        {_BUMP}
        WITH a, b, t, meta.rev AS rev
        SET t.amount = $amount, a.topology_rev = rev, b.topology_rev = rev
        {_RESUMMARIZE}
    """, {"tx": tx_id, "amount": int(amount_cents), "max": MAX_CAPACITY}).result_set, f"no transfer {tx_id}")
    return {"tx_id": tx_id, "rev": rev, "transfers_between": n}


def delete_transfer(graph: Graph, tx_id: str) -> dict:
    rev, n = _one(graph.query(f"""
        MATCH (a:Account)-[t:TRANSFERRED {{tx_id: $tx}}]->(b:Account)
        {_BUMP}
        WITH a, b, t, meta.rev AS rev
        SET a.topology_rev = rev, b.topology_rev = rev
        DELETE t
        {_RESUMMARIZE}
    """, {"tx": tx_id, "max": MAX_CAPACITY}).result_set, f"no transfer {tx_id}")
    return {"tx_id": tx_id, "rev": rev, "transfers_between": n}


_IDENTITY = {"USED_DEVICE": "Device", "LOGGED_FROM": "IP"}


def add_identity_link(graph: Graph, account: str, target: str, rel: str, first_seen: int | None = None,
                      last_seen: int | None = None) -> dict:
    now = int(time.time())
    [rev] = _one(graph.query(f"""
        MATCH (a:Account {{id: $account}}), (x:{_IDENTITY[rel]} {{id: $target}})
        {_BUMP}
        WITH a, x, meta.rev AS rev
        MERGE (a)-[l:{rel}]->(x)
        ON CREATE SET l.first_seen = $first, l.last_seen = $last, l.weight = 1
        SET a.topology_rev = rev, x.topology_rev = rev
        RETURN rev
    """, {"account": account, "target": target, "first": first_seen or now, "last": last_seen or now}).result_set,
        f"no account {account} or {_IDENTITY[rel].lower()} {target}")
    return {"rev": rev}


def delete_identity_link(graph: Graph, account: str, target: str, rel: str) -> dict:
    [rev] = _one(graph.query(f"""
        MATCH (a:Account {{id: $account}})-[l:{rel}]->(x:{_IDENTITY[rel]} {{id: $target}})
        {_BUMP}
        WITH a, x, l, meta.rev AS rev
        SET a.topology_rev = rev, x.topology_rev = rev
        DELETE l
        RETURN rev
    """, {"account": account, "target": target}).result_set, f"{account} has no {rel} link to {target}")
    return {"rev": rev}


def set_sanctioned(graph: Graph, account: str, sanctioned: bool, list_name: str | None = None) -> dict:
    label = "SET a:Sanctioned, a.sanctions_list = $list" if sanctioned else "REMOVE a:Sanctioned"
    [rev] = _one(graph.query(f"""
        MATCH (a:Account {{id: $account}})
        {_BUMP}
        WITH a, meta.rev AS rev
        SET a.topology_rev = rev
        {label}
        RETURN rev
    """, {"account": account, "list": list_name}).result_set, f"no account {account}")
    return {"rev": rev}


def delete_entity(graph: Graph, entity_id: str) -> dict:
    """Delete an account, device or IP with its transfers and links; its neighbours are stamped first."""
    rev, neighbours = _one(graph.query(f"""
        MATCH (n:Entity {{id: $id}}) WHERE n:Account OR n:Device OR n:IP
        {_BUMP}
        WITH n, meta.rev AS rev
        OPTIONAL MATCH (n)-[:FLOWS_TO|TRANSFERRED|USED_DEVICE|LOGGED_FROM]-(m:Entity)
        WITH n, rev, collect(DISTINCT m) AS neighbours
        FOREACH (m IN neighbours | SET m.topology_rev = rev)
        DETACH DELETE n
        RETURN rev, size(neighbours)
    """, {"id": entity_id}).result_set, f"no account, device or IP {entity_id}")
    return {"rev": rev, "neighbours_stamped": neighbours}
