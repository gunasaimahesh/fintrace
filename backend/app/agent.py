"""The investigation agent: plans FinTrace's MCP tools, at most MAX_STEPS calls per alert.

The harness always resolves the alert entity and checks memory first, so an unknown ID never reaches
the model. An LLM (any OpenAI-compatible endpoint) then picks tools turn by turn. Without an LLM key, or
if the LLM fails mid-run, a deterministic planner continues from the results so far. Either way the
answer goes through guardrails.verify before it is shown, and the answer state is derived from the tool
results, not taken from the model.

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
from backend.app.config import Settings, get_settings
from backend.app.db import graph_key as tenant_graph_key
from backend.app.guardrails import AnswerState, EvidenceLedger, Verdict
from backend.app.mcp_server import build_server

MAX_STEPS = 12
RESULT_CHARS = 12_000
RING_TARGET_LIMIT = 25
BROKER_SCOPE_LIMIT = 50
INVESTIGATIVE = {"explore_neighborhood", "trace_money_flow", "path_between", "detect_ring", "find_brokers",
                 "exposure_flow", "trace_governance", "freeze_impact"}

Mode = Literal["auto", "llm", "fallback"]
Emit = Callable[[dict], Awaitable[None]]

SYSTEM_PROMPT = """\
You are FinTrace, an anti-money-laundering investigator. You work only through the tools, which query a \
FalkorDB graph of accounts and their transfers, the devices and IPs they log in from, and the company's own \
employees, policies, tickets and approval decisions. The alert entity has already been resolved and its memory \
checked: those are the first tool results below.

How to investigate:
- If memory holds a CURRENT finding, answer from it without more calls. If a finding is STALE, re-investigate.
- Explore depth 1, then depth 5. A sanctioned entity you reach matters only if money from the alert entity could \
follow the path: every transfer must point away from the alert entity, and identity hops must go through a \
shared device. A shared IP is weak (proxies are shared by strangers). Anything else is association, not exposure.
- When there is exposure: trace_money_flow from the account just past the shared device, exposure_flow from the \
accounts on both sides of that device to the sanctioned account, detect_ring and find_brokers on that account, \
then trace_governance on the alert entity.
- Before requesting a freeze, run freeze_impact on the accounts you would freeze and exclude the legitimate \
customers and shared infrastructure it reports. Then call request_action with case_id {case_id}, the freeze \
policy and an on-call approver from trace_governance. Never target a sanctioned account; it is already blocked.
- You have {max_steps} tool calls in total, including the ones already made.

Final answer: reply with only this JSON object, no prose around it:
{{"verdict": "SUSPICIOUS" | "CLEAR" | "INSUFFICIENT",
 "summary": "3 to 6 sentences for an analyst",
 "claims": [{{"subject": "<id>", "relation": "<RELATION>", "object": "<id>"}}],
 "freeze_targets": ["<id>"], "excluded": ["<id>"]}}
Every entity ID in the summary must come from a tool result. Each claim must copy a [subject, relation, object] \
triple from a tool result's evidence_edges (TRANSFERRED and FLOWS_TO are interchangeable). An answer that breaks \
these rules is discarded and replaced by a summary generated from the tool results."""


@dataclass
class Investigation:
    entity_id: str
    case_id: str
    max_steps: int
    worker_id: str
    planner: str
    client: Client
    emit: Emit
    results: list[dict] = field(default_factory=list)
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)

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


async def llm_plan(inv: Investigation, settings: Settings) -> dict:
    from openai import AsyncOpenAI

    llm = AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=60, max_retries=1)
    listed = await inv.client.list_tools()
    specs = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                               "parameters": t.input_schema}} for t in listed.tools]
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(case_id=inv.case_id, max_steps=inv.max_steps)},
        {"role": "user", "content": f"Alert: investigate {inv.entity_id}."},
    ]
    for i, r in enumerate(inv.results):
        call_id = f"call_harness_{i}"
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": r["tool"], "arguments": json.dumps(r["args"])}}]})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": for_model(r)})

    for _ in range(inv.max_steps * 2):
        out_of_calls = inv.steps_left <= 0
        if out_of_calls:
            messages.append({"role": "user", "content": f"All {inv.max_steps} tool calls are used. "
                                                        "Reply now with the final JSON object."})
        tool_args = {} if out_of_calls else {"tools": specs, "tool_choice": "auto"}
        response = await llm.chat.completions.create(model=settings.llm_model, messages=messages, temperature=0,
                                                     **tool_args)
        message = response.choices[0].message
        calls = [c for c in message.tool_calls or [] if getattr(c, "function", None)]
        if not calls or out_of_calls:
            return parse_answer(message.content)
        messages.append({"role": "assistant", "content": message.content, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls]})
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


async def fallback_plan(inv: Investigation) -> None:
    """A fixed investigative order: widen, then follow money, ring, governance and collateral to a freeze request."""
    e = inv.entity_id
    memory = inv.latest("recall_memory") or {"facts": []}
    statuses = {f["status"] for f in memory["facts"]}
    if "CURRENT" in statuses and "STALE" not in statuses:
        return
    near = await inv.ensure("explore_neighborhood", {"entity_id": e, "depth": 1},
                            "Start with direct counterparties and devices.")
    if not near or near["total_reached"] == 0:
        return
    await inv.ensure("explore_neighborhood", {"entity_id": e, "depth": 5},
                     "Widen to 5 hops across money, shared devices and IPs.")
    routes, associations = gr.sanctioned_hits(inv.results)
    if not routes:
        if associations:
            await inv.notice("A sanctioned entity is within reach, but money from the alert entity cannot follow "
                             "the route there, so the investigation stops at association.")
        return

    hit = min(routes, key=lambda h: h["depth"])
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
    reason = (f"{e} is {hit['depth']} hops from sanctioned {target}"
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


def assess(inv: Investigation, proposed: Verdict | None) -> tuple[AnswerState, Verdict]:
    """The answer state from what the tools returned; the model's verdict is kept unless a SUSPICIOUS call has no path."""
    if inv.unknown:
        return AnswerState.UNKNOWN_ENTITY, Verdict.INSUFFICIENT
    facts = (inv.latest("recall_memory") or {"facts": []})["facts"]
    current = [f for f in facts if f["status"] == "CURRENT"]
    stale = any(f["status"] == "STALE" for f in facts)
    investigated = any(r["ok"] and r["tool"] in INVESTIGATIVE for r in inv.results)
    if current and not stale and not investigated:
        return AnswerState.ANSWERED_FROM_MEMORY, _verdict(current[0]["verdict"]) or Verdict.INSUFFICIENT
    routes, associations = gr.sanctioned_hits(inv.results)
    has_path = bool(routes or associations
                    or any(r["data"]["flagged"] for r in inv.results if r["ok"] and r["tool"] == "explore_neighborhood")
                    or any(r["data"]["sanctioned"] for r in inv.results if r["ok"] and r["tool"] == "trace_money_flow")
                    or any(r["data"]["connected"] for r in inv.results if r["ok"] and r["tool"] == "path_between"))
    verdict = proposed or (Verdict.SUSPICIOUS if routes else Verdict.CLEAR if investigated else Verdict.INSUFFICIENT)
    if verdict == Verdict.SUSPICIOUS and not has_path:
        verdict = Verdict.INSUFFICIENT
    if stale:
        return AnswerState.MEMORY_STALE_REINVESTIGATED, verdict
    return (AnswerState.ANSWERED_WITH_PATH if verdict == Verdict.SUSPICIOUS else AnswerState.NOT_IN_GRAPH), verdict


def finalize(inv: Investigation, answer: dict | None, model: str | None, started: float) -> dict:
    state, verdict = assess(inv, _verdict(answer.get("verdict")) if answer else None)
    given = [inv.entity_id]
    fallback_summary, fallback_claims = gr.summarize(inv.entity_id, inv.results, state, inv.ledger)
    summary, claims = fallback_summary, fallback_claims
    if answer and str(answer.get("summary") or "").strip() and state != AnswerState.UNKNOWN_ENTITY:
        summary = str(answer["summary"]).strip()
        claims = answer.get("claims") if isinstance(answer.get("claims"), list) and answer.get("claims") else fallback_claims
        verification = gr.verify(summary, claims, inv.ledger, given=given)
        if not verification.verified:
            summary, claims = fallback_summary, fallback_claims
            verification.rewritten = True
    else:
        verification = gr.verify(summary, claims, inv.ledger, given=given)

    request = inv.latest("request_action")
    if request:
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
        "summary": summary,
        "claims": [dict(zip(("subject", "relation", "object"), c)) for c in map(gr.normalize_claim, claims) if c],
        "verification": verification.to_json(),
        "freeze_targets": targets,
        "excluded": excluded,
        "approval_request": request,
        "coverage": {"depth": deepest["depth"], "entities_checked": deepest["total_reached"]} if deepest else None,
        "tool_calls": len(inv.results),
        "max_steps": inv.max_steps,
        "budget_exhausted": inv.steps_left <= 0,
        "planner": inv.planner,
        "model": model,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


async def _run(emit: Emit, entity_id: str, key: str, worker_id: str, max_steps: int, mode: Mode,
               case_id: str | None) -> None:
    settings = get_settings()
    use_llm = mode == "llm" or (mode == "auto" and settings.llm_enabled)
    started = time.perf_counter()
    async with Client(build_server(key, worker_id)) as client:
        inv = Investigation(entity_id=entity_id, case_id=case_id or f"case-{uuid.uuid4().hex[:8]}",
                            max_steps=max_steps, worker_id=worker_id, planner="llm" if use_llm else "fallback",
                            client=client, emit=emit)
        model = settings.llm_model if use_llm else None
        await emit({"type": "run_started", "entity_id": entity_id, "case_id": inv.case_id, "graph": key,
                    "planner": inv.planner, "model": model, "max_steps": max_steps, "worker_id": worker_id})
        resolved = await inv.call("resolve_entity", {"entity_id": entity_id},
                                  "Resolve the alert entity before reasoning about it.")
        answer = None
        if not resolved["ok"]:
            await inv.notice(f"Could not resolve {entity_id}: {resolved['error']}")
        elif not inv.unknown:
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
        await emit(finalize(inv, answer, model, started))


async def investigate(entity_id: str, *, graph_key: str | None = None, worker_id: str = "investigator",
                      max_steps: int = MAX_STEPS, mode: Mode = "auto", case_id: str | None = None
                      ) -> AsyncIterator[dict]:
    """Stream investigation events: run_started, tool_call, tool_result, notice, then final (or error)."""
    key = graph_key or tenant_graph_key(get_settings().default_tenant)
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def produce() -> None:
        try:
            await _run(queue.put, entity_id.strip(), key, worker_id, max(1, min(max_steps, MAX_STEPS)), mode, case_id)
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
