"""The investigation agent: plans FinTrace's MCP tools, at most MAX_STEPS calls per alert.

The harness always resolves the alert entity and checks memory first, so an unknown ID never reaches
the model. An LLM (any OpenAI-compatible endpoint) then picks tools turn by turn. Without an LLM key, or
if the LLM fails mid-run, a deterministic planner continues from the results so far. Either way the
verdict and the analyst's card come from typed facts in the tool results (guardrails.py); the model's
own statements are bound to those facts or rejected, and its rationale is shown as unverified.

    python -m backend.app.agent merch-nova-pay [--mode fallback] [--graph fintrace:acme-pay]
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp import Client
from mcp.types import CallToolResult

from backend.app import guardrails as gr
from backend.app import memory
from backend.app import queries as q
from backend.app.config import Settings, get_settings
from backend.app.contracts import PRESCRIBED_DEPTH, VERDICT_PREDICATES, current_rev
from backend.app.db import get_db
from backend.app.db import graph_key as tenant_graph_key
from backend.app.guardrails import EvidenceLedger, Verdict
from backend.app.mcp_server import build_server

MAX_STEPS = 12
RESULT_CHARS = 12_000
RING_TARGET_LIMIT = 25
BROKER_SCOPE_LIMIT = 50

Mode = Literal["auto", "llm", "fallback"]
Emit = Callable[[dict], Awaitable[None]]

SYSTEM_PROMPT = """\
You are FinTrace, an anti-money-laundering investigator. You work only through the tools, which query a \
FalkorDB graph of accounts and their transfers, the devices and IPs they log in from, and the company's own \
employees, policies, tickets and approval decisions. The alert entity has already been resolved and its memory \
checked: those results are in the alert message.

Rules:
- Memory: facts that recall_memory marks CURRENT still hold, so use them without re-running their tools. Each \
STALE fact says why, and under rerun gives the call that produced it: repeat that call. If a route or a finding of \
no route is STALE, investigate again from the start.
- Exposure means money from the alert entity could reach a sanctioned entity. explore_neighborhood reports \
these as eligible_routes and says how much of the graph each search covered. Anything reached only by ignoring \
transfer direction, or through a shared IP, is association, not exposure.
- An account can be cleared only by an eligible-route search that completed {depth} hops or exhausted everything \
reachable. A capped, timed-out or shallower search clears nobody.
- When an eligible route exists, finish the case before answering:
  1. Follow the money in time order (trace_money_flow) from the account where the route's last run of transfers \
begins: if the route crosses a shared device, the account just after the device.
  2. Bound how much could reach the sanctioned account (exposure_flow), using the accounts on both sides of each \
shared device on the route as sources.
  3. Check whether that account sits in a small ring (detect_ring) and which member brokers it (find_brokers with \
the ring's members).
  4. Check the company's own decisions about the alert entity, and who may approve a freeze (trace_governance).
  5. Run freeze_impact on the accounts you would freeze: the alert entity and every account on the route and the \
money trail except the sanctioned one, plus the members of a small ring. Leave out the legitimate customers and \
shared infrastructure it reports, and add any account it reports as also exposed.
  6. File the freeze with request_action: case_id {case_id}, the freeze policy, an on-call approver with the \
required clearance, the accounts to freeze and the ones left out. Nothing is frozen until a human signs off. Never \
target a sanctioned account; it is already blocked.
- You have {max_steps} tool calls in total, including the ones already made.

Final answer: reply with only this JSON object, no prose around it:
{{"verdict": "SUSPICIOUS" | "CLEAR" | "INSUFFICIENT",
 "statements": [{{"subject": "<id>", "predicate": "<predicate>", "object": "<id or null>", "value": <number, string or null>}}],
 "freeze_targets": ["<id>"], "excluded": ["<id>"],
 "rationale": "2 to 4 sentences for an analyst"}}
Statement predicates:
{predicates}
Amounts are US dollars. The analyst's card is written from the tool results, not from your answer: each statement \
is matched to the tool result it restates or rejected, and the rationale is shown as unverified."""


def system_prompt(case_id: str, max_steps: int) -> str:
    predicates = "\n".join(f"- {name}: {desc}" for name, desc in gr.MODEL_PREDICATES.items())
    return SYSTEM_PROMPT.format(case_id=case_id, max_steps=max_steps, depth=PRESCRIBED_DEPTH, predicates=predicates)


@dataclass
class Investigation:
    entity_id: str
    case_id: str
    max_steps: int
    worker_id: str
    planner: str
    client: Client
    emit: Emit
    rev: int | None = None
    results: list[dict] = field(default_factory=list)
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    llm_ms: list[float] = field(default_factory=list)

    @property
    def steps_left(self) -> int:
        return self.max_steps - len(self.results)

    @property
    def unknown(self) -> bool:
        return any(r["stop"] == "unknown_entity" and r["args"].get("entity_id") == self.entity_id
                   for r in self.results)

    def latest(self, tool: str) -> dict | None:
        return next((r["data"] for r in reversed(self.results) if r["tool"] == tool and r["ok"]), None)

    async def notice(self, message: str) -> None:
        await self.emit({"type": "notice", "message": message})

    async def call(self, tool: str, args: dict, thought: str | None = None) -> dict:
        step = len(self.results) + 1
        await self.emit({"type": "tool_call", "step": step, "tool": tool, "args": args, "thought": thought,
                         "planner": self.planner, "worker_id": self.worker_id})
        try:
            envelope = parse_result(tool, args, await self.client.call_tool(tool, args))
        except Exception as exc:  # transport failure: recorded as a failed call like any tool error
            envelope = failed(tool, args, f"{type(exc).__name__}: {exc}")
        self.results.append(envelope)
        if envelope["ok"]:
            self.ledger.add(envelope["evidence"])
        await self.emit({"type": "tool_result", "step": step, **envelope})
        return envelope

    async def ensure(self, tool: str, args: dict, thought: str) -> dict | None:
        """Data from a successful call with these arguments; calls the tool only if it hasn't run yet."""
        for r in self.results:
            if r["tool"] == tool and r["ok"] and all(r["args"].get(k) == v for k, v in args.items()):
                return r["data"]
        if self.steps_left <= 0:
            return None
        envelope = await self.call(tool, args, thought)
        return envelope["data"] if envelope["ok"] else None


def failed(tool: str, args: dict, error: str) -> dict:
    return {"tool": tool, "ok": False, "args": args, "data": None, "evidence": {"nodes": {}, "edges": []},
            "error": error, "stop": None, "meta": {"elapsed_ms": 0.0, "rows": 0, "queries": []}}


def parse_result(tool: str, args: dict, result: CallToolResult) -> dict:
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    if result.is_error:
        return failed(tool, args, text or "tool call failed")
    payload = result.structured_content if result.structured_content is not None else json.loads(text)
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]
    return payload


def for_model(envelope: dict) -> str:
    payload = {"ok": envelope["ok"], "error": envelope["error"], "stop": envelope["stop"],
               "data": envelope["data"], "evidence_edges": envelope["evidence"]["edges"]}
    text = json.dumps(payload, default=str, separators=(",", ":"))
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + " ...(truncated)"


def parse_answer(text: str | None) -> dict:
    text = (text or "").strip()
    if match := re.search(r"\{.*\}", text, re.S):
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {"summary": text}


def _verdict(value: Any) -> Verdict | None:
    try:
        return Verdict(str(value).upper())
    except ValueError:
        return None


def replay(call: Any) -> dict:
    """A tool call as the model sent it, with provider fields kept: Gemini 3 rejects a follow-up request whose
    tool calls lost their thought signature (extra_content)."""
    extra = getattr(call, "model_extra", None) or {}
    return {"id": call.id, "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments}, **extra}


async def llm_plan(inv: Investigation, settings: Settings) -> dict:
    from openai import AsyncOpenAI

    llm = AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=60, max_retries=1)
    try:
        return await _converse(inv, settings, llm)
    finally:
        await llm.close()


async def _converse(inv: Investigation, settings: Settings, llm: Any) -> dict:
    listed = await inv.client.list_tools()
    specs = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                               "parameters": t.input_schema}} for t in listed.tools]
    # The harness's own calls go in the alert text: providers that sign tool calls reject unsigned injected ones.
    harness = "\n".join(f"{r['tool']}({json.dumps(r['args'])}) returned: {for_model(r)}" for r in inv.results)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt(inv.case_id, inv.max_steps)},
        {"role": "user", "content": f"Alert: investigate {inv.entity_id}.\n\nAlready run:\n{harness}"},
    ]

    for _ in range(inv.max_steps * 2):
        out_of_calls = inv.steps_left <= 0
        if out_of_calls:
            messages.append({"role": "user", "content": f"All {inv.max_steps} tool calls are used. "
                                                        "Reply now with the final JSON object."})
        tool_args = {} if out_of_calls else {"tools": specs, "tool_choice": "auto"}
        asked = time.perf_counter()
        response = await llm.chat.completions.create(model=settings.llm_model, messages=messages, temperature=0,
                                                     **tool_args)
        inv.llm_ms.append(round((time.perf_counter() - asked) * 1000, 1))
        message = response.choices[0].message
        calls = [c for c in message.tool_calls or [] if getattr(c, "function", None)]
        if not calls or out_of_calls:
            return parse_answer(message.content)
        messages.append({"role": "assistant", "content": message.content, "tool_calls": [replay(c) for c in calls]})
        thought = (message.content or "").strip() or None
        for c in calls:
            if inv.steps_left <= 0:
                content = json.dumps({"ok": False, "error": "skipped: tool budget exhausted"})
            else:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = None
                if isinstance(args, dict):
                    content = for_model(await inv.call(c.function.name, args, thought))
                    thought = None
                else:
                    content = json.dumps({"ok": False, "error": "arguments must be a JSON object"})
            messages.append({"role": "tool", "tool_call_id": c.id, "content": content})
    raise RuntimeError("the model kept calling tools without answering")


async def refresh_stale(inv: Investigation, remembered: dict) -> None:
    """Repeat only the calls behind stale remembered facts; a stale broker ranking uses the refreshed ring."""
    calls: dict[str, tuple[dict, str]] = {}
    for f in remembered["facts"]:
        if f["status"] == "STALE":
            calls.setdefault(json.dumps(f["rerun"], sort_keys=True), (f["rerun"], f["why_stale"]))
    for call, why in calls.values():
        args = dict(call["args"])
        ring = inv.latest("detect_ring")
        if call["tool"] == "find_brokers" and ring:
            args["community_ids"] = [m["id"] for m in ring["members"]]
        await inv.ensure(call["tool"], args, f"Re-run {call['tool']}: the remembered result went stale ({why}).")


async def fallback_plan(inv: Investigation) -> None:
    """A fixed investigative order: widen, then follow money, ring, governance and collateral to a freeze request."""
    e = inv.entity_id
    remembered = inv.latest("recall_memory") or {"facts": []}
    verdicts = [f for f in remembered["facts"] if f["predicate"] in VERDICT_PREDICATES]
    if verdicts and all(f["status"] == "CURRENT" for f in verdicts):
        await refresh_stale(inv, remembered)
        return
    near = await inv.ensure("explore_neighborhood", {"entity_id": e, "depth": 1},
                            "Start with direct counterparties and devices.")
    if not near or near["total_reached"] == 0:
        return
    deep = await inv.ensure("explore_neighborhood", {"entity_id": e, "depth": PRESCRIBED_DEPTH},
                            f"Widen to {PRESCRIBED_DEPTH} hops, and search the routes money could actually take.")
    if not deep:
        return
    routes = deep["eligible_routes"]["routes"]
    if not routes:
        if not deep["eligible_routes"]["scope"]["complete"]:
            await inv.notice("The eligible-route search did not finish, so the account cannot be cleared.")
        elif any("Sanctioned" in h["entity"]["labels"] for h in deep["flagged"]):
            await inv.notice("A sanctioned entity is within reach, but no eligible route leads there, so the "
                             "investigation stops at association.")
        return

    hit = min(routes, key=lambda r: r["hops"])
    target, nodes = hit["entity"]["id"], hit["path"]["nodes"]
    devices = [h for h in hit["identity_hops"] if h["type"] == "Device"]
    bridge = devices[-1] if devices else None
    if bridge:
        start, sources = nodes[nodes.index(bridge["id"]) + 1], bridge["accounts_on_path"]
    else:
        start, sources = e, [e]
    flow = await inv.ensure("trace_money_flow", {"entity_id": start, "direction": "out", "max_hops": 4},
                            f"Follow {start}'s money in time order toward {target}."
                            + (f" The path reaches it through shared device {bridge['id']}." if bridge else ""))
    exposure = await inv.ensure("exposure_flow", {"source_ids": sources, "target_id": target},
                                f"Measure how much could reach {target} from {', '.join(sources)}.")
    ring = await inv.ensure("detect_ring", {"entity_id": start}, f"Check whether {start} sits in a small ring.")
    if ring and ring["community_size"] <= BROKER_SCOPE_LIMIT:
        await inv.ensure("find_brokers", {"entity_id": start, "community_ids": [m["id"] for m in ring["members"]]},
                         "Find which ring member the money has to pass through.")
    governance = await inv.ensure("trace_governance", {"entity_id": e},
                                  f"Check who approved what for {e}, and who may approve a freeze.")
    targets = freeze_targets(inv, hit, flow, ring)
    if not targets:
        return
    impact = await inv.ensure("freeze_impact", {"target_ids": targets},
                              "Check which legitimate customers a freeze would also hit.")
    if not governance or not impact:
        return
    policy, approvers = governance["freeze_policy"], governance["on_call_approvers"]
    if not policy or not approvers:
        await inv.notice("No freeze policy or on-call approver with the required clearance was found, "
                         "so no freeze was requested.")
        return
    excluded = [c["id"] for c in impact["collateral"]]
    excluded += [i["id"] for i in impact["infrastructure"] if not i["safe_to_block"]]
    reason = (f"{e} has an eligible route to sanctioned {target} in {hit['hops']} hops"
              + (f" through shared device {bridge['id']}" if bridge else "")
              + (f"; up to ${exposure['max_flow_usd']:,.0f} could reach it" if exposure else "") + ".")
    approver = approvers[0]
    await inv.ensure("request_action", {"target_ids": targets, "policy_id": policy["id"],
                                        "approver_id": approver["id"], "reason": reason, "case_id": inv.case_id,
                                        "excluded_ids": excluded, "action_type": "FREEZE"},
                     f"Route the freeze to {approver['name']}, on call with {approver['clearance']} clearance.")


def freeze_targets(inv: Investigation, hit: dict, flow: dict | None, ring: dict | None) -> list[str]:
    candidates = [inv.entity_id, *hit["path"]["nodes"]]
    if flow:
        candidates += [t[k] for s in flow["sanctioned"] for t in s["trail"] for k in ("from", "to")]
    if ring and ring["community_size"] <= RING_TARGET_LIMIT:
        candidates += [m["id"] for m in ring["members"]]
    return [c for c in dict.fromkeys(candidates)
            if "Account" in inv.ledger.labels(c) and "Sanctioned" not in inv.ledger.labels(c)]


def model_answer(answer: dict, verdict: Verdict, facts: list, inv: Investigation) -> dict:
    proposed = _verdict(answer.get("verdict"))
    rationale = str(answer.get("rationale") or answer.get("summary") or "").strip()
    return {
        "proposed_verdict": proposed.value if proposed else None,
        "agrees": proposed == verdict,
        "rationale": rationale,
        "rationale_check": gr.check_text(rationale, inv.ledger, answer.get("claims"), given=[inv.entity_id]).to_json(),
        "statements": gr.bind(answer.get("statements"), facts),
    }


def finalize(inv: Investigation, answer: dict | None, model: str | None, started: float,
             statuses: dict[str, str] | None = None) -> dict:
    facts = gr.extract_facts(inv.results, inv.rev)
    facts += gr.status_read_facts(facts, statuses or {}, inv.rev)
    state, verdict, basis = gr.derive(facts, inv.unknown)
    statements = gr.render(inv.entity_id, state, verdict, facts, inv.ledger)

    request = inv.latest("request_action")
    if request:
        request = {**request, "status": (statuses or {}).get(request["request_id"], request["status"])}
        targets, excluded = request["target_ids"], request["excluded_ids"]
    else:
        known = lambda ids: [i for i in ids if isinstance(i, str) and i in inv.ledger.nodes]  # noqa: E731
        targets = known((answer or {}).get("freeze_targets") or [])
        excluded = known((answer or {}).get("excluded") or [])
    explores = [r["data"] for r in inv.results if r["ok"] and r["tool"] == "explore_neighborhood"]
    deepest = max(explores, key=lambda d: d["depth"]) if explores else None
    return {
        "type": "final",
        "entity_id": inv.entity_id,
        "case_id": inv.case_id,
        "answer_state": state.value,
        "verdict": verdict.value,
        "verdict_basis": basis,
        "statements": [s.to_json() for s in statements],
        "summary": " ".join(s.text for s in statements),
        "badge": f"{gr._count(len(statements), 'statement')}, each rendered from a cited tool result",
        "facts": [f.to_json() for f in facts],
        "claims": gr.claims_from_facts(facts),
        "model_answer": model_answer(answer, verdict, facts, inv) if answer is not None else None,
        "freeze_targets": targets,
        "excluded": excluded,
        "approval_request": request,
        "coverage": {"depth": deepest["depth"], "exploration": deepest["scope"],
                     "eligible": deepest["eligible_routes"]["scope"]} if deepest else None,
        "evidence_rev": inv.rev,
        "tool_calls": len(inv.results),
        "max_steps": inv.max_steps,
        "budget_exhausted": inv.steps_left <= 0,
        "planner": inv.planner,
        "model": model,
        "llm_ms": inv.llm_ms,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


async def _run(emit: Emit, entity_id: str, key: str, worker_id: str, max_steps: int, mode: Mode,
               case_id: str | None, use_memory: bool) -> None:
    settings = get_settings()
    use_llm = mode == "llm" or (mode == "auto" and settings.llm_enabled)
    started = time.perf_counter()
    graph = get_db().select_graph(key)
    case_id = case_id or f"case-{uuid.uuid4().hex[:8]}"
    start_rev = await asyncio.to_thread(current_rev, graph)
    if use_memory:
        await asyncio.to_thread(memory.open_case, graph, case_id, entity_id, worker_id)
    async with Client(build_server(key, worker_id)) as client:
        inv = Investigation(entity_id=entity_id, case_id=case_id, max_steps=max_steps, worker_id=worker_id,
                            planner="llm" if use_llm else "fallback", client=client, emit=emit, rev=start_rev)
        model = settings.llm_model if use_llm else None
        await emit({"type": "run_started", "entity_id": entity_id, "case_id": inv.case_id, "graph": key,
                    "planner": inv.planner, "model": model, "max_steps": max_steps, "worker_id": worker_id})
        resolved = await inv.call("resolve_entity", {"entity_id": entity_id},
                                  "Resolve the alert entity before reasoning about it.")
        answer = None
        if not resolved["ok"]:
            await inv.notice(f"Could not resolve {entity_id}: {resolved['error']}")
        elif not inv.unknown:
            if use_memory:
                await inv.call("recall_memory", {"entity_id": entity_id}, "Check memory for earlier findings.")
            if use_llm and not settings.llm_enabled:
                await inv.notice("No LLM is configured (set LLM_API_KEY in .env), so the deterministic planner runs.")
                use_llm, model, inv.planner = False, None, "fallback"
            if use_llm:
                try:
                    answer = await llm_plan(inv, settings)
                except Exception as exc:
                    await inv.notice(f"The LLM failed ({type(exc).__name__}: {str(exc)[:200]}). The deterministic "
                                     f"planner continues from the {len(inv.results)} tool results so far.")
                    inv.planner = "llm+fallback"
                    await fallback_plan(inv)
            else:
                await fallback_plan(inv)
        requests = [r["data"]["request_id"] for r in inv.results if r["ok"] and r["tool"] == "request_action"]
        statuses = await asyncio.to_thread(q.approval_statuses, graph, requests) if requests else {}
        final = finalize(inv, answer, model, started, statuses)
        final["memory"] = None
        if use_memory:
            request = inv.latest("request_action")
            if request:
                basis = [f for f in final["facts"] if f["id"] in final["verdict_basis"]]
                await asyncio.to_thread(memory.attach_request_evidence, graph, request["request_id"], start_rev,
                                        request["target_ids"] + [d for f in basis for d in f["depends_on"]])
            final["memory"] = await asyncio.to_thread(
                memory.persist_run, graph, case_id=case_id, entity_id=entity_id, worker_id=worker_id,
                planner=inv.planner, model=model, start_rev=start_rev, results=inv.results, final=final)
        await emit(final)


async def investigate(entity_id: str, *, graph_key: str | None = None, worker_id: str = "investigator",
                      max_steps: int = MAX_STEPS, mode: Mode = "auto", case_id: str | None = None,
                      use_memory: bool = True) -> AsyncIterator[dict]:
    """Stream investigation events: run_started, tool_call, tool_result, notice, then final (or error)."""
    key = graph_key or tenant_graph_key(get_settings().default_tenant)
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def produce() -> None:
        try:
            await _run(queue.put, entity_id.strip(), key, worker_id, max(1, min(max_steps, MAX_STEPS)), mode, case_id,
                       use_memory)
        except Exception as exc:
            await queue.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            await queue.put(None)

    # The MCP client lives in its own task so a disconnecting consumer can't close it from another task.
    task = asyncio.create_task(produce())
    try:
        while (event := await queue.get()) is not None:
            yield event
    finally:
        task.cancel()


async def collect(entity_id: str, **kwargs: Any) -> list[dict]:
    return [event async for event in investigate(entity_id, **kwargs)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run one FinTrace investigation and print its events.")
    parser.add_argument("entity_id")
    parser.add_argument("--graph", default=None)
    parser.add_argument("--mode", choices=["auto", "llm", "fallback"], default="auto")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    cli = parser.parse_args()
    for event in asyncio.run(collect(cli.entity_id, graph_key=cli.graph, mode=cli.mode, max_steps=cli.max_steps)):
        if event["type"] == "tool_call":
            print(f"  {event['step']:>2}. {event['tool']}({json.dumps(event['args'])[:140]})  # {event['thought'] or ''}")
        elif event["type"] == "tool_result":
            status = "ok" if event["ok"] else f"ERROR {event['error'][:160]}"
            print(f"      -> {status} in {event['meta']['elapsed_ms']} ms" + (f" [stop: {event['stop']}]" if event["stop"] else ""))
        elif event["type"] == "final":
            print(json.dumps({k: v for k, v in event.items() if k != "type"}, indent=2, default=str))
        else:
            print(event)
