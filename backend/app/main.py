"""FinTrace API: streams investigations and hunter runs over SSE and serves the built UI.

Every endpoint takes an optional `tenant` and works only on that tenant's graph (demo data isolation, not
access control).

    uvicorn backend.app.main:app --port 8000
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from redis.exceptions import ConnectionError as RedisConnectionError
from sse_starlette.sse import EventSourceResponse

from backend.app import agent, memory, mutations, tenants
from backend.app import coordination as co
from backend.app import queries as q
from backend.app import scenario as s
from backend.app import tools as t
from backend.app.config import FRONTEND_DIST, get_settings
from backend.app.contracts import ALERT_RULES, MAX_CAPACITY
from backend.app.db import get_db, graph_exists, tenant_key

app = FastAPI(title="FinTrace", version="0.3.0")

SCENARIOS = [
    {"id": s.NOVAPAY, "label": "NovaPay merchant alert",
     "expect": "Five hops to a sanctioned syndicate through a shared device"},
    {"id": s.VIP, "label": "Enterprise payroll",
     "expect": "Shares a proxy IP with a mule: association, not exposure"},
    {"id": s.ISOLATED, "label": "Isolated account", "expect": "Nothing linked, and the agent says why that isn't proof"},
    {"id": s.UNKNOWN, "label": "Unknown ID", "expect": "Stops after one call instead of guessing"},
]
DRAIN_EVENTS = {"claimed", "tool_call", "notice", "alert_done", "swept"}
MUTATIONS = {
    "add_transfer": (mutations.add_transfer, ("src", "dst", "amount_usd")),
    "delete_transfer": (mutations.delete_transfer, ("tx_id",)),
    "add_identity_link": (mutations.add_identity_link, ("account", "target", "rel")),
    "delete_identity_link": (mutations.delete_identity_link, ("account", "target", "rel")),
    "set_sanctioned": (mutations.set_sanctioned, ("account", "sanctioned", "list_name")),
}
ID_FIELDS = {"src", "dst", "tx_id", "account", "target"}

Tenant = Annotated[str | None, Query(description="Tenant whose graph to use; defaults to DEFAULT_TENANT")]
_locks: dict[tuple[str, str], asyncio.Lock] = {}


class Approval(BaseModel):
    approver_id: str


class NewAlert(BaseModel):
    subject_id: str
    rule: Literal[ALERT_RULES]  # type: ignore[valid-type]
    priority: int = Field(3, ge=0, le=9)


class Mutation(BaseModel):
    kind: Literal["add_transfer", "delete_transfer", "add_identity_link", "delete_identity_link", "set_sanctioned"]
    src: str | None = None
    dst: str | None = None
    amount_usd: float | None = Field(None, gt=0, le=MAX_CAPACITY)
    tx_id: str | None = None
    account: str | None = None
    target: str | None = None
    rel: Literal["USED_DEVICE", "LOGGED_FROM"] | None = None
    sanctioned: bool | None = None
    list_name: str | None = None


def _key(tenant: str | None) -> str:
    try:
        key = tenant_key(tenant)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if not graph_exists(key):
        raise HTTPException(404, f"no graph for tenant {tenant or get_settings().default_tenant}")
    return key


def _graph(tenant: str | None):
    return get_db().select_graph(_key(tenant))


def _lock(key: str, purpose: str) -> asyncio.Lock:
    return _locks.setdefault((key, purpose), asyncio.Lock())


def _entity_id(value: str) -> str:
    value = value.strip()
    if not t.ID_RE.match(value):
        raise HTTPException(422, f"{value!r} is not a valid entity ID")
    return value


def _sse(event: dict) -> dict:
    # Unnamed messages: a named "error" event would collide with EventSource's own error event.
    return {"data": json.dumps(event, default=str)}


@app.exception_handler(RedisConnectionError)
async def falkordb_down(_, exc: RedisConnectionError) -> JSONResponse:
    return JSONResponse({"detail": f"FalkorDB is unreachable: {exc}"}, status_code=503)


@app.get("/api/health")
def health(tenant: Tenant = None) -> dict:
    settings = get_settings()
    key = _key(tenant)
    graph = get_db().select_graph(key)
    return {
        "status": "ok",
        "tenant": key.split(":", 1)[1],
        "graph": key,
        "nodes": graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
        "dataset": q.dataset_meta(graph),
        "llm": {"enabled": settings.llm_enabled, "model": settings.llm_model if settings.llm_enabled else None},
        "max_steps": agent.MAX_STEPS,
    }


@app.get("/api/tenants")
def list_tenants() -> dict:
    return {"default": get_settings().default_tenant, "tenants": tenants.list_tenants()}


@app.get("/api/scenarios")
def scenarios() -> list[dict]:
    return SCENARIOS


@app.get("/api/investigate")
async def investigate(entity_id: str, tenant: Tenant = None, mode: Literal["auto", "llm", "fallback"] = "auto",
                      max_steps: int = Query(agent.MAX_STEPS, ge=1, le=agent.MAX_STEPS),
                      memory: Literal["on", "procedures", "off"] = "on") -> EventSourceResponse:
    eid, key = _entity_id(entity_id), _key(tenant)

    async def events():
        async for event in agent.investigate(eid, graph_key=key, worker_id="ui-investigator", mode=mode,
                                             max_steps=max_steps, memory_mode=memory):
            yield _sse(event)

    return EventSourceResponse(events())


@app.get("/api/explore")
def explore(entity_id: str, tenant: Tenant = None, depth: int = Query(1, ge=1, le=t.MAX_DEPTH)) -> dict:
    return t.explore_neighborhood(t.ToolContext(_graph(tenant), worker_id="ui"), entity_id=_entity_id(entity_id),
                                  depth=depth)


@app.get("/api/entities/{entity_id}")
def entity(entity_id: str, tenant: Tenant = None) -> dict:
    return t.resolve_entity(t.ToolContext(_graph(tenant), worker_id="ui"), entity_id=_entity_id(entity_id))


@app.get("/api/memory/{entity_id}")
def recall(entity_id: str, tenant: Tenant = None) -> dict:
    remembered = memory.recall(_graph(tenant), _entity_id(entity_id))
    if remembered is None:
        raise HTTPException(404, f"{entity_id} is not in the graph")
    return remembered


@app.get("/api/approvals")
def approvals(tenant: Tenant = None, status: Literal["PENDING_L3", "APPROVED", "STALE"] | None = None) -> list[dict]:
    return q.list_approvals(_graph(tenant), status=status)


@app.post("/api/approvals/{request_id}/approve")
def approve(request_id: str, body: Approval, tenant: Tenant = None) -> dict:
    graph, rid = _graph(tenant), _entity_id(request_id)
    try:
        approved = q.approve_action(graph, rid, _entity_id(body.approver_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {**approved, "playbook": memory.learn_playbook(graph, rid)}


@app.get("/api/queue")
def queue(tenant: Tenant = None) -> list[dict]:
    return co.queue_state(_graph(tenant))


@app.post("/api/alerts")
def raise_alert(body: NewAlert, tenant: Tenant = None) -> dict:
    try:
        return co.raise_alert(_graph(tenant), _entity_id(body.subject_id), body.rule, body.priority)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/queue/reset")
async def reset_queue(tenant: Tenant = None) -> dict:
    key = _key(tenant)
    if _lock(key, "drain").locked():
        raise HTTPException(409, "hunters are draining this tenant's queue")
    return {"reset": await asyncio.to_thread(co.reset_queue, get_db().select_graph(key))}


@app.get("/api/queue/drain")
async def drain(tenant: Tenant = None, workers: int = Query(3, ge=1, le=6),
                mode: Literal["auto", "llm", "fallback"] = "fallback") -> EventSourceResponse:
    """Hunters drain the queue: their claims, tool calls, notices and outcomes stream as they happen."""
    key = _key(tenant)
    lock = _lock(key, "drain")
    if lock.locked():
        raise HTTPException(409, "hunters are already draining this tenant's queue")

    async def events():
        async with lock:
            stream: asyncio.Queue[dict] = asyncio.Queue()

            async def emit(event: dict) -> None:
                if event["type"] in DRAIN_EVENTS:
                    await stream.put({k: v for k, v in event.items() if k not in ("data", "evidence", "meta")})

            task = asyncio.create_task(co.drain(key, workers, mode=mode, emit=emit))
            try:
                while not (task.done() and stream.empty()):
                    try:
                        yield _sse(await asyncio.wait_for(stream.get(), timeout=0.25))
                    except TimeoutError:
                        continue
                summary = task.result()
                yield _sse({"type": "drained", **{k: v for k, v in summary.items() if k != "queue"}})
            finally:
                task.cancel()

    return EventSourceResponse(events())


@app.post("/api/mutations")
async def mutate(body: Mutation, tenant: Tenant = None) -> dict:
    """Evidence-bearing writes, one at a time per tenant: each bumps the graph revision, so memory that depended
    on what changed goes stale."""
    key = _key(tenant)
    write, fields = MUTATIONS[body.kind]
    if missing := [f for f in fields if getattr(body, f) is None and f != "list_name"]:
        raise HTTPException(422, f"{body.kind} needs {', '.join(missing)}")
    args = [_entity_id(v) if f in ID_FIELDS else round(v * 100) if f == "amount_usd" else v
            for f in fields for v in [getattr(body, f)]]
    async with _lock(key, "mutation"):
        try:
            return {"kind": body.kind, **await asyncio.to_thread(write, get_db().select_graph(key), *args)}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None


@app.post("/api/reset")
async def reset(tenant: Tenant = None) -> dict:
    """Restore the tenant's graph from its pristine copy."""
    key = _key(tenant)
    if _lock(key, "drain").locked():
        raise HTTPException(409, "hunters are draining this tenant's queue")
    async with _lock(key, "mutation"):
        try:
            return await asyncio.to_thread(tenants.reset, key.split(":", 1)[1])
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from None


class UI(StaticFiles):
    """Hashed assets cache normally; index.html must revalidate so a redeploy is picked up."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"
        return response


if FRONTEND_DIST.is_dir():
    app.mount("/", UI(directory=FRONTEND_DIST, html=True), name="ui")
