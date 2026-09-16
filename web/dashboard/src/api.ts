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

export interface SystemStatus {
  runtime_state: string;
  kill_switch: boolean;
  kill_switch_reason: string | null;
  safe_mode_reason: string | null;
  n_data_snapshots: number;
  n_backtest_results: number;
  paper_broker: string;
  oms: string;
  heartbeat: string;
}

export type FactorState = "DRAFT" | "VALIDATED" | "APPROVED" | "ACTIVE" | "DEPRECATED";

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
  artifacts: (type?: string) =>
    get<Artifact[]>(`/api/artifacts${type ? `?artifact_type=${type}` : ""}`),
  lineage: (id: string) =>
    get<{ ancestors: Artifact[]; descendants: Artifact[] }>(`/api/artifacts/${id}/lineage`),
  experiments: () => get<Experiment[]>("/api/experiments"),
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
