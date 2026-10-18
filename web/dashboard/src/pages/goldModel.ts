import type { GoldAllocation, GoldDeliberation, GoldEvidencePackage, GoldQuote, GoldResearchRequest, GoldResearchResult,
  GoldResearchRun, GoldResearchStage, GoldResearchStatus } from "../api";

export const GOLD_ETFS = [
  { symbol: "518880", name: "华安黄金ETF" },
  { symbol: "518800", name: "国泰黄金ETF" },
  { symbol: "159934", name: "易方达黄金ETF" },
  { symbol: "159937", name: "博时黄金ETF" },
];
export const GOLD_INSTRUMENTS = [{ symbol: "Au99.99", name: "黄金现货 Au99.99" }, ...GOLD_ETFS];
export const OFFICIAL_SOURCES = [
  { name: "上海黄金交易所", url: "https://www.sge.com.cn/", description: "黄金现货日行情" },
  { name: "上海证券交易所", url: "https://www.sse.com.cn/", description: "沪市黄金ETF" },
  { name: "深圳证券交易所", url: "https://www.szse.cn/", description: "深市黄金ETF" },
  { name: "上海期货交易所", url: "https://www.shfe.com.cn/", description: "AU 合约与日行情" },
  { name: "中国人民银行", url: "https://www.pbc.gov.cn/", description: "月度黄金储备（来源链接）" },
  { name: "国家外汇管理局", url: "https://www.safe.gov.cn/", description: "储备资产与宏观统计（来源链接）" },
];
export type Period = "1M" | "3M" | "1Y" | "全部";
export type ClosePoint = GoldQuote["points"][number] & { value: number };
export interface GoldDraft {
  other_assets: string;
  target_percent: string;
  holdings: GoldAllocation["holdings"];
  watchlist: string[];
  note: string;
}

export function number(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && value.trim() === "") return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

export function display(value: unknown, percent = false, digits = 2): string {
  const result = number(value);
  return result === null ? "—" : (result * (percent ? 100 : 1)).toLocaleString("zh-CN", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }) + (percent ? "%" : "");
}

function shiftDecimal(value: string, places: number): string {
  const [whole, fraction = ""] = value.split(".");
  const digits = whole + fraction;
  const position = whole.length + places;
  const shifted = position <= 0 ? `0.${"0".repeat(-position)}${digits}`
    : position >= digits.length ? digits + "0".repeat(position - digits.length)
      : `${digits.slice(0, position)}.${digits.slice(position)}`;
  return shifted.replace(/^0+(?=\d)/, "").replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

export function createDraft(record?: GoldAllocation): GoldDraft {
  return {
    other_assets: record?.other_assets ?? "0",
    target_percent: shiftDecimal(record?.target_weight ?? "0.10", 2),
    holdings: GOLD_ETFS.map(({ symbol }) => record?.holdings.find((row) => row.symbol === symbol)
      ?? { symbol, quantity: "0", average_cost: "0" }),
    watchlist: record ? [...record.watchlist] : GOLD_ETFS.map(({ symbol }) => symbol),
    note: record?.note ?? "",
  };
}

export function allocationFromDraft(draft: GoldDraft): GoldAllocation {
  const values = [draft.other_assets, draft.target_percent,
    ...draft.holdings.flatMap((row) => [row.quantity, row.average_cost])];
  if (values.some((value) => !/^\d+(?:\.\d+)?$/.test(value) || number(value) === null)) {
    throw new Error("资产、数量、成本和目标比例须为非负有限十进制数。");
  }
  const targetWeight = shiftDecimal(draft.target_percent, -2);
  const [whole, fraction = ""] = targetWeight.split(".");
  if (whole !== "0" && (whole !== "1" || /[1-9]/.test(fraction))) {
    throw new Error("目标比例须在 0% 至 100% 之间。");
  }
  const symbols = new Set(GOLD_ETFS.map(({ symbol }) => symbol));
  if (new Set(draft.holdings.map((row) => row.symbol)).size !== draft.holdings.length
    || draft.holdings.some((row) => !symbols.has(row.symbol))
    || new Set(draft.watchlist).size !== draft.watchlist.length
    || draft.watchlist.some((symbol) => !symbols.has(symbol))) throw new Error("标的重复或不受支持。");
  return { other_assets: draft.other_assets, target_weight: targetWeight,
    holdings: draft.holdings.filter((row) => Number(row.quantity) > 0).map((row) => ({ ...row })),
    watchlist: [...draft.watchlist], note: draft.note };
}

export function closePoints(quote?: GoldQuote): ClosePoint[] {
  return [...new Map((quote?.points ?? []).flatMap((point) => {
    const value = number(point.close);
    return value !== null && value > 0 ? [[point.date, { ...point, value }] as const] : [];
  })).values()].sort((first, second) => first.date.localeCompare(second.date));
}

export function inPeriod(points: ClosePoint[], period: Period): ClosePoint[] {
  if (!points.length || period === "全部") return points;
  const end = new Date(`${points.at(-1)!.date}T00:00:00Z`);
  const day = end.getUTCDate();
  end.setUTCDate(1);
  end.setUTCMonth(end.getUTCMonth() - ({ "1M": 1, "3M": 3, "1Y": 12 }[period]));
  const lastDay = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth() + 1, 0)).getUTCDate();
  end.setUTCDate(Math.min(day, lastDay));
  if (!Number.isFinite(end.getTime())) return [];
  const cutoff = end.toISOString().slice(0, 10);
  return points.filter((point) => point.date >= cutoff);
}

export function commonSeries(quotes: (GoldQuote | undefined)[], period: Period) {
  const series = quotes.map(closePoints);
  if (!series.length || series.some((points) => !points.length)) return { dates: [], values: [] };
  const maps = series.map((points) => new Map(points.map((point) => [point.date, point.value])));
  const common = series[0].filter((point) => maps.every((values) => values.has(point.date)));
  const dates = inPeriod(common, period).map((point) => point.date);
  const values = maps.map((points) => dates.map((date) => (points.get(date)! / points.get(dates[0])! - 1) * 100));
  return { dates, values };
}

export function drawdowns(points: ClosePoint[]): number[] {
  let peak = 0;
  return points.map(({ value }) => { peak = Math.max(peak, value); return value / peak - 1; });
}

export function riskMetrics(points: ClosePoint[]) {
  const returns = points.slice(1).map((point, index) => Math.log(point.value / points[index].value));
  const mean = returns.reduce((total, value) => total + value, 0) / returns.length;
  return {
    range: points.length >= 2 ? points.at(-1)!.value / points[0].value - 1 : null,
    volatility: returns.length >= 2 ? Math.sqrt(returns.reduce((total, value) => total + (value - mean) ** 2, 0)
      / (returns.length - 1) * 252) : null,
    drawdown: points.length >= 2 ? Math.min(...drawdowns(points)) : null,
  };
}

export const GOLD_RESEARCH_STEPS = [
  { stage: "fetch", label: "数据采集" },
  { stage: "researcher", label: "研究员" },
  { stage: "critic", label: "批判者" },
  { stage: "evaluation", label: "本地诊断" },
  { stage: "statistician", label: "统计员" },
  { stage: "orchestrator", label: "统筹者" },
  { stage: "final_audit", label: "独立终审" },
  { stage: "complete", label: "报告归档" },
] as const;

export const GOLD_DELIBERATION_STEPS = [
  { stage: "fetch", label: "数据采集" },
  { stage: "evaluation", label: "本地诊断" },
  { stage: "independent_analysis", label: "独立分析" },
  { stage: "cross_review", label: "交叉复核" },
  { stage: "analyst_revision", label: "分析师修订" },
  { stage: "orchestrator", label: "统筹者" },
  { stage: "final_audit", label: "独立终审" },
  { stage: "advice_revision", label: "作者修订" },
  { stage: "final_reaudit", label: "独立复审" },
  { stage: "complete", label: "报告归档" },
] as const;

export const GOLD_RESEARCH_STATUSES = {
  QUEUED: "排队中", RUNNING: "运行中", COMPLETED: "已完成", FAILED: "失败",
};

type GoldInvestmentReason = NonNullable<GoldResearchResult["investment_advice"]>["reasons"][number];
const GOLD_INVESTMENT_EVIDENCE_LABELS: Record<GoldInvestmentReason["evidence_key"], string> = {
  gold_return_126: "近126观测收益（约6个月）",
  gold_return_252: "近252观测收益（约12个月）",
  gold_ma120_gap: "收盘相对 MA120 偏离",
  gold_ma250_gap: "收盘相对 MA250 偏离",
  gold_drawdown_252: "距近252观测高点回撤（约12个月）",
  diagnostic_excess_return: "诊断收益差（策略减同仓位持有）",
  double_cost_return: "双倍成本诊断收益",
  independent_oos: "独立样本外验证",
};

export function goldResearchPriceFormat(evaluation: Pick<GoldResearchResult["evaluation"], "symbol" | "unit">) {
  const spot = evaluation.unit ? evaluation.unit === "CNY/g" : evaluation.symbol === "Au99.99";
  return { spot, unit: spot ? "元/克" : "元/份", digits: spot ? 2 : 3 };
}

export function goldInvestmentEvidence(reason: GoldInvestmentReason): string {
  const value = reason.evidence_key === "independent_oos"
    ? typeof reason.value === "boolean" ? reason.value ? "通过" : "未通过" : "未返回"
    : display(reason.value, true);
  return `${GOLD_INVESTMENT_EVIDENCE_LABELS[reason.evidence_key] ?? "未识别证据"}：${value}`;
}

export function goldTimestamp(value?: string | null): string {
  if (!value) return "未记录";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "未记录";
}

export function isGoldResearchActive(run: GoldResearchRun): boolean {
  return run.status === "QUEUED" || run.status === "RUNNING";
}

export function activeGoldResearchRunId(status: GoldResearchStatus | null, runs: GoldResearchRun[]): string | null {
  if (status && Object.hasOwn(status, "active_gold_run_id")) return status.active_gold_run_id ?? null;
  return status?.active_run_id ?? goldResearchHistory(runs).find(isGoldResearchActive)?.run_id ?? null;
}

export function shouldHydrateGoldDraft(requestRevision: number, currentRevision: number, dirtyAtStart: boolean, saving: boolean): boolean {
  return requestRevision === currentRevision && (!dirtyAtStart || saving);
}

export function goldResearchHistory(runs: GoldResearchRun[]): GoldResearchRun[] {
  const unique = new Map<string, GoldResearchRun>();
  for (const run of runs) {
    const previous = unique.get(run.run_id);
    if (!previous || Date.parse(run.updated_at) > Date.parse(previous.updated_at)) unique.set(run.run_id, run);
  }
  return [...unique.values()].sort((first, second) => Date.parse(second.created_at) - Date.parse(first.created_at)).slice(0, 20);
}

export function goldResearchRunLabel(run: GoldResearchRun): string {
  return `${goldTimestamp(run.created_at)} · ${run.request.symbol} · ${GOLD_RESEARCH_STATUSES[run.status]}`;
}

export function goldResearchReport(runs: GoldResearchRun[], selectedId: string): GoldResearchRun | undefined {
  const completed = goldResearchHistory(runs).filter((run) => run.status === "COMPLETED" && run.result);
  return completed.find((run) => run.run_id === selectedId) ?? completed[0];
}

export function goldResearchRequest(symbol: string, percent: string, focus: string): GoldResearchRequest {
  const weight = number(percent);
  if (!GOLD_INSTRUMENTS.some((item) => item.symbol === symbol)) throw new Error("请选择受支持的黄金研究对象。");
  if (weight === null || weight <= 0 || weight > 30) throw new Error("研究仓位上限须大于 0% 且不超过 30%。");
  if (!focus.trim()) throw new Error("研究重点不能为空。");
  if (focus.trim().length > 1000) throw new Error("研究重点不能超过 1000 字符。");
  return { symbol, max_weight: weight / 100, focus: focus.trim() };
}

export function isGoldDeliberationResult(result: GoldResearchResult): result is GoldResearchResult & { deliberation: GoldDeliberation } {
  return result.deliberation?.protocol === "evidence-deliberation-v1";
}

export function isGoldDeliberationRun(run?: GoldResearchRun, protocol?: string): boolean {
  if (run?.result) return isGoldDeliberationResult(run.result);
  if (run?.protocol) return run.protocol === "gold-deliberation-v4";
  if (run && ["researcher", "critic", "statistician"].includes(run.stage)) return false;
  if (run && ["independent_analysis", "cross_review", "analyst_revision"].includes(run.stage)) return true;
  return (!run || isGoldResearchActive(run)) && protocol === "gold-deliberation-v4";
}

export function goldResearchSteps(run?: GoldResearchRun, protocol?: string) {
  return isGoldDeliberationRun(run, protocol) ? GOLD_DELIBERATION_STEPS : GOLD_RESEARCH_STEPS;
}

export function goldResearchStageLabel(stage: GoldResearchStage | undefined): string | undefined {
  if (stage === "queued") return "排队中";
  if (stage === "failed") return "失败";
  return [...GOLD_RESEARCH_STEPS, ...GOLD_DELIBERATION_STEPS].find((step) => step.stage === stage)?.label;
}

export function goldResearchStepState(run: GoldResearchRun | undefined,
  stage: Exclude<GoldResearchStage, "queued" | "failed">, protocol?: string) {
  if (!run || run.status === "FAILED" || run.status === "QUEUED") return "pending";
  const steps = goldResearchSteps(run, protocol);
  const index = steps.findIndex((item) => item.stage === stage);
  if (index < 0 && stage !== "advice_revision" && stage !== "final_reaudit") return "skipped";
  const deliberation = isGoldDeliberationRun(run, protocol);
  const audit = run.result?.advice_audit;
  if (stage === "advice_revision" || stage === "final_reaudit") {
    if (run.status === "COMPLETED") {
      if (!audit) return "skipped";
      if (audit.rounds.length > 1) return "complete";
      return audit.rounds[0]?.review.decision === "REVISE" ? "unconfirmed" : "skipped";
    }
    if (run.stage === stage) return "current";
    return stage === "advice_revision" && run.stage === "final_reaudit" ? "complete" : "pending";
  }
  if (stage === "final_audit" && run.status === "COMPLETED") {
    if (!audit) return deliberation ? "unconfirmed" : "skipped";
    return audit.status === "UNAVAILABLE" || !audit.rounds.length ? "unconfirmed" : "complete";
  }
  const currentStage = !deliberation && (run.stage === "advice_revision" || run.stage === "final_reaudit") ? "final_audit" : run.stage;
  const current = steps.findIndex((item) => item.stage === currentStage);
  if (stage === "analyst_revision") {
    if (run.stage === stage) return "current";
    const review = run.result?.deliberation;
    if (review?.revisions.length) return "complete";
    if (review?.review_status === "NOT_NEEDED" || review?.review_status === "BUDGET_SKIPPED") return "skipped";
    return run.status === "COMPLETED" || index < current ? "unconfirmed" : "pending";
  }
  if (run.status === "COMPLETED" || index < current) return "complete";
  return index === current ? "current" : "pending";
}

export function goldEvidenceItem(evidence: GoldEvidencePackage | null | undefined, id: string) {
  return evidence && Object.hasOwn(evidence.items, id) ? evidence.items[id] : undefined;
}

export function goldEvidenceValue(value: GoldEvidencePackage["items"][string]["value"] | undefined): string {
  if (value === null || value === undefined || value === "" || typeof value === "number" && !Number.isFinite(value)) return "未提供";
  return String(value);
}

export function goldLargestDisagreement(deliberation: GoldDeliberation) {
  const directions: Record<string, number> = { BULLISH: 1, NEUTRAL: 0, BEARISH: -1 };
  const kinds = { DIRECTION: 2, EVIDENCE_GAP: 1, CONFIDENCE: 0 };
  function score(disagreement: GoldDeliberation["disagreements"][number]) {
    const [first, second] = disagreement.analysts.map((analyst) => deliberation.initial_states.find((state) => state.analyst === analyst));
    const span = first && second && directions[first.view] !== undefined && directions[second.view] !== undefined
      ? Math.abs(directions[first.view] - directions[second.view]) : 0;
    const confidenceGap = first && second ? Math.abs(first.confidence - second.confidence) : 0;
    return [span, kinds[disagreement.kind], confidenceGap];
  }
  return deliberation.disagreements.reduce<GoldDeliberation["disagreements"][number] | undefined>((largest, disagreement) => {
    if (!largest) return disagreement;
    const previous = score(largest);
    const next = score(disagreement);
    const difference = next.findIndex((value, index) => value !== previous[index]);
    return difference >= 0 && next[difference] > previous[difference] ? disagreement : largest;
  }, undefined);
}

export function pollGoldResearchRun(
  runId: string,
  load: (id: string, signal: AbortSignal) => Promise<GoldResearchRun>,
  onRun: (run: GoldResearchRun) => void,
  onError: (error: unknown) => void,
): () => void {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  async function poll() {
    try {
      const run = await load(runId, controller.signal);
      if (controller.signal.aborted) return;
      onRun(run);
      if (isGoldResearchActive(run)) timer = setTimeout(() => void poll(), 2000);
    } catch (error) {
      if (!controller.signal.aborted) onError(error);
    }
  }
  void poll();
  return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer); };
}