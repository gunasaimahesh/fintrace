"""FalkorDB connection and graph naming."""

from __future__ import annotations

import re
from functools import lru_cache

from falkordb import FalkorDB, Graph

from .config import get_settings

GRAPH_PREFIX = "fintrace:"
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def graph_key(tenant: str) -> str:
    return f"{GRAPH_PREFIX}{tenant}"


def tenant_key(tenant: str | None = None) -> str:
    """The graph every request and worker for `tenant` must use. Raises ValueError for a malformed name."""
    tenant = tenant or get_settings().default_tenant
    if not TENANT_RE.match(tenant):
        raise ValueError(f"{tenant!r} is not a valid tenant name")
    return graph_key(tenant)


@lru_cache
def get_db() -> FalkorDB:
    s = get_settings()
    return FalkorDB(host=s.falkordb_host, port=s.falkordb_port,
                    username=s.falkordb_username, password=s.falkordb_password)


def get_graph(tenant: str | None = None) -> Graph:
    return get_db().select_graph(graph_key(tenant or get_settings().default_tenant))


def graph_exists(key: str) -> bool:
    return key in get_db().list_graphs()


def memory_usage_mb(key: str) -> dict:
    """`GRAPH.MEMORY USAGE` as a dict of MB figures (per-label and per-type breakdowns nested)."""

    def as_dict(flat: list) -> dict:
        out = {}
        for k, v in zip(flat[::2], flat[1::2]):
            k = k.decode() if isinstance(k, bytes) else k
            out[k] = as_dict(v) if isinstance(v, list) else float(v)
        return out

    return as_dict(get_db().connection.execute_command("GRAPH.MEMORY", "USAGE", key))
