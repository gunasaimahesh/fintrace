"""FalkorDB connection and graph naming."""

from __future__ import annotations

from functools import lru_cache

from falkordb import FalkorDB, Graph

from .config import get_settings

GRAPH_PREFIX = "fintrace:"


def graph_key(tenant: str) -> str:
    return f"{GRAPH_PREFIX}{tenant}"


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
