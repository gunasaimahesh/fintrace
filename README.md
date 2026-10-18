# FinTrace

AML investigation agents whose context lives in a FalkorDB graph: the money, the devices and IPs behind it, the
company's own people and policies, and everything the agents have learned.

An alert lands on a merchant. One hop out, nothing looks wrong. Five hops out, through a device the merchant's
payout account shares with a clearing account, the money reaches a sanctioned holding company. FinTrace's agents
find that route, show what it rests on, check who in the company waived the control that should have
caught it, ask the right on-call officer to approve a surgical freeze, and remember all of it, so the next
investigation starts from what is already known and notices when the evidence changes.

**Hackathon:** WeMakeDevs × FalkorDB *Graph Hacks: Context for AI Agents*. **Tracks entered:**
[Track 1: Connected Data](#track-1-connected-data), [Track 2: Memory & Coordination](#track-2-memory--coordination)
and [Track 3: Company Brain](#track-3-company-brain).

- **Demo video:** *link added at submission*
- **Live demo:** [app-production-1451.up.railway.app](https://app-production-1451.up.railway.app) (shared demo graph; see [Public demo](#public-demo))
- **Repository:** https://github.com/gunasaimahesh/fintrace

Everything here is demo data: the transfers are **IBM synthetic AML data**, and the people, policies, tickets and
sanctions entries are synthetic. Approver sign-off in the UI is simulated, not authenticated.

## Verify our claims in two minutes

```bash
git clone https://github.com/gunasaimahesh/fintrace && cd fintrace
docker compose up --build -d          # FalkorDB + the app; the first start seeds both tenants (seconds)
open http://localhost:8000            # or visit it in a browser
```

1. **Investigate** tab: press *Depth 1*, then *Depth 5* (31 entities and nothing sanctioned, then 30,110 entities
   and one sanctioned account five hops away). Press *Investigate* and watch each tool call stream in with the
   Cypher it ran. Without an LLM key the deterministic planner runs; add one in `.env` for the model planner.
2. **Approvals** tab: sign off the freeze request as the simulated on-call L3 officer.
3. **Memory** tab: add a transfer, unlink an account from the shared device or sanction an account, and see which
   remembered facts go stale and why.
4. **Hunters** tab: three hunters drain the 16-alert queue through claims and leases.
5. **Eval** tab: every measured result below, read from `backend/eval/results.json`.

```bash
docker compose exec app python -m pytest backend/tests -q     # 137 tests, about a minute
```

## How it works

```mermaid
flowchart LR
    subgraph Clients
        UI["React UI<br/>graph, trace, board, inbox, eval"]
        Cursor["Cursor (MCP client)"]
    end
    subgraph App["FastAPI app"]
        Agent["Investigation agent<br/>LLM planner or deterministic fallback"]
        Guard["Guardrails<br/>typed facts, verdict rule, bound statements"]
        Memory["Memory<br/>cases, episodes, facts, playbooks"]
        Hunters["Hunters<br/>claims, leases, sweeper"]
        Tools["11 MCP tools"]
    end
    MCP["MCP server (stdio)"]
    DB[("FalkorDB<br/>one graph per tenant<br/>+ pristine copy")]
    UI -- "SSE + JSON" --> Agent
    UI --> Hunters
    Cursor --> MCP --> Tools
    Agent --> Tools
    Agent --> Guard
    Agent --> Memory
    Hunters --> Agent
    Tools --> DB
    Memory --> DB
    Hunters --> DB
```

- **The agent** (`backend/app/agent.py`) always resolves the alert entity and checks memory first, so an unknown
  ID never reaches the model. An LLM (any OpenAI-compatible endpoint; we used `gemini-3.1-flash-lite`) then picks
  tools turn by turn, at most 14 calls per alert. Without a key, or if the model fails mid-run, a deterministic
  planner continues from the results so far.
- **The verdict comes from the evidence, not the model** (`guardrails.py`). Every statement on the analyst's card
  is rendered from a typed fact bound to the tool result it came from (tool, step, result key). The model may
  propose statements; they are bound to facts or rejected with a reason, and its rationale is shown as unverified.
- **The same 11 tools** serve the agent in-process and Cursor over MCP (`mcp_server.py`; `.cursor/mcp.json` is
  included). Each returns the data the model reads, the evidence (every entity and edge shown), and the
  parameterized Cypher that ran, for the trace inspector.

| Tool | What it answers |
|---|---|
| `resolve_entity` | Look up an entity by ID |
| `recall_memory` | Remembered facts (CURRENT, or STALE with the reason), recent episodes, approval requests and playbooks |
| `explore_neighborhood` | Counts per depth of everything connected, flagged or sanctioned entities with a path to each, and the eligible routes |
| `trace_money_flow` | Time-ordered money flow downstream or upstream over individual transfers |
| `path_between` | Shortest connection between two entities across money and shared devices/IPs |
| `detect_ring` | Label-propagation community containing a risk-layer entity |
| `find_brokers` | Members of a ring ranked by betweenness (who the money has to pass through) |
| `exposure_flow` | Maximum dollars that could move from source accounts to a target (multi-source max flow) |
| `trace_governance` | Decisions applying to an entity, their approvers, sources and policy, and the on-call approvers for a freeze |
| `freeze_impact` | Legitimate customers who share a device or IP with accounts you plan to freeze |
| `request_action` | Ask the on-call approver to sign off a freeze; nothing is frozen until they approve |

## Graph data model

```mermaid
flowchart LR
    Alert -->|ALERT_ON| Account
    Account["Account<br/>(also Merchant, Sanctioned)"] -->|"TRANSFERRED, FLOWS_TO"| Account
    Account -->|USED_DEVICE| Device
    Account -->|LOGGED_FROM| IP
    Decision -->|APPLIES_TO| Account
    Employee -->|APPROVED| Decision
    Decision -->|GOVERNED_BY| PolicyDoc
    Decision -->|SOURCED_FROM| Ticket
    Decision -->|SOURCED_FROM| Message
    Employee -->|"OPENED, AUTHORED"| Ticket
    Employee -->|MEMBER_OF| Team
    Employee -->|REPORTS_TO| Employee
    PolicyDoc -->|OWNED_BY| Team
    Case -->|"ABOUT, EVIDENCE"| Account
    Episode -->|FOR_CASE| Case
    Episode -->|HAS_STEP| Step
    SemanticFact -->|FROM_EPISODE| Episode
    SemanticFact -->|DERIVED_FROM| Account
    Playbook -->|DERIVED_FROM| Episode
    ApprovalRequest -->|"TARGETS, EXCLUDES"| Account
    ApprovalRequest -->|ROUTED_TO| Employee
    ApprovalRequest -->|UNDER_POLICY| PolicyDoc
    Attempt -->|FOR_ALERT| Alert
    classDef risk fill:#1e3a5f,stroke:#60a5fa,color:#fff
    classDef company fill:#14532d,stroke:#4ade80,color:#fff
    classDef learned fill:#4c1d95,stroke:#c084fc,color:#fff
    class Account,Device,IP,Alert risk
    class Employee,Team,PolicyDoc,Decision,Ticket,Message company
    class Case,Episode,Step,SemanticFact,Playbook,ApprovalRequest,Attempt learned
```

Blue: money, identity and alerts. Green: the company layer. Purple: what investigations and hunters write
(simplified; `AUTHORED` goes to `Message`, and facts derive from any evidence, not only accounts).

Every node carries `:Entity` and a unique `id` (indexed). The seeded graph for tenant `acme-pay`
(`python -m backend.app.seed_data`):

| Layer | Nodes | Relationships |
|---|---|---|
| Money (IBM synthetic AML, HI-Small slice) | 40,091 `:Account` (6 also `:Merchant`, 12 also `:Sanctioned`) | 429,039 `TRANSFERRED` (one per transfer: `tx_id`, `amount` in cents, `ts`, `channel`); 57,037 `FLOWS_TO` summaries (one per account pair: `amount`, `transfers`, `first_ts`, `last_ts`, `capacity`) |
| Identity (synthetic) | 10,109 `:Device`, 8,918 `:IP` | 10,120 `USED_DEVICE`, 16,883 `LOGGED_FROM` |
| Company (synthetic, from files) | `:Employee`, `:Team`, `:PolicyDoc`, `:Decision`, `:Ticket`, `:Message` | `MEMBER_OF`, `REPORTS_TO`, `APPROVED`, `APPLIES_TO`, `GOVERNED_BY`, `SOURCED_FROM`, `OPENED`, `AUTHORED`, `OWNED_BY` |
| Work queue | 16 `:Alert` | `ALERT_ON` |
| `:Meta {key: 'dataset'}` | data window, fixture version, and `rev`, the graph revision | |

Accounts, devices and IPs also carry `:RiskNode`, the scope of the graph algorithms. Investigations and hunters
then write their own layer into the same graph:

| Written by | Nodes | Relationships |
|---|---|---|
| Investigations | `:Case`, `:Episode`, `:Step`, `:SemanticFact` | `ABOUT` (from a case, episode or fact to its subject), `(:Episode)-[:FOR_CASE]->(:Case)`, `(:Episode)-[:HAS_STEP]->(:Step)`, `(:SemanticFact)-[:FROM_EPISODE]->(:Episode)`, `(:SemanticFact)-[:DERIVED_FROM]->(evidence)`, `(:SemanticFact)-[:SUPERSEDES]->(:SemanticFact)`, `(:Case)-[:EVIDENCE]->(entity)`, `(:Case)-[:MERGED_INTO]->(:Case)` |
| Freeze requests | `:ApprovalRequest` | `TARGETS`, `EXCLUDES`, `ROUTED_TO` (an `:Employee`), `UNDER_POLICY`, `FOR_CASE`, and `SIGNED_OFF` once approved |
| Learning | `:Playbook` | `(:Playbook)-[:DERIVED_FROM]->(:Episode)`, `(:Episode)-[:USED_PLAYBOOK]->(:Playbook)` |
| Hunters | `:Attempt` | `(:Attempt)-[:FOR_ALERT]->(:Alert)`, `(:Episode)-[:REUSED]->(:SemanticFact)`, `(:Episode)-[:DEFERRED_TO]->(:ApprovalRequest)` |

**Money and identity are different things.** A shared device or IP links people, not funds. The verdict rule
follows transfers only in their own direction, crosses identity only through a shared device
(account → device ← account, counted as two hops), and never follows a shared IP. A sanctioned account reachable
only through an IP, or only against the direction of the money, is an association, not exposure.

**Summary edges keep traversal tractable.** The IBM slice has many parallel transfers per account pair, so
traversals run over one `FLOWS_TO` per pair, and `trace_money_flow` drops to the raw `TRANSFERRED` edges when
time order matters. Every transfer write updates both together (`mutations.py`).

## Core Cypher and algorithms

**Breadth-first, one hop per query.** FalkorDB's variable-length patterns enumerate every path (parallel transfers
make depth 4+ time out), `algo.SSpaths` enumerates paths too, and `algo.BFS` only follows outgoing edges, so none
could count a depth-5 undirected neighborhood within a second. Each level is one query over internal IDs; Python
keeps the parents, the node and time budgets, and a record of how much of the requested scope was covered:

```cypher
// eligible route search, from the accounts on the frontier (queries.py)
UNWIND $frontier AS nid
MATCH (a) WHERE ID(a) = nid
MATCH (a)-[:FLOWS_TO|USED_DEVICE]->(b)
WITH b, min(ID(a)) AS parent
RETURN collect(ID(b)), collect(parent),
       collect(CASE WHEN b:Device THEN 1 ELSE 0 END),
       collect(CASE WHEN b:Sanctioned THEN ID(b) END)

// ... and from the devices on the frontier: back to every account that used them
UNWIND $frontier AS nid
MATCH (d) WHERE ID(d) = nid
MATCH (d)<-[:USED_DEVICE]-(b)
WITH b, min(ID(d)) AS parent
RETURN collect(ID(b)), collect(parent),
       collect(CASE WHEN b:Device THEN 1 ELSE 0 END),
       collect(CASE WHEN b:Sanctioned THEN ID(b) END)
```

NovaPay's depth-5 neighborhood (30,110 entities) takes about 0.3 s; the busiest hub's (45,460 entities) about
0.6 s (`backend/tests/checkpoint_report.json`).

**Time-ordered money flow** follows individual transfers, each hop only after the money arrived:

```cypher
UNWIND $frontier AS f
MATCH (a) WHERE ID(a) = f[0]
MATCH (a)-[t:TRANSFERRED]->(b)
WHERE t.ts >= f[1] AND t.ts <= $until AND t.amount >= $min_amount
WITH b, collect([t.ts, ID(a), t.amount, t.tx_id]) AS options
RETURN collect(ID(b)),
       collect(reduce(best = head(options), o IN options | CASE WHEN o[0] < best[0] THEN o ELSE best END))
```

**Graph algorithms:**

```cypher
// detect_ring: the community around an entity
CALL algo.labelPropagation({nodeLabels: ['RiskNode'], relationshipTypes: $rels, maxIterations: 10})
YIELD node, communityId
WITH communityId, collect(node.id) AS members WHERE $id IN members
RETURN size(members), members[0..$limit]

// find_brokers: betweenness inside the ring only, scoped by a temporary label
CALL algo.betweenness({nodeLabels: ['BrokerScope_1'], relationshipTypes: $rels, samplingSize: 64, samplingSeed: 42})
YIELD node, score RETURN node.id, score ORDER BY score DESC

// exposure_flow: how much could reach the target from several sources at once
MATCH (s:Entity) WHERE s.id IN $sources AND s:Account
WITH collect(s) AS sources MATCH (t:Entity {id: $target})
CALL algo.maxFlow({sourceNodes: sources, targetNodes: [t], nodeLabels: ['Account'],
                   relationshipTypes: ['FLOWS_TO'], capacityProperty: 'capacity'})
YIELD edges, edgeFlows, maxFlow
RETURN maxFlow, [e IN edges | [startNode(e).id, endNode(e).id]], edgeFlows

// path_between: shortest connection across money and shared devices/IPs
MATCH (a:Entity {id: $from_id}), (b:Entity {id: $to_id})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: $rels, weightProp: 'weight',
                   relDirection: 'both', pathCount: 1})
YIELD path, pathWeight RETURN [n IN nodes(path) | ID(n)], pathWeight
```

In the NovaPay scenario, `exposure_flow` from the merchant's payout account and the clearing account that shares
its device returns **$132,000**. That is an upper bound over `FLOWS_TO` capacities in whole dollars (clamped,
timing ignored), with the device as an identity link: it is not proof that $132,000 moved from NovaPay to the
sanctioned account.

## Track 1: Connected Data

| Claim | Evidence |
|---|---|
| Depth changes the answer: 31 entities and nothing sanctioned at depth 1; 30,110 entities and a five-hop route to a sanctioned account at depth 5 | `test_depth_one_is_clean_and_depth_five_reaches_one_sanctioned`, `test_novapay_is_exactly_five_hops_from_vortex`, the *Depth 1 / Depth 5* toggle |
| Routes follow money in its direction; a device bridge costs two hops; shared IPs and reversed money are only association | `test_eligible_routes_keep_direction_and_a_device_bridge_costs_two_hops`, `test_a_valid_route_is_found_despite_a_shorter_ip_path`, `test_a_shared_ip_or_money_flowing_back_is_only_association` |
| CLEAR needs a complete search (to the prescribed depth of 6, or everything reachable); a capped or timed-out search is INSUFFICIENT; a sanctioned account beyond six hops gets a qualified CLEAR | `test_clear_needs_the_prescribed_depth_or_an_exhausted_frontier`, `test_a_capped_or_timed_out_search_is_insufficient_not_clear`, `test_a_sanctioned_account_beyond_six_hops_gets_a_qualified_clear` |
| The model's statements are bound to facts or rejected; swapping in another number, subject or approval is caught | `test_model_statements_are_bound_to_facts_or_rejected`, `test_swapping_monthly_volume_for_exposure_is_rejected`, `test_another_subjects_path_length_is_rejected`, `test_another_approvals_status_is_rejected` |
| Only facts about the alert's own entity decide its verdict: a route or a complete search the model ran from another account is reported as context, and a sanctioned subject is SUSPICIOUS as direct exposure, never CLEAR (it is already blocked, so no freeze request may target it) | `test_a_route_the_model_found_from_another_account_does_not_decide_the_subject`, `test_a_complete_search_the_model_ran_from_another_account_does_not_clear_the_subject`, `test_a_sanctioned_subject_is_suspicious_without_a_route_and_is_not_put_up_for_freezing`, `test_a_sanctioned_account_is_never_put_up_for_a_freeze` |
| An unknown ID stops after one call instead of guessing | `test_an_unknown_id_stops_after_one_call` |
| The agent and Cursor use the same 11 tools over MCP | `test_the_stdio_server_lists_the_eleven_tools_and_answers` |
| Nothing in the graph, tool schemas or prompt gives the answer away | `test_no_ground_truth_in_graph`, `test_tool_schemas_and_the_prompt_name_nothing_from_the_scenario` |
| On 16 labelled cases the agent's verdict and route were right on every run (48 with the real model, 48 with the deterministic planner); one-entity lookups with the same budget and verdict rule ended INSUFFICIENT on 12 of 48 with either planner, never with a wrong verdict | [Evaluation](#evaluation) |

## Track 2: Memory & Coordination

**Memory lives in the graph, next to the evidence it rests on.** Every investigation opens a `:Case`, writes an
`:Episode` with its `:Step`s, and records typed `:SemanticFact`s with `DERIVED_FROM` links to the entities and
edges they depend on. A new session answers from memory (`test_a_new_session_answers_from_memory`).

**Facts know when they stop holding.** Every evidence-bearing write bumps the graph revision and stamps what it
touched. On recall, a route goes STALE only when something it depends on changed (`test_an_unrelated_transfer_keeps_the_route_and_refreshes_only_the_aggregates`,
`test_deleting_a_route_transfer_updates_its_summary_and_stales_the_route`); a CLEAR finding or an aggregate (max
flow, ring, brokers) goes STALE on any change (`test_a_clear_finding_goes_stale_on_any_change`). The re-run fact
SUPERSEDES the stale one. Facts are written CURRENT only if the graph is still at the revision the investigation
started from (`test_a_change_during_the_investigation_is_not_cached`), and a sign-off is refused if the evidence
under the request changed (`test_sign_off_is_refused_once_the_evidence_changed`). The sign-off commits in one
write that requires the graph to still be at the revision whose evidence it checked, so neither a change during
the check nor a second approver racing it can freeze on stale evidence or twice
(`test_evidence_removed_while_sign_off_checks_it_freezes_nothing`, `test_racing_sign_offs_approve_once`). The
request itself is created in one write, with its target and exclusion links and the evidence it rests on: what
the verdict's facts depend on, unchanged since the investigation started. So it can't be signed off half-built
or with only its targets checked (`test_a_request_is_complete_from_the_moment_it_can_be_signed_off`,
`test_a_request_rests_on_its_route_from_the_moment_it_is_filed`), and a request missing a target link freezes
nothing (`test_a_request_missing_a_target_link_freezes_nothing`).

**Procedural memory: playbooks.** When a human approves a freeze, the run's tool steps become a `:Playbook` with
each argument replaced by the role it played (the subject, the sanctioned end of the route, the accounts around
the shared device), and nothing learned if any ID played no role. A playbook matches new alerts by topology alone,
binds its roles from the new subject's live results step by step, and never replays the action: the planner
decides any freeze afresh (`test_a_playbook_is_learned_from_an_approved_run_and_holds_roles_not_ids`,
`test_it_matches_held_out_subjects_by_topology_alone`,
`test_on_a_lookalike_it_stops_where_the_evidence_does_and_the_answer_stays_clear`). Learned from the NovaPay
investigation, it applied to four held-out cases, and with the real model it cut LLM requests and tokens on all
12 of their runs without changing a verdict, at the cost of one or two extra tool calls (table in
[Evaluation](#evaluation)).

**Hunters coordinate only through graph state.** Alerts go PENDING → CLAIMED → COMPLETED | FAILED. A claim is one
write that stamps a fresh token, a lease and an `:Attempt`; renewing, completing or failing commits only if the
alert still carries that token and the lease has not run out
(`test_a_lapsed_lease_cannot_renew_complete_or_fail_even_before_the_sweep`). The hunter's case, facts, case
merges and freeze request are written under the same check, in the same query, so a hunter whose claim passed to
another publishes nothing but its own episode, kept as a record and marked `claim_lost`, and it stops once it
notices
(`test_a_hunter_whose_claim_passed_on_mid_run_publishes_nothing_and_the_new_owner_finishes`). A sweeper returns
expired claims and retries failures up to three attempts. Three hunters drain the queue (`test_three_hunters_drain_the_queue_without_losing_or_duplicating_work`);
a later hunter reuses an earlier hunter's route and defers to its pending freeze instead of filing another
(`test_a_later_hunter_reuses_an_earlier_hunters_route_and_defers_to_its_freeze`); cases merge on a shared
high-risk entity (`test_cases_merge_only_on_a_high_risk_entity_or_two_shared_entities`). Under 20 claimants with
injected crashes, stalls and failures, nothing is lost or completed twice ([Contention](#contention)).

**Tenants.** Each tenant has its own graph and pristine copy; memory, episodes, queue, approvals, revision and
reset stay inside it through the API (`test_memory_queue_approvals_revision_and_reset_stay_inside_their_tenant`).
This is demo data isolation, not access control.

## Track 3: Company Brain

The company's own knowledge is part of the graph, built from source files in `backend/data/company/` (synthetic):
the org chart (`org.toml`), policies (`policies/*.md`, TOML front matter), tickets, chat messages and approval
decisions. Every node records its `source_path`, `source_ref` and a `content_hash`, and re-syncing unchanged files
writes nothing (`test_every_company_node_carries_its_provenance`, `test_unchanged_files_cause_no_writes`).

The NovaPay story runs through it. In September, Rohan Mehta, an L2 onboarding manager, waived device
attestation for NovaPay (`dec-waiver-77`, sourced from a ticket and a Slack message). Policy AML §4.2 requires L3
for waivers on merchants above $250,000 a month, and NovaPay does $410,000. `trace_governance` finds the breach,
and §4.5 says a freeze needs L3 and must exclude infrastructure shared with unrelated customers, so the agent:

- checks collateral with `freeze_impact` (an enterprise payroll account shares a proxy IP with the ring, so the IP
  stays open and the account is spared),
- files the request with `request_action`, routed to the on-call L3 officer, Priya Nair,
- freezes nothing until she signs off in the Approval Inbox, and the sign-off re-checks the evidence
  (`test_a_freeze_needs_an_on_call_l3_and_happens_only_on_sign_off`).

**Keeping company knowledge current.** Raising the waiver threshold in the policy file and re-syncing clears the
breach in one revision, and the waiver-compliance fact goes stale while the sanctions route stays current
(`test_raising_the_waiver_threshold_clears_the_breach_in_one_revision`,
`test_a_policy_edit_refreshes_the_waiver_check_and_keeps_the_route`). The approver ID in the UI is chosen in the
browser: role switching is simulated, not authenticated.

## Evaluation

All results are in `backend/eval/results.json` with their configuration, graph version, case IDs, expected and
actual outcomes, per-run measurements and failures, and every run is logged in `backend/eval/runs/`. The tables
below are generated by `python -m backend.eval.report`. Controlled cases and IBM retrieval are separate
measurements and are reported separately. In a local checkout, `python -m backend.eval.acceptance` runs the whole
acceptance checklist (each row tied to named tests, saved results or repository checks) and records the evidence
in `backend/eval/acceptance.json`, with the commit it ran at.

### Controlled cases

Sixteen labelled alerts on a seeded overlay outside NovaPay's neighborhood (`backend/app/overlay.py`; expected
outcomes live only in `backend/eval/cases.json`): seven positives with an eligible route to a sanctioned account
within six hops, and nine look-alikes without one (IP-only links, money flowing the wrong way, a busy legitimate
merchant, a benign shared device, an isolated new account, a device bridge to a clean account, and routes longer
than six hops). Plus two unknown IDs, two runs starved of budget, and the two demo subjects. A verdict counts only with
the expected route (target and hop count) for suspicious cases, and a complete search for clear ones.

- **Cold:** the agent on a fresh copy of the graph. It is guided: its prompt spells out the procedure (search for
  eligible routes, then follow the money, bound the exposure, check rings, governance and collateral, and file).
  **Warm:** the same, on a copy holding one playbook learned from the approved NovaPay investigation (not a case
  here); the subject's own findings are never read. What the playbook adds beyond the prompt is the concrete
  calls, with arguments bound to this alert's entities by the roles they play, run before the model's first turn.
  **Revisit:** the same subject again, right after the cold run. **Baseline:** one entity per call
  (`lookup_entity`, 25 neighbours a page) with the same 14-call budget and verdict rule, followed breadth-first.

```bash
python -m backend.eval.run_eval --planner fallback --repeats 3
LLM_REQUESTS_PER_MINUTE=12 python -m backend.eval.run_eval --planner llm --repeats 3
```

**LLM planner** (3 repeats. Repeats 1 and 2 ran on commit `d1384dd` with uncommitted backend changes, on the
provider's free tier; repeat 3 and all three baseline repeats ran on `3b3c5c7`. Revisit was added from repeat 2,
and repeat 2 stopped at the free-tier daily quota before its last Revisit run, a demo subject):

| Condition | Detection | Positives | Negatives | Unknown IDs | Out of budget | Demo | Tool calls | Verdict at call | LLM requests | LLM tokens | Failures |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Cold: the agent on a fresh graph | 48/48 | 21/21 | 27/27 | 6/6 | 6/6 | 6/6 | 3 (3–11) | 3 | 2 (2–10) | 9,198 (8,199–69,178) | 1 (1 fell back) |
| Warm: after learning the NovaPay playbook | 48/48 | 21/21 | 27/27 | – | – | – | 4 (3–12) | 3 (3–4) | 2 (1–10) | 9,203.5 (5,543–66,404) | 0 |
| Revisit: same subject, right after cold | 32/32 | 14/14 | 18/18 | – | – | 3/3 | 2 (2–7) | 2 (2–3) | 1 (1–6) | 5,281.5 (4,303–45,376) | 0 |
| Baseline: one-entity lookups, same budget | 36/48 | 21/21 | 15/27 | 6/6 | 6/6 | 0/6 | 9.5 (2–14) | 7 (1–12) | 8 (2–14) | 11,149 (1,502–31,203) | 0 |

`gemini-3.1-flash-lite`, temperature 0; medians (ranges) over the detection runs. The one failure is a demo run
that hit the daily quota mid-investigation and was finished by the deterministic planner (its verdict was right).
With the model choosing the lookups, the baseline found every positive but ended INSUFFICIENT on 12 of the 27
negatives, on every repeat: it ran out of calls on c11, c12 and c14, and stopped after 12 calls with the search
still incomplete on c10. It never gave a wrong verdict.

Where the playbook applied, it cut LLM requests and tokens on all 12 runs:

| Case | Correct | Tool calls (cold → warm) | LLM requests | LLM tokens |
|---|---|---|---|---|
| c01 | 3/3 | 10 → 11 | 9 → 2 | 59,043 → 18,662 |
| c05 | 3/3 | 10 → 12 | 9 → 3 | 59,418 → 29,291 |
| c14 | 3/3 | 3 → 4 | 2 → 1 | 8,660 → 5,984 |
| c15 | 3/3 | 3 → 4 | 2 → 1 | 8,359 → 5,552 |

It costs one or two extra tool calls (the playbook re-checks a step the model skips). Wall time, which is mostly
provider latency and our 12-requests-a-minute limit, fell on 11 of the 12 runs and rose on one (c05, repeat 1:
43.9 s cold, 64.9 s warm). With the deterministic planner the playbook runs the same tools as cold, by
construction.

The LLM runs predate two sets of fixes: `bf9e615` scopes verdicts to the subject's own facts, treats a sanctioned
subject as direct exposure, and fences sign-off and hunters' writes; `eb7b3df` files each freeze request complete,
with the evidence it rests on, in one write. No saved run explored an account other than its subject or had a
sanctioned subject, so none of their verdicts would change, and the prompt and tool outputs the model sees are
unchanged.

**Deterministic planner** (3 repeats, on `eb7b3df`, the deployed code):

| Condition | Detection | Positives | Negatives | Unknown IDs | Out of budget | Demo | Tool calls | Verdict at call | Failures |
|---|---|---|---|---|---|---|---|---|---|
| Cold: the agent on a fresh graph | 48/48 | 21/21 | 27/27 | 6/6 | 6/6 | 6/6 | 4 (3–11) | 4 (3–4) | 0 |
| Warm: after learning the NovaPay playbook | 48/48 | 21/21 | 27/27 | – | – | – | 4 (3–11) | 4 (3–4) | 0 |
| Revisit: same subject, right after cold | 48/48 | 21/21 | 27/27 | – | – | 6/6 | 2 | 2 | 0 |
| Baseline: one-entity lookups, same budget | 36/48 | 18/21 | 18/27 | 6/6 | 6/6 | 0/6 | 8.5 (1–14) | 7 (1–12) | 0 |

Every deterministic baseline miss is an INSUFFICIENT because the 14 calls ran out before the search was complete
(c02, c10, c11, c14 and both demo subjects, on every repeat), never a wrong verdict. One scoring rule changed after we saw a
result, and we say so: a three-call run on c01's subject was expected to end INSUFFICIENT, but the model
established c01's five-hop route within those three calls. Out-of-budget runs now count as correct if they end INSUFFICIENT *or* establish the subject's true verdict
and route (`run_eval.SCORING`, recorded in the results).

### IBM retrieval

Starting from one member of each of the 370 labelled laundering instances in the IBM data (the member with the
smallest `sha256(instance_id + account_id)`), how many of the instance's other members does each strategy surface?
The graph strategy follows money in time order up to four hops each way and adds the account's community
(`detect_ring`, which no hop limit bounds); the lookup strategies list counterparties breadth-first up to four
hops. Recall counts only account IDs the planner actually received, after each tool's output limits.

```bash
python -m backend.eval.retrieval
```

| Strategy | Recall | Found any | IDs returned | Not in the instance | Calls | DB queries | DB ms |
|---|---|---|---|---|---|---|---|
| `graph`: trace_money_flow out + in (max_hops 4), detect_ring: 3 calls | 0.615 | 367/370 | 67 (2–92) | 65 (0–90) | 3 | 16 (11–17) | 143.8 (132.5–272.2) |
| `lookup@3`: lookup_entity breadth-first over transfers both ways, 3 calls | 0.679 | 370/370 | 19 (2–70) | 14 (0–62) | 3 | 3 | 3.6 (1.8–32.0) |
| `lookup@14`: the same, 14 calls (the agent's budget) | 0.816 | 370/370 | 66 (2–345) | 55 (0–335) | 14 (3–14) | 14 (3–14) | 14.2 (2.0–113.5) |

| Pattern family | Instances | `graph` | `lookup@3` | `lookup@14` |
|---|---|---|---|---|
| bipartite | 49 | 0.47 | 0.47 | **0.50** |
| cycle | 54 | **0.90** | 0.75 | 0.88 |
| fan-in | 40 | 0.46 | 0.72 | **0.93** |
| fan-out | 48 | 0.48 | 0.68 | **0.99** |
| gather-scatter | 51 | 0.57 | 0.83 | **0.96** |
| random | 41 | **0.87** | 0.68 | 0.81 |
| scatter-gather | 44 | 0.68 | 0.83 | **0.96** |
| stack | 43 | **0.48** | 0.47 | 0.48 |

**What this shows:** for gathering an instance's members, our graph tools are *not* better than plain lookups
overall. They recover more of the multi-hop patterns (cycles, random), but their output limits (the 3 largest
arrivals per hop, 60 community members) hide members of wide fans, where listing 25 counterparties a page wins.
We report it as measured and did not retune the tools after seeing it. This benchmark measures evidence retrieval from a known member, not
detection from an alert, which is what the controlled cases measure.

### Contention

```bash
python -m backend.eval.contention --runs 5
```

5 runs of 20 hunters racing for 200 alerts (0.3 s leases, 8% of claims crash, 6% stall past their lease, 6% fail):
**0 invariant violations** (no alert lost, none completed twice, attempt numbers unbroken), 69 late commits
refused and 0 accepted, 102 crashed claims recovered, 235.1 (174.1–239.9) alerts/s; final statuses: 993
completed, 7 failed after three attempts. Measured on `bf9e615`, with the lease checks and claim fencing.

## Running it

**Docker (recommended):** `docker compose up --build -d`, then open http://localhost:8000. Copy `.env.example` to
`.env` and set `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL` for the model planner (any OpenAI-compatible
endpoint). FalkorDB's browser is on http://localhost:3000. Ports are configurable with `APP_PORT`,
`FALKORDB_PUBLISHED_PORT` and `FALKORDB_BROWSER_PORT`.

**Local development** (Python 3.12, Node 22+):

```bash
docker compose up -d falkordb
python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp .env.example .env                                   # optional: LLM key
.venv/bin/python -m backend.app.seed_data --reset      # tenant acme-pay (59,149 nodes)
.venv/bin/python -m backend.app.seed_data --tenant globex-eu --profile small --reset
(cd frontend && npm ci && npm run build)
.venv/bin/uvicorn backend.app.main:app --port 8000
```

**In Cursor:** with the local setup running, the bundled `.cursor/mcp.json` starts the MCP server, so Cursor's
agent can call the same 11 tools (`python -m backend.app.mcp_server`; set `FINTRACE_GRAPH` for another graph).

**From the command line:** `python -m backend.app.agent merch-nova-pay [--mode fallback]` runs one investigation;
`python -m backend.app.coordination --workers 3` drains the queue.

### Public demo

The live demo runs with `PUBLIC_DEMO=1`. Every visitor shares each tenant's graph, so changes are applied one at a
time and everyone sees them; *Reset tenant* restores the seeded data. It waits until no investigation is running
and any write it accepted earlier has finished, and until it is done the tenant refuses new investigations,
drains and writes: changes, approvals, alerts and queue resets
(`test_a_reset_behind_a_mutation_holds_off_new_work_until_it_ends`,
`test_a_reset_waits_for_a_write_admitted_before_it`). Model investigations are capped per hour, overall and per visitor (a count of investigations, not of tokens or
spend: one can make several model requests; past the cap an investigation uses the
deterministic planner and says so), concurrent investigations are bounded, and hunters stay deterministic. See
`backend/app/demo.py` and the `DEMO_*` settings in `.env.example`.

It runs on Railway as two services: the same `falkordb/falkordb:v4.22.1` image with a volume and no public
address, and the app built from this repository's `Dockerfile` (`railway.json`), reaching FalkorDB over Railway's
private network. The header shows which planner is available; without a model key, investigations use the
deterministic planner.

## Limitations

- **Demo data.** IBM synthetic AML data plus a planted scenario and synthetic company records; no real customers.
- **Simulated roles.** The approver is chosen in the browser; nothing here authenticates a compliance officer or
  should be read as production authorization.
- **Tenants are demo data isolation, not access control**, and the public demo shares one graph per tenant.
- **Outside the agent.** A freeze request filed by another MCP client, such as Cursor, rests on its target
  accounts only, because the server doesn't know which results that client relied on. The reset hold covers the
  HTTP API, not a local MCP client writing to the same graph.
- **Planted cases.** The controlled cases were designed for this evaluation; 16 cases is a small sample, and the
  LLM numbers come from one model at temperature 0, which still varies run to run.
- **Retrieval.** See above: on IBM instances the graph tools' output limits cost recall against plain lookups.
- **Max flow is an upper bound** over clamped, timing-free capacities, not a traced transfer.

## FalkorDB issues we hit

Found while building (reports drafted for upstream, not filed yet):

- A `WHERE` on a `WITH` right after an `OPTIONAL MATCH` is planned inside the optional match (v4.22.1), so a
  failing test keeps the row with the variable set to NULL instead of dropping it:
  `OPTIONAL MATCH (m:Meta) WITH m WHERE m.rev = 5 CREATE (:Written)` writes even when `m.rev` is 1. FinTrace's
  revision-fenced sign-off filters a projected value instead (`WITH coalesce(m.rev, 0) AS at WHERE at = $rev`),
  which is dropped as expected.
- `algo.maxFlow` rejects relationship types with parallel edges between the same pair of nodes (undocumented),
  so FinTrace keeps one `FLOWS_TO` summary per account pair.
- `algo.maxFlow`'s capacity-range error says 2^32, but capacities spanning `[1, 2^32 − 1]` already fail
  (FinTrace clamps them to `[1, 2^31 − 1]`).
- `algo.BFS` (outgoing edges only) and `algo.betweenness` (follows edge direction) have no direction option.
- `algo.SSpaths` returns a single path by default: `pathCount` (default 1) caps the whole result, not paths per
  destination, which is easy to miss for a procedure described as finding paths to all reachable nodes.
- `algo.labelPropagation` takes no seed.

## Repository layout

```
backend/app/        agent, tools, queries, guardrails, memory, playbook, coordination, company, mutations, API
backend/data/       IBM slice, scenario anchors, company source files, data license
backend/eval/       cases, labels, evaluators, results.json and run logs
backend/tests/      137 tests (pytest), run against throwaway copies of the seeded graph
frontend/           React + Vite UI
scripts/            how the IBM slice was built from the published dataset
```

## AI assistance

FinTrace was built with substantial AI assistance:

- Most of the code, tests and documentation were written by Claude Opus 5.5 working as a coding agent in Cursor.
- The plans were drafted and reviewed across several AI models (Claude Opus 5.5, GPT-6 Astra and Codex), each
  reviewing the others' proposals.
- The participant chose the problem and the track strategy, set the scope and constraints, ran the review rounds
  between the models and decided between their proposals, and set up the environment (FalkorDB, the LLM provider,
  the MCP server in Cursor).
<!-- Participant: check the line above and add anything you did (testing, recording, reviewing code), in your own words. -->

Every measured number in this README is produced by a command in this repository, and the saved results keep the
failures.

## Data and license

The transfers are derived from **IBM Transactions for Anti Money Laundering (AML)**, HI-Small (Altman et al.,
NeurIPS 2023 Datasets and Benchmarks), synthetic data licensed under CDLA-Sharing-1.0; the derived files are shared
under the same license. See [`backend/data/DATA_LICENSE.md`](backend/data/DATA_LICENSE.md) for attribution and the
changes made. Everything else in the demo graph was written for FinTrace.
