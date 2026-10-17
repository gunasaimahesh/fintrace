"""Load the FinTrace demo graph into FalkorDB.

Usage (from the repo root):
    python -m backend.app.seed_data --reset
    python -m backend.app.seed_data --tenant globex-eu --reset
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import defaultdict

import pandas as pd
from falkordb import Graph

from .company import load_company
from .config import DATA_DIR, get_settings
from .contracts import MAX_CAPACITY
from .db import get_db, graph_key
from .scenario import DAY, HOUR, GraphSpec, build_scenario

INDEXES = [("Entity", "id"), ("Account", "id"), ("Alert", "status"), ("Case", "status"), ("Episode", "id"),
           ("SemanticFact", "id"), ("Playbook", "id")]
FIXTURE_VERSION = "base-3"
BATCH = 5_000
WRITE_TIMEOUT_MS = 120_000
DEVICE_SHARE = 0.25
RESIDENTIAL_IP_SHARE = 0.20
DEVICE_OS = ["iOS 18 Safari", "Android 15 Chrome", "Windows 11 Edge", "macOS 15 Safari",
             "Windows 11 Chrome", "Android 14 WebView"]


def create_indexes(graph: Graph) -> None:
    for label, prop in INDEXES:
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as exc:
            if "already indexed" not in str(exc):
                raise


def write(graph: Graph, query: str, rows: list, **params) -> None:
    for i in range(0, len(rows), BATCH):
        graph.query(query, {"rows": rows[i:i + BATCH], **params}, timeout=WRITE_TIMEOUT_MS)


def load_ibm_accounts(graph: Graph, accounts: pd.DataFrame, loaded_at: int) -> None:
    rows = [[r.account_id, r.owner_name, r.bank, r.owner_id, r.owner_kind,
             "enterprise" if r.owner_kind == "Corporation" else "standard", int(r.monthly_volume)]
            for r in accounts.itertuples()]
    write(graph, """
        UNWIND $rows AS r
        CREATE (:Entity:RiskNode:Account {id: r[0], name: r[1], bank: r[2], owner_id: r[3], kind: r[4],
                tier: r[5], monthly_volume: r[6], status: 'ACTIVE', source: 'ibm-aml',
                topology_updated_at: $loaded_at})
    """, rows, loaded_at=loaded_at)


def load_ibm_transfers(graph: Graph, transfers: pd.DataFrame) -> None:
    rows = [[r.src, r.dst, r.tx_id, int(r.amount), int(r.ts), r.channel] for r in transfers.itertuples()]
    write(graph, """
        UNWIND $rows AS r
        MATCH (a:Account {id: r[0]}), (b:Account {id: r[1]})
        CREATE (a)-[:TRANSFERRED {tx_id: r[2], amount: r[3], ts: r[4], channel: r[5], weight: 1}]->(b)
    """, rows)


def build_flow_summary(graph: Graph) -> None:
    """One FLOWS_TO edge per account pair, because `algo.maxFlow` rejects parallel edges.

    `capacity` is whole dollars clamped to [1, 2^31 - 1]: maxFlow also rejects capacity ranges
    it considers too wide (its error says 2^32, but 2^32 - 1 against a minimum of 1 already fails).
    """
    graph.query("""
        MATCH (a:Account)-[t:TRANSFERRED]->(b:Account)
        WITH a, b, toInteger(sum(t.amount)) AS amount, count(t) AS transfers,
             min(t.ts) AS first_ts, max(t.ts) AS last_ts
        WITH a, b, amount, transfers, first_ts, last_ts, toInteger(round(amount / 100.0)) AS dollars
        CREATE (a)-[:FLOWS_TO {amount: amount, transfers: transfers, first_ts: first_ts, last_ts: last_ts,
                               capacity: CASE WHEN dollars < 1 THEN 1 WHEN dollars > $max THEN $max
                                              ELSE dollars END,
                               weight: 1}]->(b)
    """, {"max": MAX_CAPACITY}, timeout=WRITE_TIMEOUT_MS)


def build_identity_layer(accounts: pd.DataFrame, window: tuple[int, int], loaded_at: int,
                         seed: int = 7) -> GraphSpec:
    """Synthetic devices and IPs. Accounts with the same owner share an office IP; the rest is sparse.

    Nothing here looks at laundering labels.
    """
    rng = random.Random(seed)
    spec = GraphSpec()
    start, end = window
    used_addresses: set[str] = {"185.220.101.7"}

    def new_ip(kind: str) -> str:
        while True:
            first = rng.randint(11, 223)
            if first in (10, 127, 169, 172, 185, 192):
                continue
            address = f"{first}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
            if address not in used_addresses:
                used_addresses.add(address)
                ip_id = "ip-" + address.replace(".", "-")
                spec.node("Entity:RiskNode:IP", id=ip_id, address=address, kind=kind,
                          topology_updated_at=loaded_at)
                return ip_id

    def seen() -> tuple[int, int]:
        first_seen = rng.randint(start - 90 * DAY, start)
        return first_seen, rng.randint(start, end)

    owners: dict[str, list[str]] = defaultdict(list)
    for acct, owner in zip(accounts["account_id"], accounts["owner_id"]):
        if owner:
            owners[owner].append(acct)
    in_office: set[str] = set()
    for owner in sorted(owners):
        members = owners[owner]
        if len(members) < 2:
            continue
        ip_id = new_ip("office")
        for acct in members:
            spec.identity("LOGGED_FROM", acct, ip_id, *seen())
            in_office.add(acct)

    for acct in accounts["account_id"]:
        if rng.random() < DEVICE_SHARE:
            dev_id = f"dev-{rng.getrandbits(40):010x}"
            spec.node("Entity:RiskNode:Device", id=dev_id, os=rng.choice(DEVICE_OS),
                      topology_updated_at=loaded_at)
            spec.identity("USED_DEVICE", acct, dev_id, *seen())
        if acct not in in_office and rng.random() < RESIDENTIAL_IP_SHARE:
            spec.identity("LOGGED_FROM", acct, new_ip("residential"), *seen())
    return spec


def load_spec(graph: Graph, spec: GraphSpec) -> None:
    nodes: dict[str, list[dict]] = defaultdict(list)
    for labels, props in spec.nodes:
        nodes[labels].append(props)
    for labels, rows in nodes.items():
        write(graph, f"UNWIND $rows AS r CREATE (n:{labels}) SET n += r", rows)

    edges: dict[str, list[dict]] = defaultdict(list)
    for rel, src, dst, props in spec.edges:
        edges[rel].append({"src": src, "dst": dst, "props": props})
    for rel, rows in edges.items():
        write(graph, f"""
            UNWIND $rows AS r
            MATCH (a:Entity {{id: r.src}}), (b:Entity {{id: r.dst}})
            CREATE (a)-[e:{rel}]->(b) SET e += r.props
        """, rows)


def seed(tenant: str, reset: bool) -> None:
    from .overlay import apply_overlay, write_cases  # the overlay builds on this module

    db = get_db()
    key = graph_key(tenant)
    if key in db.list_graphs():
        if not reset:
            raise SystemExit(f"{key} already exists; pass --reset to rebuild it")
        db.select_graph(key).delete()
    graph = db.select_graph(key)

    manifest = json.loads((DATA_DIR / "slice_manifest.json").read_text())
    anchors = json.loads((DATA_DIR / "scenario_anchors.json").read_text())
    accounts = pd.read_csv(DATA_DIR / "ibm_aml_hi_small_accounts.csv.gz", keep_default_na=False)
    transfers = pd.read_csv(DATA_DIR / "ibm_aml_hi_small_slice.csv.gz")
    window = (int(manifest["ts_range"][0]), int(manifest["ts_range"][1]))
    base_ts = window[1] // DAY * DAY - 2 * DAY + 8 * HOUR
    loaded_at = int(time.time())

    steps = [
        ("indexes", lambda: create_indexes(graph)),
        ("IBM accounts", lambda: load_ibm_accounts(graph, accounts, loaded_at)),
        ("IBM transfers", lambda: load_ibm_transfers(graph, transfers)),
        ("identity layer", lambda: load_spec(graph, build_identity_layer(accounts, window, loaded_at))),
        ("planted scenario", lambda: load_spec(graph, build_scenario(anchors, window, base_ts, loaded_at))),
        ("company sources", lambda: load_company(graph)),
        ("FLOWS_TO summaries", lambda: build_flow_summary(graph)),
        ("meta", lambda: graph.query("""
            CREATE (:Meta {key: 'dataset', source: $source, as_of_ts: $as_of, window_start: $start,
                           scenario_base_ts: $base, loaded_at: $loaded_at, rev: 0, fixture_version: $fixture})
        """, {"source": manifest["source"], "as_of": window[1], "start": window[0], "base": base_ts,
              "loaded_at": loaded_at, "fixture": FIXTURE_VERSION})),
        # After the summaries and Meta: it reads the data window from Meta and summarizes its own transfers.
        ("challenge overlay", lambda: write_cases(apply_overlay(graph), FIXTURE_VERSION)),
    ]
    for name, step in steps:
        started = time.perf_counter()
        step()
        print(f"  {name:<24} {time.perf_counter() - started:6.1f}s")

    nodes = graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
    edges = graph.ro_query("MATCH ()-[e]->() RETURN count(e)").result_set[0][0]
    print(f"Seeded {key}: {nodes:,} nodes, {edges:,} edges")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the FinTrace demo graph into FalkorDB.")
    parser.add_argument("--tenant", default=get_settings().default_tenant)
    parser.add_argument("--reset", action="store_true", help="delete and rebuild the graph if it exists")
    args = parser.parse_args()
    seed(args.tenant, args.reset)


if __name__ == "__main__":
    main()
