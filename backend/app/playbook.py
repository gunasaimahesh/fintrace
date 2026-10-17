"""Learned procedures: a verified investigation's tool steps, each argument replaced by the role it played.

Learning matches every argument of a successful step against role values worked out from the run's own earlier
results (the alert subject, the sanctioned end of the shortest eligible route, the accounts around its shared
device, the ring). Arguments whose meaning the tool schema fixes (the freeze plan) take their role from the schema.
If any ID argument played no role, the run does not generalize and nothing is learned.

Applying binds the same roles from the new subject's live results, step by step, so every step re-checks live
evidence. Action steps are never replayed: the planner decides any freeze afresh.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

HARNESS_TOOLS = {"resolve_entity", "recall_memory"}
ACTION_TOOLS = {"request_action"}
DEVICE_PATTERNS = ("shares_device", "counterparty_shares_device")
FAMILY = "device-linked sanctions exposure"
SCHEMA_ROLES = {
    ("freeze_impact", "target_ids"): "freeze.targets",
    ("request_action", "target_ids"): "freeze.targets",
    ("request_action", "excluded_ids"): "freeze.excluded",
    ("request_action", "policy_id"): "policy.freeze",
    ("request_action", "approver_id"): "approver.on_call",
    ("request_action", "case_id"): "case",
    ("request_action", "reason"): "reason",
}
ROLE_NAMES = {
    "subject": "the alert subject",
    "route.target": "a sanctioned account at the end of an eligible route",
    "route.after_device": "the account just past the route's shared device",
    "route.device_sides": "the accounts on both sides of the route's shared device",
    "ring.members": "a ring around the account past the device",
    "freeze.targets": "a set of accounts to freeze",
}
_ID = re.compile(r"^[a-z]+-[A-Za-z0-9-]+$")


def latest(results: list[dict], tool: str) -> dict | None:
    return next((r["data"] for r in reversed(results) if r["ok"] and r["tool"] == tool), None)


def route(results: list[dict]) -> dict | None:
    """The shortest eligible route found by the deepest exploration that found one."""
    found = [r["data"] for r in results if r["ok"] and r["tool"] == "explore_neighborhood"
             and r["data"]["eligible_routes"]["routes"]]
    if not found:
        return None
    return min(max(found, key=lambda d: d["depth"])["eligible_routes"]["routes"], key=lambda r: r["hops"])


def roles(results: list[dict], subject: str, case_id: str) -> dict[str, Any]:
    """Role values established by `results`."""
    values: dict[str, Any] = {"subject": subject, "case": case_id}
    if hit := route(results):
        values["route.target"] = hit["entity"]["id"]
        devices = [h for h in hit["identity_hops"] if h["type"] == "Device"]
        if devices:
            nodes = hit["path"]["nodes"]
            values["route.after_device"] = nodes[nodes.index(devices[-1]["id"]) + 1]
            values["route.device_sides"] = devices[-1]["accounts_on_path"]
    if ring := latest(results, "detect_ring"):
        values["ring.members"] = [m["id"] for m in ring["members"]]
    return values


def _role_of(value: Any, values: dict[str, Any]) -> str | None:
    for role, known in values.items():
        if value == known or (isinstance(value, list) and isinstance(known, list) and sorted(value) == sorted(known)):
            return role
    return None


def _literal(value: Any) -> bool:
    return isinstance(value, (bool, int, float)) or (isinstance(value, str) and not _ID.match(value))


def parameterize(results: list[dict], subject: str, case_id: str) -> list[dict] | None:
    """The run's successful steps in order, with roles in place of IDs; None if the run does not generalize."""
    steps: list[dict] = []
    for i, r in enumerate(results):
        if not r["ok"] or r["tool"] in HARNESS_TOOLS:
            continue
        known = roles(results[:i], subject, case_id)
        args: dict[str, Any] = {}
        for name, value in r["args"].items():
            role = SCHEMA_ROLES.get((r["tool"], name)) or _role_of(value, known)
            if role:
                args[name] = {"role": role}
            elif _literal(value):
                args[name] = value
            else:
                return None
        step = {"tool": r["tool"], "args": args}
        if step not in steps:
            steps.append(step)
    return steps or None


def bind(args: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Concrete arguments for a step, or (None, the first role this subject has not established)."""
    bound = {}
    for name, value in args.items():
        if isinstance(value, dict) and "role" in value:
            if values.get(value["role"]) in (None, []):
                return None, value["role"]
            bound[name] = values[value["role"]]
        else:
            bound[name] = value
    return bound, None


def required_patterns(fingerprint: dict) -> list[str]:
    return [p for p in fingerprint["patterns"] if p in DEVICE_PATTERNS]


def signature(steps: list[dict], required: list[str]) -> str:
    return hashlib.sha256(json.dumps([steps, required], sort_keys=True).encode()).hexdigest()[:10]
