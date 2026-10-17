export type EvidenceNode = { labels: string[]; name: string | null };
export type Evidence = { nodes: Record<string, EvidenceNode>; edges: [string, string, string][] };

export type QueryRecord = { cypher: string; params: string[]; ms: number; calls: number };

export type Envelope = {
  tool: string;
  ok: boolean;
  args: Record<string, unknown>;
  data: any;
  evidence: Evidence;
  error: string | null;
  stop: string | null;
  meta: { elapsed_ms: number; rows: number; queries: QueryRecord[] };
};

export type Claim = { subject: string; relation: string; object: string };

export type Fact = {
  id: string;
  kind: string;
  subject: string;
  predicate: string;
  object: string | null;
  value: unknown;
  unit: string | null;
  source: { tool: string; step: number; result_key: string };
  scope: Record<string, any>;
  evidence_rev: number | null;
  depends_on: string[];
  aggregate: boolean;
};

export type Statement = { text: string; facts: string[] };

export type SearchScope = {
  requested_depth: number;
  completed_depth: number;
  exhausted: boolean;
  reached: number;
  truncated: boolean;
  timed_out: boolean;
  complete: boolean;
  elapsed_ms: number;
};

export type BoundStatement =
  | { statement: unknown; status: "bound"; fact: string }
  | { statement: unknown; status: "rejected"; reason: string };

export type ModelAnswer = {
  proposed_verdict: Verdict | null;
  agrees: boolean;
  rationale: string;
  rationale_check: {
    ids_checked: number;
    unknown_ids: string[];
    rejected_claims: { claim: unknown; reason: string }[];
    passed: boolean;
  };
  statements: BoundStatement[];
};

export type ApprovalRequest = {
  request_id: string;
  status: string;
  case_id: string;
  target_ids: string[];
  excluded_ids: string[];
  policy_id: string;
  approver: { id: string; name: string; clearance: string };
  note: string;
};

export type Verdict = "SUSPICIOUS" | "CLEAR" | "INSUFFICIENT";

export type FinalEvent = {
  type: "final";
  entity_id: string;
  case_id: string;
  answer_state: string;
  verdict: Verdict;
  verdict_basis: string[];
  statements: Statement[];
  summary: string;
  badge: string;
  facts: Fact[];
  claims: Claim[];
  model_answer: ModelAnswer | null;
  freeze_targets: string[];
  excluded: string[];
  approval_request: ApprovalRequest | null;
  coverage: { depth: number; exploration: SearchScope; eligible: SearchScope } | null;
  evidence_rev: number | null;
  tool_calls: number;
  max_steps: number;
  budget_exhausted: boolean;
  planner: string;
  model: string | null;
  elapsed_ms: number;
};

export type ToolCallEvent = {
  type: "tool_call";
  step: number;
  tool: string;
  args: Record<string, unknown>;
  thought: string | null;
  planner: string;
  worker_id: string;
};

export type ToolResultEvent = { type: "tool_result"; step: number } & Envelope;

export type RunEvent =
  | {
      type: "run_started";
      entity_id: string;
      case_id: string;
      graph: string;
      planner: string;
      model: string | null;
      max_steps: number;
      worker_id: string;
    }
  | ToolCallEvent
  | ToolResultEvent
  | { type: "notice"; message: string }
  | { type: "error"; message: string }
  | FinalEvent;

export type Health = {
  status: string;
  graph: string;
  nodes: number;
  llm: { enabled: boolean; model: string | null };
  max_steps: number;
};

export type Scenario = { id: string; label: string; expect: string };

export type ApprovalResult = { request_id: string; status: string; frozen: string[]; spared: string[] };
