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
  playbook: { id: string; family: string; steps: number; ran: number; stopped: string | null } | null;
  memory: PersistedRun | null;
  llm_ms: number;
  llm_wait_ms: number;
  elapsed_ms: number;
};

export type CaseLink = {
  case_id: string;
  canonical: string;
  merged_into: string | null;
  merged: string[];
  shared: string[];
  rule: string | null;
};

export type PersistedRun = {
  episode_id: string;
  case_id: string;
  facts_written: number;
  cached: boolean;
  note: string | null;
  superseded: string[];
  marked_stale: string[];
  reused: string[];
  deferred_to: string[];
  case: CaseLink | null;
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
  tenant: string;
  graph: string;
  nodes: number;
  dataset: { rev?: number; fixture_version?: string; source?: string };
  llm: { enabled: boolean; model: string | null };
  max_steps: number;
  alert_rules: string[];
  public_demo: {
    llm_runs_per_hour: number;
    llm_runs_per_visitor_per_hour: number;
    llm_runs_left: number;
    max_investigations: number;
    hunter_planner: string;
  } | null;
};

export type Scenario = { id: string; label: string; expect: string };

export type LearnedPlaybook = {
  id: string;
  family: string;
  steps: { tool: string; args: Record<string, unknown> }[];
  required_patterns: string[];
  uses: number;
  derived_from: { episode_id: string; subject_id: string; request_id: string; approved_by: string }[];
};

export type ApprovalResult = {
  request_id: string;
  status: string;
  frozen: string[];
  spared: string[];
  playbook: LearnedPlaybook | null;
};

export type AttemptStatus = "CLAIMED" | "COMPLETED" | "FAILED" | "EXPIRED";

export type Attempt = {
  worker_id: string;
  attempt: number;
  status: AttemptStatus;
  claimed_at: number;
  finished_at: number | null;
  error: string | null;
  episode_id: string | null;
  commit_refused_at: number | null;
  refused: string | null;
};

export type QueueAlert = {
  id: string;
  subject_id: string;
  rule: string;
  priority: number;
  created_at: number;
  status: "PENDING" | "CLAIMED" | "COMPLETED" | "FAILED";
  attempt: number | null;
  claimed_by: string | null;
  case_id: string | null;
  verdict: Verdict | null;
  answer_state: string | null;
  episode_id: string | null;
  request_id: string | null;
  error: string | null;
  completed_by: string | null;
  attempts: Attempt[];
  merged_into: string | null;
  merged_on: string[] | null;
};

export type CaseRow = {
  id: string;
  status: "OPEN" | "MERGED";
  subject_id: string;
  created_at: number;
  merged_into: string | null;
  merge: { rule: string; shared: string[]; by: string; at: number } | null;
  evidence: string[];
  episodes: string[];
  requests: string[];
  alerts: string[];
};

export type EpisodeRow = {
  id: string;
  case_id: string;
  entity_id: string;
  worker_id: string;
  planner: string;
  model: string;
  verdict: Verdict;
  answer_state: string;
  created_at: number;
  tool_calls: number;
  elapsed_ms: number;
  request_id: string | null;
  playbook_id: string | null;
  facts_cached: boolean | null;
  cache_note: string | null;
  evidence_rev: number;
  reused: { fact_id: string; episode_id: string; worker_id: string; case_id: string; found_from: string; target: string }[];
  deferred_to: { request_id: string; targets: string[]; status: string }[];
  facts_written: number;
};

export type FactRow = {
  id: string;
  kind: string;
  subject: string;
  predicate: string;
  object: string | null;
  value: unknown;
  unit: string;
  status: "CURRENT" | "STALE" | "SUPERSEDED";
  recorded_status: "CURRENT" | "STALE" | "SUPERSEDED";
  why_stale: string | null;
  changed: string[];
  superseded_by: string | null;
  valid_rev: number;
  created_at: number;
  claim: string;
  episode_id: string;
  worker_id: string;
  case_id: string;
  supersedes: string[];
  shared: { fact_id: string; episode_id: string; case_id: string; worker_id: string; found_from: string } | null;
};

export type PlaybookRow = {
  id: string;
  family: string;
  uses: number;
  created_at: number;
  last_used_at: number | null;
  required_patterns: string[];
  steps: string[];
  learned_from: { episode_id: string; subject_id: string; case_id: string; request_id: string; approved_by: string; at: number }[];
  used_by: { episode_id: string; subject_id: string; verdict: Verdict; worker_id: string }[];
};

export type ApprovalRow = {
  id: string;
  status: "PENDING_L3" | "APPROVED" | "STALE";
  action_type: string;
  reason: string;
  created_at: number;
  requested_by: string;
  target_ids: string[];
  excluded_ids: string[];
  required_clearance: string;
  evidence_rev: number;
  approved_by?: string;
  stale_reason?: string;
  approver: { id: string; name: string; title: string; clearance: string } | null;
  policy: { id: string; title: string } | null;
  case: { id: string; status: string; subject_id: string } | null;
};

export type Board = {
  rev: number;
  dataset: Health["dataset"];
  queue: QueueAlert[];
  cases: CaseRow[];
  episodes: EpisodeRow[];
  facts: { counts: Record<FactRow["status"], number>; recent: FactRow[] };
  playbooks: PlaybookRow[];
  approvals: ApprovalRow[];
};

export type AlertOutcome = {
  alert_id: string;
  subject_id: string;
  worker_id: string;
  attempt: number;
  case_id: string;
  verdict: Verdict | null;
  answer_state: string | null;
  episode_id: string | null;
  merged_into: string | null;
  request_id: string | null;
  tool_calls: number;
  lost_lease: boolean;
  error: string | null;
  committed: boolean;
  status: string;
};

type Hunter = { worker_id: string; alert_id: string };

export type DrainEvent =
  | ({ type: "claimed"; subject_id: string; attempt: number; case_id: string; priority: number } & Hunter)
  | ({ type: "tool_call"; step: number; tool: string; args: Record<string, unknown>; planner: string } & Hunter)
  | ({ type: "notice"; message: string } & Hunter)
  | ({ type: "alert_done" } & AlertOutcome)
  | { type: "swept"; expired: string[]; gave_up: string[]; retried: string[] }
  | { type: "drained"; graph: string; workers: number; outcomes: AlertOutcome[]; elapsed_ms: number }
  | { type: "error"; message: string };

export type Spread = { median: number; min: number; max: number; n: number } | null;

export type Condition = "cold" | "warm" | "revisit" | "baseline";

export type Score = { correct: number; runs: number };

type Measure = "tool_calls" | "verdict_step" | "elapsed_ms" | "llm_requests" | "llm_ms" | "llm_tokens" | "db_queries" | "db_ms" | "bytes_to_planner";

export type ConditionSummary = {
  groups: Record<string, Score & { verdicts: Record<string, number> }>;
  detection: Score;
  failures: { case_id: string; repeat: number; failure: string }[];
  planner_used: Record<string, number>;
  model_agreed: number;
  model_answered: number;
  provider_retries: number;
} & Record<Measure, Spread>;

export type CaseSummary = {
  subject_id: string;
  group: string;
  pattern: string | null;
  split: string | null;
  expected: Verdict | null;
} & Partial<Record<Condition, { correct: string; verdicts: Record<string, number>; tool_calls: Spread; elapsed_ms: Spread }>>;

export type Delta = { runs: number; correct: string } & Partial<Record<"tool_calls" | "elapsed_ms" | "llm_requests" | "llm_tokens", Record<string, number>>>;

export type Stage = { started_at: string; finished_at: string; conditions: Condition[]; repeats: number[]; runs: number; stopped_early: string | null };

export type ControlledResult = {
  config: {
    planner: string;
    model: string | null;
    llm_requests_per_minute: number | null;
    repeats: number | number[];
    conditions: Condition[];
    max_steps: number;
    depth: number;
    graph: string;
    commit: string;
  };
  stages?: Stage[];
  finished_at?: string;
  stopped_early?: string | null;
  playbook: { id: string; family: string } | null;
  summary: {
    conditions: Partial<Record<Condition, ConditionSummary>>;
    cases: Record<string, CaseSummary>;
    warm_vs_cold: { playbook_used_on: string[]; cases: Record<string, Delta | null> };
    revisit_vs_cold: Record<string, Delta | null>;
  };
};

export type StrategySummary = {
  instances: number;
  macro_recall: number;
  found_any: number;
  with_caps: number;
  truncated_results: number;
  failures: number;
} & Record<"recall" | "returned" | "off_instance" | "calls" | "db_queries" | "db_ms" | "bytes", Spread>;

export type RetrievalResult = {
  started_at: string;
  config: { instances: number; hops: number; strategies: Record<string, string>; output_limits: Record<string, number>; seed_rule: string; recall: string };
  summary: {
    strategies: Record<string, StrategySummary>;
    families: Record<string, { instances: number } & Record<string, { macro_recall: number; found_any: string }>>;
  };
};

export type ContentionResult = {
  at: string;
  config: { claimants: number; alerts: number; lease_s: number; max_attempts: number; crash: number; stall: number; fail: number };
  summary: {
    runs: number;
    violations: number;
    alerts_per_s: Spread;
    late_commits_refused: number;
    late_commits_accepted: number;
    crashed_claims_recovered: number;
    statuses: Record<string, number>;
  };
};

export type EvalResults = {
  updated_at: string;
  controlled?: Partial<Record<"fallback" | "llm", ControlledResult>>;
  retrieval?: Partial<Record<"pilot" | "all", RetrievalResult>>;
  contention?: ContentionResult;
};
