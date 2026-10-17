"""The lookup baseline: what an investigator who can only look up one entity at a time establishes on the same
graph, with the same call budget and verdict rule as the agent.

lookup_entity returns one entity and one page of its direct relationships: transfers (one row per counterparty,
the FLOWS_TO summaries the agent's route search follows), the devices and IPs an account logs in from, or the
accounts that use a device or IP. Rows carry the neighbour's labels, so a sanctioned account shows up in the page
of whoever pays it, and a direction filter lets the caller page through only what a route can follow.

The verdict comes from the lookups made, under the agent's rule: an eligible route within PRESCRIBED_DEPTH hops
is SUSPICIOUS; CLEAR needs every account and device on eligible routes within that depth listed in full;
anything less is INSUFFICIENT. Two planners use it: a breadth-first search over eligible relationships, and the
agent's LLM with the same budget, pacing and output rules.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from falkordb import Graph

from backend.app import agent
from backend.app import queries as q
from backend.app.config import get_settings
from backend.app.contracts import PRESCRIBED_DEPTH

PAGE_SIZE = 25
DIRECTIONS = ("out", "in", "both")
_NAME = "coalesce({v}.name, {v}.title, {v}.summary, {v}.address, {v}.os)"
LOOKUP = f"""
MATCH (e:Entity {{id: $id}})
OPTIONAL MATCH (e)-[r:FLOWS_TO|USED_DEVICE|LOGGED_FROM]-(n:Entity)
WHERE $direction = 'both' OR (startNode(r) = e) = ($direction = 'out')
WITH e, r, n ORDER BY type(r), n.id
WITH e, collect(CASE WHEN r IS NULL THEN NULL ELSE {{id: n.id, name: {_NAME.format(v="n")}, labels: labels(n),
     rel: type(r), out: startNode(r) = e, amount: r.amount, transfers: r.transfers}} END) AS rows
RETURN {_NAME.format(v="e")}, labels(e), size(rows), rows[$skip..$end]
"""

SPEC = {"type": "function", "function": {
    "name": "lookup_entity",
    "description": (
        "One entity and one page of its direct relationships. For an account: transfers, one row per counterparty "
        "with the total and number of transfers, and the devices and IPs it logs in from. For a device or IP: the "
        "accounts that use it. Each row gives the neighbour's labels, including Sanctioned. direction 'out' lists "
        "transfers the entity sent and the devices and IPs it uses; 'in' lists transfers it received, or the "
        f"accounts using a device or IP; 'both' lists everything. {PAGE_SIZE} rows per page; has_more says whether "
        "another page exists."),
    "parameters": {"type": "object", "properties": {
        "entity_id": {"type": "string", "description": "Entity ID"},
        "direction": {"type": "string", "enum": list(DIRECTIONS), "description": "Which relationships to list"},
        "page": {"type": "integer", "minimum": 0, "description": "Page number, from 0"}},
        "required": ["entity_id"]}}}

PROMPT = """\
You are an anti-money-laundering investigator. You work only through lookup_entity, which reads one entity of a \
graph of accounts and their transfers and the devices and IPs they log in from, with one page of its direct \
relationships. The alert entity has already been looked up: the result is in the alert message.

Rules:
- Exposure means money from the alert entity could reach a sanctioned entity: a route that follows transfers in \
their own direction, and may pass between two accounts through a device both use (account, device, account: two \
hops). Anything reached only by ignoring transfer direction, or through a shared IP, is association, not exposure.
- Routes count up to {depth} hops. An account can be cleared only when every account and device on such routes \
within {depth} hops has been looked up in full, every page, and none of them is sanctioned. A partial search \
clears nobody.
- You have {max_steps} lookups in total, including the one already made. Each page is one lookup.

Final answer: reply with only this JSON object, no prose around it:
{{"verdict": "SUSPICIOUS" | "CLEAR" | "INSUFFICIENT", "route": ["<id>", "..."] or null,
 "rationale": "2 to 4 sentences for an analyst"}}
The verdict on the analyst's card is derived from the lookups you made, under the same rule."""


def _visible(labels: list[str]) -> list[str]:
    return [label for label in labels if label not in q.HIDDEN_LABELS]


def lookup(graph: Graph, entity_id: str, direction: str = "both", page: int = 0) -> dict:
    """lookup_entity as an envelope shaped like the agent's tool results, with the database work it took."""
    args = {"entity_id": entity_id, "direction": direction, "page": page}
    started = time.perf_counter()
    data, error, queries = None, None, []
    if direction not in DIRECTIONS or not isinstance(page, int) or page < 0:
        error = "direction must be out, in or both, and page a whole number from 0"
    else:
        rows = graph.ro_query(LOOKUP, {"id": entity_id, "direction": direction, "skip": page * PAGE_SIZE,
                                       "end": (page + 1) * PAGE_SIZE}).result_set
        queries = [{"cypher": "lookup_entity", "calls": 1, "ms": round((time.perf_counter() - started) * 1000, 1)}]
        if not rows:
            error = f"{entity_id} is not in the graph"
        else:
            name, labels, total, page_rows = rows[0]
            data = {"entity": {"id": entity_id, "name": name, "labels": _visible(labels)}, "direction": direction,
                    "page": page, "total_rows": total, "has_more": (page + 1) * PAGE_SIZE < total,
                    "rows": [{"id": r["id"], "name": r["name"], "labels": _visible(r["labels"]), "rel": r["rel"],
                              "direction": "out" if r["out"] else "in",
                              **({"amount_usd": r["amount"] / 100, "transfers": r["transfers"]}
                                 if r["rel"] == "FLOWS_TO" else {})}
                             for r in page_rows]}
    return {"tool": "lookup_entity", "ok": error is None, "args": args, "data": data, "error": error, "stop": None,
            "evidence": {"nodes": {}, "edges": []},
            "meta": {"elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                     "rows": len(data["rows"]) if data else 0, "queries": queries}}


def for_model(envelope: dict) -> str:
    """A lookup as the model receives it, under the agent's result cap."""
    text = json.dumps({"ok": envelope["ok"], "error": envelope["error"], "data": envelope["data"]}, default=str,
                      separators=(",", ":"))
    return text if len(text) <= agent.RESULT_CHARS else text[:agent.RESULT_CHARS] + " ...(truncated)"


def _pages(total: int) -> set[int]:
    return set(range(max(1, math.ceil(total / PAGE_SIZE))))


def _eligible(labels: list[str], row: dict) -> bool:
    if "Account" in labels:
        return row["direction"] == "out" and row["rel"] in ("FLOWS_TO", "USED_DEVICE")
    return "Device" in labels and row["direction"] == "in" and row["rel"] == "USED_DEVICE"


@dataclass
class Lookups:
    """What a series of lookups established: labels seen, eligible next steps, and which lists were read in full."""

    subject: str
    labels: dict[str, list[str]] = field(default_factory=dict)
    successors: dict[str, set[str]] = field(default_factory=dict)
    pages: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    totals: dict[tuple[str, str], int] = field(default_factory=dict)
    unknown: bool = False

    def add(self, envelope: dict) -> None:
        if not envelope["ok"]:
            self.unknown |= (envelope["args"]["entity_id"] == self.subject
                             and "not in the graph" in (envelope["error"] or ""))
            return
        data = envelope["data"]
        entity = data["entity"]["id"]
        self.labels[entity] = data["entity"]["labels"]
        key = (entity, data["direction"])
        self.pages.setdefault(key, set()).add(data["page"])
        self.totals[key] = data["total_rows"]
        for row in data["rows"]:
            self.labels.setdefault(row["id"], row["labels"])
            if _eligible(self.labels[entity], row):
                self.successors.setdefault(entity, set()).add(row["id"])

    def needed(self, entity: str) -> str:
        return "in" if "Device" in self.labels.get(entity, []) else "out"

    def next_page(self, entity: str) -> int | None:
        """The next unread page of the list a route can follow from this entity; None once it is read in full."""
        for direction in (self.needed(entity), "both"):
            key = (entity, direction)
            if key in self.totals and self.pages[key] >= _pages(self.totals[key]):
                return None
        key = (entity, self.needed(entity))
        return min(_pages(self.totals[key]) - self.pages[key]) if key in self.totals else 0


def derive(lookups: Lookups, depth: int = PRESCRIBED_DEPTH) -> dict:
    """The agent's verdict rule applied to what the lookups showed."""
    if lookups.unknown:
        return {"verdict": "INSUFFICIENT", "answer_state": "unknown_entity", "route": None, "reached": 0,
                "unread": 0}
    subject = lookups.subject
    hops, parent, unread = {subject: 0}, {}, []
    queue = deque([subject])
    while queue:
        node = queue.popleft()
        if node != subject and "Sanctioned" in lookups.labels.get(node, []):
            route = [node]
            while route[-1] in parent:
                route.append(parent[route[-1]])
            return {"verdict": "SUSPICIOUS", "answer_state": "answered_with_path", "route": route[::-1],
                    "target": node, "hops": hops[node], "reached": len(hops), "unread": len(unread)}
        if hops[node] >= depth:
            continue
        if lookups.next_page(node) is not None:
            unread.append(node)
        for nxt in sorted(lookups.successors.get(node, ())):
            if nxt not in hops:
                hops[nxt], parent[nxt] = hops[node] + 1, node
                queue.append(nxt)
    verdict = "INSUFFICIENT" if unread else "CLEAR"
    return {"verdict": verdict, "answer_state": "not_in_graph", "route": None, "reached": len(hops),
            "unread": len(unread)}


def verdict_step(subject: str, envelopes: list[dict], depth: int = PRESCRIBED_DEPTH) -> int | None:
    """The lookup after which the final verdict was established; None for INSUFFICIENT."""
    replay = Lookups(subject)
    for n, envelope in enumerate(envelopes, 1):
        replay.add(envelope)
        if derive(replay, depth)["verdict"] != "INSUFFICIENT":
            return n
    return None


def _result(subject: str, envelopes: list[dict], budget: int, depth: int, started: float, **extra: Any) -> dict:
    lookups = Lookups(subject)
    for envelope in envelopes:
        lookups.add(envelope)
    outcome = derive(lookups, depth)
    return {**outcome, "tool_calls": len(envelopes), "verdict_step": verdict_step(subject, envelopes, depth),
            "budget_exhausted": len(envelopes) >= budget and outcome["verdict"] == "INSUFFICIENT",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "envelopes": envelopes, **extra}


def search(graph: Graph, subject: str, budget: int = agent.MAX_STEPS, depth: int = PRESCRIBED_DEPTH) -> dict:
    """Breadth-first over eligible relationships, one page per call, stopping at a route or the budget."""
    started = time.perf_counter()
    lookups, envelopes = Lookups(subject), []

    def call(entity: str, direction: str, page: int) -> dict:
        envelope = lookup(graph, entity, direction, page)
        envelopes.append(envelope)
        lookups.add(envelope)
        return envelope

    if call(subject, "out", 0)["ok"]:
        hops, queue = {subject: 0}, deque([subject])
        while queue and len(envelopes) < budget and derive(lookups, depth)["verdict"] != "SUSPICIOUS":
            node = queue[0]
            if (page := lookups.next_page(node)) is not None:
                call(node, lookups.needed(node), page)
                continue
            queue.popleft()
            for nxt in sorted(lookups.successors.get(node, ())):
                if nxt not in hops:
                    hops[nxt] = hops[node] + 1
                    if hops[nxt] < depth and "Sanctioned" not in lookups.labels.get(nxt, []):
                        queue.append(nxt)
    return _result(subject, envelopes, budget, depth, started, planner="lookup-bfs")


async def llm_search(graph: Graph, subject: str, budget: int = agent.MAX_STEPS,
                     depth: int = PRESCRIBED_DEPTH) -> dict:
    """The agent's model with lookup_entity alone: same budget, pacing, temperature and verdict rule."""
    from openai import AsyncOpenAI

    settings = get_settings()
    started = time.perf_counter()
    envelopes: list[dict] = []
    llm_ms: list[float] = []
    tokens = {"prompt": 0, "completion": 0}
    wait, answer, failure = 0.0, None, None
    first = await asyncio.to_thread(lookup, graph, subject, "out", 0)
    envelopes.append(first)
    if first["ok"]:
        llm = AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=60,
                          max_retries=settings.llm_max_retries)
        messages: list[dict] = [
            {"role": "system", "content": PROMPT.format(depth=depth, max_steps=budget)},
            {"role": "user", "content": f"Alert: investigate {subject}.\n\nAlready run:\nlookup_entity("
                                        f"{json.dumps(first['args'])}) returned: {for_model(first)}"},
        ]
        try:
            for _ in range(budget * 2):
                out_of_calls = len(envelopes) >= budget
                if out_of_calls:
                    messages.append({"role": "user", "content": f"All {budget} lookups are used. "
                                                                "Reply now with the final JSON object."})
                response, waited, ms = await agent.complete(
                    llm, settings, messages=messages,
                    **({} if out_of_calls else {"tools": [SPEC], "tool_choice": "auto"}))
                wait += waited
                llm_ms.append(ms)
                agent.count_tokens(tokens, response)
                message = response.choices[0].message
                calls = [c for c in message.tool_calls or [] if getattr(c, "function", None)]
                if not calls or out_of_calls:
                    answer = agent.parse_answer(message.content)
                    break
                messages.append({"role": "assistant", "content": message.content,
                                 "tool_calls": [agent.replay(c) for c in calls]})
                for c in calls:
                    messages.append({"role": "tool", "tool_call_id": c.id,
                                     "content": await _run_call(graph, c, envelopes, budget)})
            else:
                failure = "the model kept calling tools without answering"
        except Exception as exc:  # recorded with the run; the verdict still comes from the lookups made
            failure = f"The LLM failed ({agent.llm_failure(exc)})"
        finally:
            await llm.close()
    proposed = str((answer or {}).get("verdict") or "").upper() or None
    return _result(subject, envelopes, budget, depth, started, planner="lookup-llm", model=settings.llm_model,
                   model_proposed=proposed, failure=failure,
                   llm={"requests": len(llm_ms), "ms": round(sum(llm_ms), 1), "wait_ms": round(wait * 1000, 1),
                        "tokens": tokens})


async def _run_call(graph: Graph, call: Any, envelopes: list[dict], budget: int) -> str:
    if len(envelopes) >= budget:
        return json.dumps({"ok": False, "error": "skipped: lookup budget exhausted"})
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        args = None
    if call.function.name != "lookup_entity" or not isinstance(args, dict):
        return json.dumps({"ok": False, "error": "call lookup_entity with a JSON object of arguments"})
    page = args.get("page", 0)
    page = int(page) if isinstance(page, (int, float)) or (isinstance(page, str) and page.isdigit()) else -1
    envelope = await asyncio.to_thread(lookup, graph, str(args.get("entity_id") or ""),
                                       str(args.get("direction") or "both"), page)
    envelopes.append(envelope)
    return for_model(envelope)
