const BASE = "";

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(BASE + path, { signal });
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

export interface Artifact {
  artifact_id: string;
  artifact_type: string;
  created_at: string;
  producer: string;
  parent_artifact_ids: string[];
  data_snapshot_id?: string | null;
  code_commit: string;
  config_hash: string;
  content_hash: string;
  uri: string;
  metadata: Record<string, unknown>;
  status: string;
}

export interface MarketDataStatus {
  ready: boolean;
  latest_trade_date: string | null;
  stale: boolean;
  quality_status: string | null;
  quality_report_id: string | null;
  market_snapshot_id: string | null;
  data_snapshot_id: string | null;
}

export interface DominantFuturesContract {
  product: string;
  symbol: string;
  close: number;
  settlement: number;
  return: number;
  volume: number;
  open_interest: number;
}

export interface GoldQuote {
  symbol: string;
  name: string;
  kind: "spot" | "etf" | "futures";
  unit: "CNY/g" | "CNY/share";
  source: string;
  source_url: string;
  fetched_at: string;
  points: {
    date: string;
    close: string;
    open: string | null;
    high: string | null;
    low: string | null;
    volume: string | null;
    amount: string | null;
  }[];
  stale: boolean;
}

export interface GoldAllocation {
  other_assets: string;
  target_weight: string;
  holdings: { symbol: string; quantity: string; average_cost: string }[];
  watchlist: string[];
  note: string;
}

export interface GoldDashboard {
  snapshot: {
    artifact_id: string;
    fetched_at: string;
    quotes: GoldQuote[];
    warnings: string[];
  } | null;
  allocation: { artifact_id: string | null; updated_at: string | null; record: GoldAllocation };
  summary: {
    gold_value: string | null;
    total_assets: string | null;
    weight: string | null;
    target_weight: string;
    deviation: string | null;
    unrealized_pnl: string | null;
    unpriced_symbols: string[];
    has_holdings: boolean;
  };
  sources: { name: string; url: string; description: string }[];
  futures: { trade_date: string | null; contracts: DominantFuturesContract[] };
}

export type GoldResearchStage = "queued" | "fetch" | "researcher" | "critic" | "evaluation"
  | "statistician" | "independent_analysis" | "cross_review" | "analyst_revision"
  | "orchestrator" | "final_audit" | "advice_revision" | "final_reaudit" | "complete" | "failed";

export interface GoldResearchStatus {
  ready: boolean;
  protocol?: string;
  roles: Record<string, { provider: string; model: string }>;
  budget: { max_calls: number; max_tokens: number };
  active_run_id: string | null;
  active_gold_run_id?: string | null;
  busy?: boolean;
}

export interface GoldResearchRequest {
  symbol: string;
  max_weight: number;
  focus: string;
}

export interface GoldResearchParameters {
  fast_window: number;
  slow_window: number;
  volatility_target: number;
  trailing_stop: number;
}

export interface GoldResearchDiagnostic {
  status: string;
  total_return?: number;
  max_drawdown?: number;
  trade_count?: number;
  cost_fraction_of_initial?: number;
  buy_hold_same_cap_return?: number;
  start_date?: string;
  end_date?: string;
  curve?: { date: string; nav: number }[];
  trades: { signal_date: string; execution_date: string; side: string }[];
}

export interface GoldInvestmentAdvicePayload {
  horizon: "3至12个月";
  stance: "WAIT" | "GRADUAL_ALLOCATION" | "DEFENSIVE";
  summary: string;
  reasons: {
    evidence_key: "gold_return_126" | "gold_return_252" | "gold_ma120_gap" | "gold_ma250_gap"
      | "gold_drawdown_252" | "diagnostic_excess_return" | "double_cost_return" | "independent_oos";
    implication: string;
    value: number | boolean;
  }[];
  reassess_when: string;
  confidence: "LOW";
  limitations: string;
}

export interface GoldAdviceAudit {
  status: "PASS" | "REJECT" | "UNAVAILABLE";
  reason: string;
  published_draft_hash: string | null;
  rounds: {
    round: number;
    draft: GoldInvestmentAdvicePayload;
    draft_hash: string;
    review: {
      decision: "PASS" | "REVISE" | "REJECT";
      unsupported_claims: string[];
      missing_risks: string[];
      confidence_adjustment: number;
      rationale: string;
    };
  }[];
}

export type GoldAnalystRole = "technical_analyst" | "macro_analyst" | "quant_analyst" | "risk_analyst";

export interface GoldEvidencePackage {
  snapshot_id: string;
  as_of: string;
  items: Record<string, {
    category: "price" | "diagnostic" | "macro" | "constraint";
    value: number | string | boolean | null;
    definition: string;
    source: string;
  }>;
  limitations: string[];
}

export interface GoldResearchState {
  analyst: GoldAnalystRole;
  view: "BULLISH" | "NEUTRAL" | "BEARISH" | "INSUFFICIENT_EVIDENCE";
  confidence: number;
  summary: string;
  claims: { claim: string; evidence_ids: string[]; counterevidence_ids?: string[]; confidence: number }[];
  risks: string[];
  open_questions: string[];
  invalidating_conditions: string[];
  missing_evidence: string[];
}

export interface GoldDeliberation {
  protocol: "evidence-deliberation-v1";
  evidence_hash: string;
  initial_states: GoldResearchState[];
  states: GoldResearchState[];
  disagreements: {
    disagreement_id: string;
    kind: "DIRECTION" | "EVIDENCE_GAP" | "CONFIDENCE";
    analysts: [GoldAnalystRole, GoldAnalystRole];
    question: string;
    evidence_ids: string[];
  }[];
  revisions: {
    action: "MAINTAIN" | "LOWER_CONFIDENCE" | "RETRACT";
    state: GoldResearchState;
    addressed_disagreement_ids: string[];
    evidence_requests: string[];
    rationale: string;
  }[];
  skipped_analysts: Record<string, string>;
  review_status: "NOT_NEEDED" | "REVISED" | "UNRESOLVED" | "BUDGET_SKIPPED";
  conclusion: "EVIDENCE_BOUNDED" | "INSUFFICIENT_EVIDENCE";
}

export interface GoldResearchResult {
  proposal: {
    name: string;
    hypothesis: string;
    market_view: string;
    risks: string[];
    parameters: GoldResearchParameters;
  };
  evaluation: {
    symbol: string;
    unit?: "CNY/g" | "CNY/share";
    instrument_kind?: "spot" | "etf";
    signal: "ENTER" | "EXIT_OR_WAIT" | "INSUFFICIENT_DATA";
    target_weight: number;
    as_of?: string;
    close?: number;
    fast_average?: number;
    slow_average?: number;
    trailing_reference?: number;
    annualized_volatility?: number;
    max_weight: number;
    parameters: GoldResearchParameters;
    rules: { entry: string; exit: string; sizing: string; execution: string; invalidation: string };
    diagnostic: GoldResearchDiagnostic;
    double_cost: GoldResearchDiagnostic;
    limitations: string[];
    deployment_eligible: false;
    research_only: true;
  };
  critic: { approved: boolean; issues: string[]; rationale: string };
  statistician: { robust: boolean; findings: string[]; rationale: string };
  decision: { recommend_candidate: boolean; rationale: string; next_steps: string[] };
  investment_advice?: GoldInvestmentAdvicePayload | null;
  advice_audit?: GoldAdviceAudit;
  evidence_package?: GoldEvidencePackage | null;
  deliberation?: GoldDeliberation | null;
  local_gates: Record<string, boolean>;
  outcome: "OBSERVE" | "PAPER_CANDIDATE";
  deployment_eligible: false;
  analysis_context: {
    as_of: string;
    latest_fetch: string;
    snapshot_id: string;
    facts: Record<string, unknown>;
    warnings: string[];
  };
  calls: {
    role: string;
    provider: string;
    model: string;
    input_tokens: number | null;
    output_tokens: number | null;
    raw_request_id: string | null;
  }[];
}

export interface GoldResearchRun {
  run_id: string;
  protocol?: string;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
  stage: GoldResearchStage;
  created_at: string;
  updated_at: string;
  request: GoldResearchRequest;
  snapshot_id: string | null;
  report_artifact_id?: string | null;
  error: string | null;
  result: GoldResearchResult | null;
}

export interface MarketSnapshotPayload {
  schema_version: string;
  trade_date: string;
  equity: {
    symbols: number;
    advancers: number;
    decliners: number;
    unchanged: number;
    median_return: number;
    total_amount: number;
  };
  futures: {
    contracts: number;
    dominant_contracts: DominantFuturesContract[];
  };
}

export interface MarketSnapshotResponse {
  artifact: Artifact;
  snapshot: MarketSnapshotPayload;
}

export interface EquityPoint {
  date: string;
  cash: string;
  frozen_cash: string;
  market_value: string;
  nav: string;
}

export interface Metrics {
  performance: Record<string, any>;
  execution: Record<string, any>;
  risk: Record<string, any>;
  violations: string[];
}

export interface PositionDoc {
  cash: string;
  frozen_cash: string;
  market_value: string;
  nav: string;
  realized_pnl: string;
  unrealized_pnl: string;
  fees: string;
  taxes: string;
  positions: Record<
    string,
    { total: number; available: number; avg_cost: string; last_price: string }
  >;
  source_artifact_id?: string;
}

export interface OrderRow {
  order_id: string;
  client_order_id: string;
  strategy_id: string;
  symbol: string;
  side: string;
  quantity: number;
  filled_quantity: number;
  avg_fill_price: string | null;
  reference_price: string | null;
  status: string;
  reject_reason: string;
  created_at: string;
}

export interface TradeRow {
  fill_id: string;
  order_id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: string;
  commission: string;
  stamp_tax: string;
  transfer_fee: string;
  trade_date: string;
}

export interface Experiment {
  experiment_id: string;
  name: string;
  description: string;
  state: string;
  created_at: string;
  updated_at: string;
}

export interface AgentResearchStatus {
  ready: boolean;
  providers: Record<string, boolean>;
  roles: Record<string, { provider: string; model: string }>;
  budget: {
    max_candidates_per_run: number;
    max_llm_calls_per_run: number;
    max_total_tokens_per_run: number;
    max_cost_usd_per_run: number;
  };
  factor_validation: {
    min_ic_days: number;
    min_coverage: number;
    min_abs_rank_ic: number;
  };
}

export interface AgentResearchRequest {
  topic: string;
  universe_id: string;
  start_date: string;
  end_date: string;
  target: string;
}

export interface AgentResearchResult {
  trace_id: string;
  experiment: Experiment;
  experiment_run: { run_id: string; status: string; metrics: Record<string, unknown> };
  factor_run: FactorRun;
  candidate: { name: string; hypothesis: string; expression: string; horizon: number };
  critic: { approved: boolean; issues: string[]; rationale: string };
  statistician: { robust: boolean; findings: string[]; rationale: string };
  decision: { recommend_candidate: boolean; rationale: string; next_steps: string[] };
  local_gates: Record<string, boolean>;
  report_artifact_id: string;
  calls: {
    role: string;
    provider: string;
    model: string;
    input_tokens: number | null;
    output_tokens: number | null;
  }[];
}

export interface SystemStatus {
  runtime_state: string;
  kill_switch: boolean;
  kill_switch_reason: string | null;
  safe_mode_reason: string | null;
  n_data_snapshots: number;
  n_backtest_results: number;
  n_futures_simulations: number;
  n_futures_backtests: number;
  futures_gateway: string;
  paper_broker: string;
  oms: string;
  heartbeat: string;
}

export interface FuturesContract {
  symbol: string;
  name: string;
  exchange: string;
  product: string;
  multiplier: string;
  price_tick: string;
  long_margin_rate: string;
  short_margin_rate: string;
  open_fee_per_lot: string;
  close_fee_per_lot: string;
  close_today_fee_per_lot: string;
  last_trading_day: string | null;
  catalog_version: string;
}

export interface FuturesSimulationSummary {
  simulation_id: string;
  name: string;
  mode: "PAPER";
  status: string;
  initial_balance: string;
  current_trading_day: string | null;
  last_event_seq: number;
  last_event_hash: string;
  created_at: string;
  updated_at: string;
}

export interface FuturesAccount {
  balance: string;
  equity: string;
  available: string;
  margin: string;
  frozen_margin: string;
  unrealized_pnl: string;
  realized_pnl: string;
  fees: string;
  risk_ratio: string;
}

export interface FuturesPosition {
  symbol: string;
  long_today: number;
  long_yesterday: number;
  short_today: number;
  short_yesterday: number;
  long_settlement_price: string;
  short_settlement_price: string;
}

export interface FuturesSnapshot {
  simulation_id: string;
  name: string;
  mode: "PAPER";
  status: string;
  current_trading_day: string | null;
  next_trading_day: string | null;
  account: FuturesAccount;
  marks: Record<string, string>;
  positions: FuturesPosition[];
  order_count: number;
  trade_count: number;
  settlement_count: number;
  last_event_seq: number;
  last_event_hash: string;
  updated_at: string;
}

export interface FuturesOrder {
  order_id: string;
  client_order_id: string;
  symbol: string;
  direction: "LONG" | "SHORT";
  offset: "OPEN" | "CLOSE" | "CLOSE_TODAY" | "CLOSE_YESTERDAY";
  quantity: number;
  order_type: "MARKET" | "LIMIT";
  limit_price: string | null;
  status: string;
  filled_quantity: number;
  average_fill_price: string | null;
  reject_reason: string;
  created_at: string;
  updated_at: string;
}

export interface FuturesTrade {
  trade_id: string;
  order_id: string;
  symbol: string;
  direction: "LONG" | "SHORT";
  offset: string;
  quantity: number;
  price: string;
  fee: string;
  trading_day: string;
  traded_at: string;
}

export interface FuturesSettlement {
  trading_day: string;
  balance: string;
  equity: string;
  available: string;
  margin: string;
  risk_ratio: string;
  settlements: Record<string, string>;
}

export interface FuturesOrderCreate {
  client_order_id: string;
  symbol: string;
  direction: "LONG" | "SHORT";
  offset: "OPEN" | "CLOSE" | "CLOSE_TODAY" | "CLOSE_YESTERDAY";
  quantity: number;
  order_type: "MARKET" | "LIMIT";
  limit_price?: string;
}

export interface FuturesBacktestArtifact extends Artifact {
  metadata: {
    run_id: string;
    final_event_hash: string;
    metrics: {
      final_equity: string;
      total_return: number;
      max_drawdown: number;
      trade_count: number;
      fees: string;
      final_risk_ratio: number;
    };
  };
}

export interface FuturesBacktestResult {
  run_id: string;
  spec_artifact_id: string;
  result_artifact_id: string;
  run_dir: string;
  final_event_hash: string;
  metrics: FuturesBacktestArtifact["metadata"]["metrics"];
}

export type FactorState =
  | "PROPOSED"
  | "SCREENED"
  | "CANDIDATE"
  | "VALIDATING"
  | "VALIDATED"
  | "STRATEGY_ELIGIBLE"
  | "DEPRECATED";

export interface Factor {
  factor_id: string;
  factor_key: string;
  version: string;
  name: string;
  category: string;
  description: string;
  owner: string;
  state: FactorState;
  code_ref: string;
  data_snapshot_id: string | null;
  validation_artifact_id: string | null;
  parameters: Record<string, unknown>;
  metrics: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface FactorCreate {
  factor_key: string;
  version: string;
  name: string;
  category: string;
  description: string;
  owner: string;
  code_ref: string;
  data_snapshot_id?: string | null;
  parameters?: Record<string, unknown>;
  metrics: Record<string, number>;
}

export interface FactorRun {
  run_id: string;
  expression: string;
  universe_id: string;
  start_date: string;
  end_date: string;
  target: string;
  horizon: number;
  status: string;
  inputs: string[];
  complexity: number;
  metrics: Record<string, unknown>;
  ic_series: { date: string; ic: number }[];
  quantile_returns: Record<string, number | null>;
  decay: { horizon: number; rank_ic: number | null }[];
  yearly_ic: Record<string, number>;
  created_at: string;
}

export interface FactorEvaluateRequest {
  expression: string;
  universe_id: string;
  start_date: string;
  end_date: string;
  target: string;
  horizon: number;
}

export interface ResearchBudget {
  max_calls: number;
  max_tokens: number;
  timeout_seconds: number;
  max_parallel_calls: number;
}

export interface ResearchRequestSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  format?: string;
  default?: unknown;
  enum?: unknown[];
  const?: unknown;
  required?: string[];
  properties?: Record<string, ResearchRequestSchema>;
  items?: ResearchRequestSchema;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  pattern?: string;
  additionalProperties?: boolean | ResearchRequestSchema;
  $ref?: string;
  $defs?: Record<string, ResearchRequestSchema>;
  anyOf?: ResearchRequestSchema[];
  oneOf?: ResearchRequestSchema[];
}

export interface TaskDescriptor {
  task_type: string;
  module_id: string;
  title: string;
  version: string;
  result_schema_version: string;
  roles: string[] | Record<string, unknown>;
  capabilities: string[];
  default_budget: ResearchBudget;
  acquisition: boolean | string | Record<string, unknown>;
  ready: boolean;
  unavailable_reason: string | null;
  missing_roles: string[];
  request_schema: ResearchRequestSchema;
}

export interface ResearchModules {
  ready: boolean;
  active_run_id: string | null;
  busy: boolean;
  max_parallel_tasks: number;
  max_parallel_calls: number;
  modules: TaskDescriptor[];
}

export interface ResearchPlanTask {
  task_id: string;
  task_type: string;
  payload: Record<string, unknown>;
  inputs: Record<string, { task_id: string; output_key: string }>;
  depends_on: string[];
  required: boolean;
  budget: ResearchBudget;
}

export interface ResearchPlan {
  schema_version: "research-plan-v1";
  plan_id: string;
  question: string;
  tasks: ResearchPlanTask[];
  mode: "LATEST" | "HISTORICAL";
  knowledge_cutoff: string | null;
  budget: ResearchBudget;
  source: "EXPLICIT" | "RULE" | "MODEL";
}

export interface ResearchPlanRequest {
  question: string;
  payload?: Record<string, unknown>;
  use_model: boolean;
  budget?: ResearchBudget;
}

export interface EvidenceRef {
  artifact_id: string;
  content_hash?: string | null;
  namespace?: string;
  schema_version?: string;
  record_key?: string | null;
  [key: string]: unknown;
}

export interface TaskResult {
  outcome: string;
  summary: string;
  payload: Record<string, unknown>;
  evidence: EvidenceRef[];
  publication: {
    decision: "PASS" | "WITHHELD" | "NOT_APPLICABLE";
    content_hash: string | null;
    reason: string | null;
  };
  limitations: string[];
  report_artifact_id: string | null;
}

export type ResearchRunStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED"
  | "INTERRUPTED" | "CANCELLED";

export interface ResearchUsage {
  calls_used: number;
  tokens_used: number;
  tokens_reserved: number;
  max_calls: number;
  max_tokens: number;
  [key: string]: unknown;
}

export interface ResearchRun {
  run_id: string;
  root_run_id: string;
  parent_run_id: string | null;
  task_id: string | null;
  task_type: string;
  module_id: string;
  task_version: string;
  result_schema_version: string;
  status: ResearchRunStatus;
  stage: string;
  created_at: string;
  updated_at: string;
  request: Record<string, unknown>;
  knowledge_cutoff: string | null;
  result: TaskResult | null;
  error: string | null;
  children: string[];
  usage: ResearchUsage;
  cancel_requested: boolean;
  revision: number;
}

export interface ResearchRunRequest {
  task_type?: string;
  payload?: Record<string, unknown>;
  plan?: ResearchPlan;
  budget?: ResearchBudget;
  idempotency_key: string;
  mode?: "LATEST" | "HISTORICAL";
  knowledge_cutoff?: string | null;
}

export interface ResearchEvent {
  run_id: string;
  sequence: number;
  event_type: string;
  stage: string;
  at: string;
  data: Record<string, unknown>;
}

export interface ResearchCallReceipt {
  call_id: string;
  sequence: number;
  role: string;
  provider: string;
  model: string;
  purpose: string;
  status: string;
  messages: { role: string; content: unknown }[];
  response: {
    content: string;
    provider: string;
    model: string;
    input_tokens: number | null;
    output_tokens: number | null;
    raw_request_id: string | null;
  } | null;
  error: string | null;
  parse_error: string | null;
  reservation_tokens: number;
  used_tokens: number;
  repair_of: string | null;
  created_at: string;
}

export interface ResearchPlanResponse {
  plan: ResearchPlan;
  artifact_id: string;
  receipts?: ResearchCallReceipt[];
}

export const api = {
  researchModules: (signal?: AbortSignal) => get<ResearchModules>("/api/research/modules", signal),
  researchPlan: (body: ResearchPlanRequest, signal?: AbortSignal) =>
    post<ResearchPlanResponse>("/api/research/plans", body, signal),
  researchRuns: (offset = 0, limit = 20, signal?: AbortSignal) =>
    get<ResearchRun[]>(`/api/research/runs?offset=${offset}&limit=${limit}`, signal),
  startResearchRun: (body: ResearchRunRequest, signal?: AbortSignal) =>
    post<ResearchRun>("/api/research/runs", body, signal),
  researchRun: (id: string, signal?: AbortSignal) =>
    get<ResearchRun>(`/api/research/runs/${encodeURIComponent(id)}`, signal),
  researchEvents: (id: string, offset = 0, limit = 100, signal?: AbortSignal) =>
    get<ResearchEvent[]>(`/api/research/runs/${encodeURIComponent(id)}/events?offset=${offset}&limit=${limit}`, signal),
  researchCalls: (id: string, offset = 0, limit = 100, signal?: AbortSignal) =>
    get<ResearchCallReceipt[]>(`/api/research/runs/${encodeURIComponent(id)}/calls?offset=${offset}&limit=${limit}`, signal),
  cancelResearchRun: (id: string, signal?: AbortSignal) =>
    post<ResearchRun>(`/api/research/runs/${encodeURIComponent(id)}/cancel`, undefined, signal),
  health: () => get<{ status: string; db: string; time: string }>("/api/health"),
  goldDashboard: () => get<GoldDashboard>("/api/gold/dashboard"),
  saveGoldAllocation: (body: GoldAllocation) => post<GoldDashboard>("/api/gold/allocation", body),
  goldResearchStatus: (signal?: AbortSignal) => get<GoldResearchStatus>("/api/gold/research/status", signal),
  goldResearchRuns: (signal?: AbortSignal) => get<GoldResearchRun[]>("/api/gold/research/runs", signal),
  startGoldResearch: (body: GoldResearchRequest, signal?: AbortSignal) =>
    post<GoldResearchRun>("/api/gold/research/runs", body, signal),
  goldResearchRun: (id: string, signal?: AbortSignal) =>
    get<GoldResearchRun>(`/api/gold/research/runs/${encodeURIComponent(id)}`, signal),
  marketDataStatus: () => get<MarketDataStatus>("/api/market-data/status"),
  marketSnapshots: () => get<Artifact[]>("/api/market-snapshots"),
  latestMarketSnapshot: () => get<MarketSnapshotResponse>("/api/market-snapshots/latest"),
  artifacts: (type?: string) =>
    get<Artifact[]>(`/api/artifacts${type ? `?artifact_type=${type}` : ""}`),
  lineage: (id: string) =>
    get<{ ancestors: Artifact[]; descendants: Artifact[] }>(`/api/artifacts/${id}/lineage`),
  experiments: () => get<Experiment[]>("/api/experiments"),
  agentResearchStatus: () => get<AgentResearchStatus>("/api/agent-research/status"),
  agentResearchRuns: () => get<Artifact[]>("/api/agent-research/runs"),
  runAgentResearch: (body: AgentResearchRequest) =>
    post<AgentResearchResult>("/api/agent-research/runs", body),
  backtests: () => get<Artifact[]>("/api/backtests"),
  metrics: (id: string) => get<Metrics>(`/api/backtests/${id}/metrics`),
  equity: (id: string) => get<EquityPoint[]>(`/api/backtests/${id}/equity`),
  orders: (id: string, limit = 2000) => get<OrderRow[]>(`/api/backtests/${id}/orders?limit=${limit}`),
  trades: (id: string, limit = 2000) => get<TradeRow[]>(`/api/backtests/${id}/trades?limit=${limit}`),
  positions: (id: string) => get<PositionDoc>(`/api/backtests/${id}/positions`),
  latestPortfolio: () => get<PositionDoc>("/api/portfolio/latest"),
  factors: () => get<Factor[]>("/api/factors"),
  createFactor: (body: FactorCreate) => post<Factor>("/api/factors", body),
  transitionFactor: (id: string, target: FactorState, reason: string) =>
    post<Factor>(`/api/factors/${id}/transition`, { target, reason, actor: "dashboard" }),
  factorRuns: () => get<FactorRun[]>("/api/factor-runs"),
  evaluateFactor: (body: FactorEvaluateRequest) =>
    post<FactorRun>("/api/factor-runs/evaluate", body),
  systemStatus: () => get<SystemStatus>("/api/system/status"),
  killSwitch: (engage: boolean, reason: string) =>
    post<{ kill_switch: boolean }>(`/api/system/killswitch?engage=${engage}&reason=${encodeURIComponent(reason)}`),
  futuresContracts: () => get<FuturesContract[]>("/api/futures/contracts"),
  futuresSimulations: () => get<FuturesSimulationSummary[]>("/api/futures/simulations"),
  createFuturesSimulation: (name: string, initialBalance: string) =>
    post<FuturesSnapshot>("/api/futures/simulations", {
      name,
      initial_balance: initialBalance,
      mode: "PAPER",
    }),
  futuresSnapshot: (id: string) => get<FuturesSnapshot>(`/api/futures/simulations/${id}`),
  futuresOrders: (id: string) => get<FuturesOrder[]>(`/api/futures/simulations/${id}/orders`),
  futuresTrades: (id: string) => get<FuturesTrade[]>(`/api/futures/simulations/${id}/trades`),
  futuresSettlements: (id: string) =>
    get<FuturesSettlement[]>(`/api/futures/simulations/${id}/settlements`),
  submitFuturesOrder: (id: string, body: FuturesOrderCreate) =>
    post<FuturesOrder>(`/api/futures/simulations/${id}/orders`, body),
  cancelFuturesOrder: (id: string, orderId: string) =>
    post<FuturesOrder>(`/api/futures/simulations/${id}/orders/${orderId}/cancel`),
  advanceFuturesSimulation: (id: string) =>
    post<FuturesSnapshot>(`/api/futures/simulations/${id}/advance`),
  futuresBacktests: () => get<FuturesBacktestArtifact[]>("/api/futures/backtests"),
  runFuturesBacktest: (symbol: string) =>
    post<FuturesBacktestResult>("/api/futures/backtests", {
      strategy_id: "moving_average_trend",
      symbol,
      initial_balance: "100000",
      lots: 2,
      fast_window: 2,
      slow_window: 3,
      random_seed: 42,
    }),
};

export const fmt = {
  num: (v: unknown, digits = 2) => {
    const n = typeof v === "string" ? parseFloat(v) : (v as number);
    return n === undefined || n === null || Number.isNaN(n)
      ? "-"
      : n.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
  },
  pct: (v: unknown, digits = 2) => {
    const n = typeof v === "string" ? parseFloat(v) : (v as number);
    return n === undefined || n === null || Number.isNaN(n) ? "-" : (n * 100).toFixed(digits) + "%";
  },
};
