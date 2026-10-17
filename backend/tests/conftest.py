"""Graph tests run on a throwaway GRAPH.COPY of the seeded tenant graph, deleted afterwards. They copy its
pristine snapshot when there is one, so using the demo doesn't change what the tests start from."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from backend.app import seed_data
from backend.app.config import get_settings
from backend.app.db import copy_graph, get_db, graph_key
from backend.app.tenants import pristine_key


def _copy_seeded_graph() -> str:
    tenant = get_settings().default_tenant
    try:
        graphs = get_db().list_graphs()
    except RedisConnectionError:
        pytest.skip("FalkorDB is not running; start it with docker compose up -d")
    source = pristine_key(tenant) if pristine_key(tenant) in graphs else graph_key(tenant)
    if source not in graphs:
        pytest.skip(f"{source} is not loaded; run python -m backend.app.seed_data --reset")
    key = f"fintrace:test-{uuid.uuid4().hex[:8]}"
    copy_graph(source, key)
    return key


@pytest.fixture(scope="session")
def scratch_key() -> Iterator[str]:
    """Shared by tests that only add pending requests; nothing in it is frozen."""
    key = _copy_seeded_graph()
    yield key
    get_db().select_graph(key).delete()


@pytest.fixture
def fresh_key() -> Iterator[str]:
    """A private copy for tests that change account status."""
    key = _copy_seeded_graph()
    yield key
    get_db().select_graph(key).delete()


@pytest.fixture
def other_key() -> Iterator[str]:
    """A second private copy, for comparing two conditions side by side."""
    key = _copy_seeded_graph()
    yield key
    get_db().select_graph(key).delete()


@pytest.fixture
def small_tenant() -> Iterator[str]:
    """A second tenant seeded from the small fixture, with the pristine copy resets restore."""
    name = f"test-small-{uuid.uuid4().hex[:6]}"
    seed_data.seed(name, reset=False, profile="small")
    yield name
    for key in (graph_key(name), pristine_key(name)):
        if key in get_db().list_graphs():
            get_db().select_graph(key).delete()
