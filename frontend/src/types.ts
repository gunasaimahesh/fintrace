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

export type Verification = {
  verified: boolean;
  claims_checked: number;
  ids_checked: number;
  unknown_ids: string[];
  rejected_claims: { claim: unknown; reason: string }[];
  rewritten: boolean;
  badge: string;
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
  summary: string;
  claims: Claim[];
  verification: Verification;
  freeze_targets: string[];
  excluded: string[];
  approval_request: ApprovalRequest | null;
  coverage: { depth: number; entities_checked: number } | null;
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
