"""Shared pieces of the evaluation runners: throwaway graph copies, provenance, measurements and results.json."""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
import subprocess
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app import queries as q
from backend.app.config import get_settings
from backend.app.db import copy_graph, get_db
from backend.app.tenants import pristine_key

EVAL_DIR = Path(__file__).parent
RESULTS = EVAL_DIR / "results.json"
RUNS = EVAL_DIR / "runs"
ROOT = EVAL_DIR.parent.parent
OUTPUTS = ("backend/eval/results.json", "backend/eval/runs", "backend/eval/acceptance.json",
           "backend/tests/checkpoint_report.json")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def source_graph() -> str:
    """The default tenant's pristine snapshot: every trial starts from it, whatever the demo graph has been through."""
    key = pristine_key(get_settings().default_tenant)
    if key not in get_db().list_graphs():
        raise SystemExit(f"{key} is missing: run python -m backend.app.seed_data --reset")
    return key


@contextmanager
def scratch(source: str) -> Iterator[str]:
    """A throwaway GRAPH.COPY of source, deleted afterwards. test- keys stay out of the tenant list."""
    key = f"fintrace:test-eval-{uuid.uuid4().hex[:8]}"
    copy_graph(source, key)
    try:
        yield key
    finally:
        if key in get_db().list_graphs():
            get_db().select_graph(key).delete()


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def provenance(source: str) -> dict:
    graph = get_db().select_graph(source)
    meta = {k: v for k, v in q.dataset_meta(graph).items() if k != "key"}
    return {"graph": source, "dataset": meta,
            "nodes": graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
            "edges": graph.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
            "commit": _git("rev-parse", "--short", "HEAD"),
            "uncommitted_backend_changes": bool(_git("status", "--porcelain", "--", "backend",
                                                     *(f":(exclude){path}" for path in OUTPUTS)))}


def stats(values: list[float | int | None]) -> dict | None:
    """Median and range, the summary the plan asks for; None-valued runs are left out and counted by n."""
    kept = [v for v in values if v is not None]
    if not kept:
        return None
    return {"median": round(statistics.median(kept), 1), "min": min(kept), "max": max(kept), "n": len(kept)}


def db_work(envelopes: list[dict], visible: Callable[[dict], str]) -> dict:
    """Database work behind a run's tool results: Cypher executions, summed round-trip time (not server CPU
    time), entities and edges the results show, and the bytes the planner receives after the result cap."""
    queries = [x for e in envelopes for x in e["meta"]["queries"]]
    return {"queries": sum(x["calls"] for x in queries), "ms": round(sum(x["ms"] for x in queries), 1),
            "entities": sum(len(e["evidence"]["nodes"]) for e in envelopes),
            "edges": sum(len(e["evidence"]["edges"]) for e in envelopes),
            "bytes": sum(len(visible(e).encode()) for e in envelopes)}


def save(path: list[str], value: Any) -> None:
    """Set results.json[path...] = value, keeping every other section."""
    data = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value
    data["updated_at"] = now()
    RESULTS.write_text(json.dumps(data, indent=1, default=str) + "\n")


class RetryCounter(logging.Handler):
    """Counts the OpenAI client's automatic retries (rate limits, server errors), which it otherwise hides."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "Retrying request" in record.getMessage():
            self.count += 1

    def __enter__(self) -> RetryCounter:
        logger = logging.getLogger("openai._base_client")
        logger.addHandler(self)
        logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc: object) -> None:
        logging.getLogger("openai._base_client").removeHandler(self)
