"""Contracts shared by the agent, memory, alert workers and loaders.

Alert lifecycle: PENDING -> CLAIMED -> COMPLETED | FAILED. A claim sets a fresh claim_token and a lease;
completing or failing is conditional on that token, so a worker whose lease expired cannot commit. A
sweeper returns expired claims, and failed alerts with attempt < MAX_ATTEMPTS, to PENDING.

Facts: every consequential statement on the analyst's card is rendered from a Fact bound to the tool
result it came from (tool, step, result_key).

Revisions: :Meta {key: 'dataset'}.rev goes up in the same query as every evidence-bearing write (a
transfer and its FLOWS_TO summary, an identity link, a sanction flag, a policy property), and every entity
that write touched gets topology_rev = rev, stamped before anything is deleted. Memory bookkeeping, queue
state, approvals, freezes and broker-scope labels are not evidence and leave it alone.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

PRESCRIBED_DEPTH = 6
MAX_ATTEMPTS = 3
LEASE_SECONDS = 120
MAX_CAPACITY = 2**31 - 1
VERDICT_PREDICATES = ("eligible_route_hops", "no_eligible_route")


class AlertStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FactKind(StrEnum):
    SANCTIONS_EXPOSURE = "sanctions_exposure"
    WAIVER_COMPLIANCE = "waiver_compliance"
    ACTION = "action"
    PROFILE = "profile"


PERSISTED_KINDS = (FactKind.SANCTIONS_EXPOSURE, FactKind.WAIVER_COMPLIANCE)


class FactStatus(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class Source:
    tool: str
    step: int
    result_key: str


@dataclass
class Fact:
    """One typed statement taken from one tool result.

    `depends_on` lists the node IDs and edge keys (see `edge_key`) the statement rests on. Aggregate facts
    (max flow, community membership, broker rank) depend on more of the graph than they cite, so they go
    stale on any later revision.
    """

    id: str
    kind: str
    subject: str
    predicate: str
    object: str | None
    value: Any
    unit: str | None
    source: Source
    scope: dict = field(default_factory=dict)
    evidence_rev: int | None = None
    depends_on: list[str] = field(default_factory=list)
    aggregate: bool = False

    def to_json(self) -> dict:
        return asdict(self)


def edge_key(src: str, rel: str, dst: str) -> str:
    return f"{src}|{rel}|{dst}"


META_MATCH = "MATCH (meta:Meta {key: 'dataset'})"
BUMP_REV = META_MATCH + " SET meta.rev = coalesce(meta.rev, 0) + 1 WITH meta.rev AS rev"


def current_rev(graph: Any) -> int:
    rows = graph.ro_query(META_MATCH + " RETURN coalesce(meta.rev, 0)").result_set
    return rows[0][0] if rows else 0
