"""FinTrace API: streams investigations over SSE and serves the built UI.

    uvicorn backend.app.main:app --port 8000
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError
from sse_starlette.sse import EventSourceResponse

from backend.app import agent, memory
from backend.app import queries as q
from backend.app import scenario as s
from backend.app import tools as t
from backend.app.config import FRONTEND_DIST, get_settings
from backend.app.db import get_db, graph_key

app = FastAPI(title="FinTrace", version="0.2.0")

SCENARIOS = [
    {"id": s.NOVAPAY, "label": "NovaPay merchant alert",
     "expect": "Five hops to a sanctioned syndicate through a shared device"},
    {"id": s.VIP, "label": "Enterprise payroll",
     "expect": "Shares a proxy IP with a mule: association, not exposure"},
    {"id": s.ISOLATED, "label": "Isolated account", "expect": "Nothing linked, and the agent says why that isn't proof"},
    {"id": s.UNKNOWN, "label": "Unknown ID", "expect": "Stops after one call instead of guessing"},
]


class Approval(BaseModel):
    approver_id: str


def _key() -> str:
    return graph_key(get_settings().default_tenant)


def _graph():
    return get_db().select_graph(_key())


def _entity_id(value: str) -> str:
    value = value.strip()
    if not t.ID_RE.match(value):
        raise HTTPException(422, f"{value!r} is not a valid entity ID")
    return value


@app.exception_handler(RedisConnectionError)
async def falkordb_down(_, exc: RedisConnectionError) -> JSONResponse:
    return JSONResponse({"detail": f"FalkorDB is unreachable: {exc}"}, status_code=503)


@app.get("/api/health")
def health() -> dict:
    settings = get_settings()
    graph = _graph()
    return {
        "status": "ok",
        "graph": _key(),
        "nodes": graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
        "dataset": q.dataset_meta(graph),
        "llm": {"enabled": settings.llm_enabled, "model": settings.llm_model if settings.llm_enabled else None},
        "max_steps": agent.MAX_STEPS,
    }


@app.get("/api/scenarios")
def scenarios() -> list[dict]:
    return SCENARIOS


@app.get("/api/investigate")
async def investigate(entity_id: str, mode: Literal["auto", "llm", "fallback"] = "auto",
                      max_steps: int = Query(agent.MAX_STEPS, ge=1, le=agent.MAX_STEPS),
                      memory: Literal["on", "procedures", "off"] = "on") -> EventSourceResponse:
    eid = _entity_id(entity_id)

    async def events():
        # Unnamed messages: a named "error" event would collide with EventSource's own error event.
        async for event in agent.investigate(eid, graph_key=_key(), worker_id="ui-investigator", mode=mode,
                                             max_steps=max_steps, memory_mode=memory):
            yield {"data": json.dumps(event, default=str)}

    return EventSourceResponse(events())


@app.get("/api/explore")
def explore(entity_id: str, depth: int = Query(1, ge=1, le=t.MAX_DEPTH)) -> dict:
    return t.explore_neighborhood(t.ToolContext(_graph(), worker_id="ui"), entity_id=_entity_id(entity_id), depth=depth)


@app.get("/api/entities/{entity_id}")
def entity(entity_id: str) -> dict:
    return t.resolve_entity(t.ToolContext(_graph(), worker_id="ui"), entity_id=_entity_id(entity_id))


@app.get("/api/approvals")
def approvals(status: Literal["PENDING_L3", "APPROVED", "STALE"] | None = None) -> list[dict]:
    return q.list_approvals(_graph(), status=status)


@app.post("/api/approvals/{request_id}/approve")
def approve(request_id: str, body: Approval) -> dict:
    graph, rid = _graph(), _entity_id(request_id)
    try:
        approved = q.approve_action(graph, rid, _entity_id(body.approver_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {**approved, "playbook": memory.learn_playbook(graph, rid)}


class UI(StaticFiles):
    """Hashed assets cache normally; index.html must revalidate so a redeploy is picked up."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"
        return response


if FRONTEND_DIST.is_dir():
    app.mount("/", UI(directory=FRONTEND_DIST, html=True), name="ui")
