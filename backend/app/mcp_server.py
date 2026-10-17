"""FinTrace MCP server: the 11 investigation tools over one FalkorDB graph.

Over stdio for Cursor or Claude Desktop:  python -m backend.app.mcp_server
The investigation agent (agent.py) connects to the same server in-process. Set FINTRACE_GRAPH to
point it at a graph other than the default tenant's.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from backend.app import tools as t
from backend.app.config import get_settings
from backend.app.contracts import Claim
from backend.app.db import get_db, graph_key

INSTRUCTIONS = """\
FinTrace investigates anti-money-laundering alerts over a FalkorDB graph: accounts and their transfers \
(a real IBM AML dataset slice), the devices and IPs they log in from, and the company's own employees, \
policies, tickets and approval decisions. Start with resolve_entity. Paths that cross a shared device or IP \
are identity links, not money. Freezes require an approver with the clearance the policy demands: use \
trace_governance to find them and request_action to ask; nothing is frozen until they sign off."""

EntityId = Annotated[str, Field(description="Entity ID exactly as a tool returned it (prefixes: acct-, merch-, "
                                             "dev-, ip-, emp-, dec-, pol-, tick-, msg-, case-, apr-)")]
EntityIds = Annotated[list[str], Field(description="Entity IDs", min_length=1, max_length=t.MAX_IDS)]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
SCOPED = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def build_server(key: str | None = None, worker_id: str = "mcp-client", claim: Claim | None = None) -> MCPServer:
    ctx = t.ToolContext(graph=get_db().select_graph(key or graph_key(get_settings().default_tenant)),
                        worker_id=worker_id, claim=claim)
    server = MCPServer("FinTrace", instructions=INSTRUCTIONS)

    @server.tool(annotations=READ)
    def resolve_entity(entity_id: EntityId) -> dict[str, Any]:
        """Look up one entity (account, merchant, device, IP, employee, policy, ticket...) by ID: its type,
        name, status, profile and number of counterparties. Call this first. If found is false the entity is
        not in the graph: stop there."""
        return t.resolve_entity(ctx, entity_id=entity_id)

    @server.tool(annotations=READ)
    def recall_memory(entity_id: EntityId, facts: bool = True) -> dict[str, Any]:
        """Prior investigations of this entity: typed facts that are CURRENT (still hold; reuse them) or STALE
        (with the reason, and the tool call that produced them, under `rerun`); under `shared`, routes other
        investigations found that pass through it and still hold (from the entity onward, each is an eligible
        route of its own); recent episodes, approval requests with their status now, and playbooks that match
        the entity's topology fingerprint. Playbooks are procedures learned from investigations a human verified:
        ordered tool steps whose arguments are roles to bind from this entity's own results. With facts=false,
        only the playbooks are returned."""
        return t.recall_memory(ctx, entity_id=entity_id, facts=facts)

    @server.tool(annotations=READ)
    def explore_neighborhood(
        entity_id: EntityId,
        depth: Annotated[int, Field(ge=1, le=t.MAX_DEPTH, description="Hops to search, 1 to 6")] = 2,
    ) -> dict[str, Any]:
        """Two searches from one entity, each reporting the scope it covered (requested and completed depth,
        entities reached, whether the reachable frontier was exhausted, truncated or timed out).
        1. Exploration: everything connected within `depth` hops over transfers and shared devices/IPs in
           either direction, with a count per depth and each sanctioned or risk-flagged entity reached, with its
           shortest path. Reaching something this way is association, not exposure.
        2. eligible_routes: sanctioned accounts that money from the entity could reach. Transfers are followed
           only in their own direction; identity hops only through a shared device (account -> device <-
           account, counted as 2 hops); shared IPs are never followed.
        Depth 1 also lists the direct neighbors."""
        return t.explore_neighborhood(ctx, entity_id=entity_id, depth=depth)

    @server.tool(annotations=READ)
    def trace_money_flow(
        entity_id: EntityId,
        direction: Annotated[Literal["out", "in"], Field(description="'out' follows money sent, 'in' money received")] = "out",
        max_hops: Annotated[int, Field(ge=1, le=t.MAX_DEPTH)] = 4,
        window_days: Annotated[int | None, Field(ge=1, le=3650, description="Only transfers in the last N days")] = None,
    ) -> dict[str, Any]:
        """Follow money through individual transfers in time order (each hop at or after the previous one).
        Returns accounts reached per hop with the largest transfers, any sanctioned account reached with the
        full time-ordered trail, and round trips back to the start. Identity links (devices, IPs) are not followed."""
        return t.trace_money_flow(ctx, entity_id=entity_id, direction=direction, max_hops=max_hops,
                                  window_days=window_days)

    @server.tool(annotations=READ)
    def path_between(from_id: EntityId, to_id: EntityId) -> dict[str, Any]:
        """Shortest connection between two entities over transfers and shared devices/IPs, in either direction."""
        return t.path_between(ctx, from_id=from_id, to_id=to_id)

    @server.tool(annotations=READ)
    def detect_ring(
        entity_id: EntityId,
        runs: Annotated[int, Field(ge=1, le=5, description="Label propagation runs; members must win a majority")] = 1,
    ) -> dict[str, Any]:
        """The community (label propagation) an account, device or IP belongs to. A small community around a
        suspect is a candidate ring; a large one means the entity sits in ordinary busy traffic."""
        return t.detect_ring(ctx, entity_id=entity_id, runs=runs)

    @server.tool(annotations=SCOPED)
    def find_brokers(
        entity_id: EntityId,
        community_ids: Annotated[list[str] | None, Field(description="Ring members to score; defaults to the "
                                                                     "entity's detected community", max_length=t.MAX_COMMUNITY)] = None,
    ) -> dict[str, Any]:
        """Rank ring members by betweenness centrality: the accounts money between the others must pass through."""
        return t.find_brokers(ctx, entity_id=entity_id, community_ids=community_ids)

    @server.tool(annotations=READ)
    def exposure_flow(
        source_ids: EntityIds,
        target_id: EntityId,
        include_shared_device_accounts: Annotated[bool, Field(description="Also count accounts that share a device "
                                                                          "with a source as the same controller")] = True,
    ) -> dict[str, Any]:
        """Maximum dollars that could move from the source accounts to a target account (multi-source max flow
        over aggregated transfers), with the edges that carry it. Use every account behind a shared device as a
        source: identity links carry no money. Ignores timing, so it is an upper bound."""
        return t.exposure_flow(ctx, source_ids=source_ids, target_id=target_id,
                               include_shared_device_accounts=include_shared_device_accounts)

    @server.tool(annotations=READ)
    def trace_governance(entity_id: EntityId) -> dict[str, Any]:
        """The company's own decisions about an entity: waivers that apply to it, who approved them with what
        clearance, the tickets and messages behind them, the governing policy and any breach of it, other
        decisions by the same approver, and the on-call employees allowed to approve a freeze."""
        return t.trace_governance(ctx, entity_id=entity_id)

    @server.tool(annotations=READ)
    def freeze_impact(target_ids: EntityIds) -> dict[str, Any]:
        """Before freezing: accounts outside `target_ids` that share a device or IP with them, split into
        legitimate collateral (leave them and the infrastructure they share untouched) and accounts with an
        eligible route of their own to a sanctioned account (add those to the freeze). Also lists targets that
        already await sign-off in another pending request (leave those out of a new one)."""
        return t.freeze_impact(ctx, target_ids=target_ids)

    @server.tool(annotations=WRITE)
    def request_action(
        target_ids: Annotated[list[str], Field(description="Accounts to freeze", min_length=1, max_length=t.MAX_IDS)],
        policy_id: Annotated[str, Field(description="Policy that governs freezes, from trace_governance")],
        approver_id: Annotated[str, Field(description="On-call approver with the required clearance, from trace_governance")],
        reason: Annotated[str, Field(description="One or two sentences citing the evidence", max_length=1000)],
        case_id: Annotated[str | None, Field(description="Case this request belongs to")] = None,
        excluded_ids: Annotated[list[str] | None, Field(description="Accounts or devices/IPs deliberately left "
                                                                    "untouched", max_length=t.MAX_IDS)] = None,
        action_type: Annotated[Literal["FREEZE"], Field(description="Requested action")] = "FREEZE",
    ) -> dict[str, Any]:
        """Ask the on-call approver to sign off a freeze. Creates a pending approval request linked to the case,
        targets, exclusions, policy and approver. Nothing is frozen until a human approves it. An account can be
        in only one pending request, so a request including one that already awaits sign-off is refused."""
        return t.request_action(ctx, target_ids=target_ids, policy_id=policy_id, approver_id=approver_id,
                                reason=reason, case_id=case_id, excluded_ids=excluded_ids, action_type=action_type)

    return server


if __name__ == "__main__":
    build_server(os.environ.get("FINTRACE_GRAPH")).run()
