"""Real-model runs on the demo subjects, every attempt logged to backend/eval/runs/ (failures included).

Runs on a throwaway copy of the tenant graph, so freeze requests don't accumulate in it.

    python -m backend.eval.live_runs [merch-nova-pay acct-tier1-payroll ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from backend.app import agent
from backend.app import scenario as s
from backend.app.config import get_settings
from backend.app.db import copy_graph, get_db, graph_key

RUNS = Path(__file__).parent / "runs"
EXPECTED = {s.NOVAPAY: "SUSPICIOUS", s.VIP: "CLEAR", s.ISOLATED: "CLEAR", s.UNKNOWN: "INSUFFICIENT"}


def record(entity_id: str, events: list[dict], seconds: float, key: str, started_at: str) -> dict:
    settings = get_settings()
    final = events[-1] if events and events[-1]["type"] == "final" else None
    failure = next((e["message"] for e in events if e["type"] in ("error", "notice") and "failed" in e["message"]), None)
    return {
        "entity_id": entity_id,
        "started_at": started_at,
        "model": settings.llm_model,
        "base_url": settings.llm_base_url,
        "graph": key,
        "seconds": seconds,
        "expected_verdict": EXPECTED.get(entity_id),
        "verdict": final and final["verdict"],
        "planner": final and final["planner"],
        "tool_calls": final and final["tool_calls"],
        "model_answer": final and final["model_answer"],
        "failure": failure,
        "events": events,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("subjects", nargs="*", default=list(EXPECTED))
    args = parser.parse_args()
    settings = get_settings()
    if not settings.llm_enabled:
        raise SystemExit("No LLM configured: set LLM_API_KEY in .env.")

    key = f"fintrace:live-{uuid.uuid4().hex[:6]}"
    copy_graph(graph_key(settings.default_tenant), key)
    RUNS.mkdir(parents=True, exist_ok=True)
    try:
        for entity_id in args.subjects:
            started_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            started = time.perf_counter()
            events = asyncio.run(agent.collect(entity_id, graph_key=key, mode="llm"))
            run = record(entity_id, events, round(time.perf_counter() - started, 2), key, started_at)
            path = RUNS / f"{started_at}-{entity_id}.json"
            path.write_text(json.dumps(run, indent=1, default=str))
            proposal = (run["model_answer"] or {}).get("proposed_verdict")
            print(f"{entity_id}: {run['verdict']} (expected {run['expected_verdict']}, model proposed {proposal}) "
                  f"via {run['planner']}, {run['tool_calls']} calls, {run['seconds']} s"
                  + (f" | {run['failure']}" if run["failure"] else "") + f" -> {path.name}")
    finally:
        get_db().select_graph(key).delete()


if __name__ == "__main__":
    main()
