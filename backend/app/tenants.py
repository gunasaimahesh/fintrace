"""One graph per tenant (demo data isolation, not access control), each with a pristine copy to reset from.

Every API call and worker resolves its graph from the tenant name; nothing is shared between tenant graphs.
"""

from __future__ import annotations

from backend.app.contracts import current_rev
from backend.app.db import GRAPH_PREFIX, get_db, tenant_key

PRISTINE_PREFIX = "fintrace-pristine:"


def pristine_key(tenant: str) -> str:
    return PRISTINE_PREFIX + tenant_key(tenant).removeprefix(GRAPH_PREFIX)


def list_tenants(include_tests: bool = False) -> list[str]:
    names = [k.removeprefix(GRAPH_PREFIX) for k in get_db().list_graphs() if k.startswith(GRAPH_PREFIX)]
    return sorted(n for n in names if include_tests or not n.startswith("test-"))


def snapshot(tenant: str) -> str:
    """Copy the tenant's graph to its pristine key, replacing any earlier snapshot."""
    db, pristine = get_db(), pristine_key(tenant)
    if pristine in db.list_graphs():
        db.select_graph(pristine).delete()
    db.select_graph(tenant_key(tenant)).copy(pristine)
    return pristine


def reset(tenant: str) -> dict:
    """Replace the tenant's graph with its pristine copy: memory, queue, approvals, freezes and mutations go."""
    db, key, pristine = get_db(), tenant_key(tenant), pristine_key(tenant)
    if pristine not in db.list_graphs():
        raise LookupError(f"{tenant} has no pristine snapshot; reseed it with python -m backend.app.seed_data")
    if key in db.list_graphs():
        db.select_graph(key).delete()
    db.select_graph(pristine).copy(key)
    graph = db.select_graph(key)
    return {"tenant": tenant, "graph": key, "rev": current_rev(graph),
            "nodes": graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]}
