const BASE = "";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path);
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
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

export const api = {
  health: () => get<{ status: string; db: string; time: string }>("/api/health"),
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
