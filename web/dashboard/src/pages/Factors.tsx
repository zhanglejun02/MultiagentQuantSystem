import { FormEvent, useState } from "react";
import {
  ArrowRight,
  Ban,
  BookOpen,
  Braces,
  ChartNoAxesCombined,
  FlaskConical,
  History,
  Layers3,
  Play,
  Plus,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import {
  Factor,
  FactorCreate,
  FactorEvaluateRequest,
  FactorRun,
  FactorState,
  api,
  fmt,
} from "../api";
import Chart, { areaGradient, chartColors } from "../components/Chart";
import DataTable, { Column } from "../components/DataTable";
import StatCard from "../components/StatCard";
import { useAsync } from "../hooks";

type LabView = "workspace" | "explorer" | "runs" | "library";

const TEMPLATES = [
  {
    name: "波动调整反转",
    badge: "推荐起点",
    description: "短期价格变化按近期波动率归一化，观察横截面反转。",
    expression: "rank(-delta(close, 5) / ts_std(return_1d, 20))",
    inputs: "Price · Return",
    horizon: 1,
    icon: FlaskConical,
  },
  {
    name: "中期动量",
    badge: "趋势基线",
    description: "使用过去 20 个交易日价格变化建立可解释的动量基线。",
    expression: "rank(delta(close, 20))",
    inputs: "Price",
    horizon: 5,
    icon: ChartNoAxesCombined,
  },
  {
    name: "成交量持续性",
    badge: "量价特征",
    description: "衡量近期成交量相对前一日的持续变化，不接触未来数据。",
    expression: "rank(ts_mean(volume / delay(volume, 1), 10))",
    inputs: "Volume",
    horizon: 1,
    icon: Layers3,
  },
];

const EMPTY_FACTOR: FactorCreate = {
  factor_key: "",
  version: "1.0.0",
  name: "",
  category: "reversal",
  description: "",
  owner: "research",
  code_ref: "factor_dsl",
  parameters: {},
  metrics: {},
};

const DEFAULT_EVALUATION: FactorEvaluateRequest = {
  expression: TEMPLATES[0].expression,
  universe_id: "cn_a_all",
  start_date: "2024-02-01",
  end_date: "2024-05-31",
  target: "NEXT_OPEN_TO_CLOSE",
  horizon: 1,
};

function numberMetric(source: Record<string, unknown>, key: string): number | undefined {
  const value = source[key];
  return typeof value === "number" ? value : undefined;
}

export default function Factors() {
  const [view, setView] = useState<LabView>("workspace");
  const [refresh, setRefresh] = useState(0);
  const { data: factors, error: factorError } = useAsync(() => api.factors(), [refresh]);
  const { data: runs, error: runsError } = useAsync(() => api.factorRuns(), [refresh]);
  const [evaluation, setEvaluation] = useState(DEFAULT_EVALUATION);
  const [selectedRun, setSelectedRun] = useState<FactorRun | null>(null);
  const [factorForm, setFactorForm] = useState<FactorCreate>(EMPTY_FACTOR);
  const [showCreate, setShowCreate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  const factorRows = factors ?? [];
  const runRows = runs ?? [];

  function loadTemplate(template: (typeof TEMPLATES)[number]) {
    setEvaluation((current) => ({
      ...current,
      expression: template.expression,
      horizon: template.horizon,
    }));
    setSelectedRun(null);
    setView("explorer");
  }

  async function evaluate() {
    setBusy(true);
    setActionError("");
    try {
      const run = await api.evaluateFactor(evaluation);
      setSelectedRun(run);
      setRefresh((value) => value + 1);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "评估失败");
    } finally {
      setBusy(false);
    }
  }

  function prepareRegistration(run: FactorRun) {
    setFactorForm({
      ...EMPTY_FACTOR,
      factor_key: `factor_${run.run_id.slice(-6).toLowerCase()}`,
      name: "未命名候选因子",
      code_ref: `factor_dsl:${run.run_id}`,
      parameters: {
        expression: run.expression,
        evaluation_run_id: run.run_id,
        inputs: run.inputs,
        universe_id: run.universe_id,
        target: run.target,
        horizon: run.horizon,
      },
    });
    setShowCreate(true);
    setView("library");
  }

  async function createFactor(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setActionError("");
    try {
      await api.createFactor(factorForm);
      setFactorForm(EMPTY_FACTOR);
      setShowCreate(false);
      setRefresh((value) => value + 1);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "登记失败");
    } finally {
      setBusy(false);
    }
  }

  async function transition(factor: Factor, target: FactorState) {
    const action = "停用";
    if (!confirm(`确认${action}因子 ${factor.factor_key}@${factor.version}？`)) return;
    setBusy(true);
    setActionError("");
    try {
      await api.transitionFactor(factor.factor_id, target, `${action} from Alpha Lab`);
      setRefresh((value) => value + 1);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "状态更新失败");
    } finally {
      setBusy(false);
    }
  }

  const runColumns: Column<FactorRun>[] = [
    { key: "run_id", title: "Run", render: (run) => <span className="mono">{run.run_id.slice(-10)}</span> },
    { key: "expression", title: "表达式", render: (run) => <span className="mono expression-cell">{run.expression}</span> },
    { key: "period", title: "样本区间", render: (run) => `${run.start_date} → ${run.end_date}` },
    { key: "rank_ic", title: "Rank IC", align: "right", render: (run) => fmt.num(numberMetric(run.metrics, "rank_ic"), 4) },
    { key: "icir", title: "ICIR", align: "right", render: (run) => fmt.num(numberMetric(run.metrics, "icir"), 3) },
    { key: "coverage", title: "覆盖率", align: "right", render: (run) => fmt.pct(numberMetric(run.metrics, "coverage")) },
    { key: "status", title: "状态", render: (run) => <span className="badge ok">{run.status}</span> },
    {
      key: "action",
      title: "操作",
      render: (run) => <button className="button-compact" onClick={() => { setSelectedRun(run); setView("explorer"); }}>查看报告</button>,
    },
  ];

  const factorColumns: Column<Factor>[] = [
    {
      key: "name",
      title: "因子",
      render: (factor) => <div><div className="factor-name">{factor.name}</div><div className="mono muted">{factor.factor_key}@{factor.version}</div></div>,
    },
    { key: "category", title: "分类", render: (factor) => <span className="factor-tag">{factor.category}</span> },
    { key: "state", title: "状态", render: (factor) => <span className={`badge factor-${factor.state}`}>{factor.state}</span> },
    { key: "owner", title: "负责人" },
    { key: "code_ref", title: "来源", render: (factor) => <span className="mono muted">{factor.code_ref}</span> },
    { key: "updated_at", title: "更新于", render: (factor) => factor.updated_at.slice(0, 19) },
    {
      key: "actions",
      title: "操作",
      render: (factor) => <div className="table-actions">
        {factor.state !== "DEPRECATED" && <button className="button-compact danger" disabled={busy} onClick={() => transition(factor, "DEPRECATED")}><Ban size={13} /> 停用</button>}
      </div>,
    },
  ];

  const activeFactors = factorRows.filter(
    (factor) => factor.state === "STRATEGY_ELIGIBLE"
  ).length;
  const latestRun = selectedRun ?? runRows[0] ?? null;

  return (
    <div className="alpha-lab">
      <div className="lab-topline">
        <span>研究工作台</span><ArrowRight size={13} /><strong>Alpha Lab</strong>
        <div className="lab-mode"><span /> PIT 严格模式</div>
      </div>

      <header className="lab-titlebar">
        <div className="lab-title">
          <span className="lab-title-icon"><FlaskConical size={25} /></span>
          <div><h1>Alpha Lab</h1><p>人工因子研究、独立评估与版本晋级</p></div>
        </div>
        <div className="lab-title-actions">
          <button className="primary lab-run" onClick={() => setView("explorer")}><Play size={15} /> 打开 Explorer</button>
          <div className="lab-brand-mark" aria-hidden="true">
            <img src="/characters/gengar.png" alt="" />
          </div>
        </div>
      </header>

      <div className="lab-notice"><span>V0</span> 当前使用本地 PIT 合成市场数据；因子评估与策略回测相互独立。<strong>不代表真实收益</strong></div>

      <nav className="lab-tabs" aria-label="Alpha Lab views">
        {([
          ["workspace", "工作台", Sparkles],
          ["explorer", "Explorer", Braces],
          ["runs", "Runs", History],
          ["library", "Library", BookOpen],
        ] as const).map(([key, label, Icon]) => (
          <button key={key} className={view === key ? "active" : ""} onClick={() => setView(key)}><Icon size={14} /> {label}</button>
        ))}
      </nav>

      {(factorError || runsError || actionError) && <div className="error-box">{factorError || runsError || actionError}</div>}

      {view === "workspace" && <>
        <section className="lab-section">
          <div className="section-heading"><div><h2>研究模板</h2><p>选择一个可解释基线，再进入 Explorer 调整公式与评估口径。</p></div></div>
          <div className="template-grid">
            {TEMPLATES.map((template) => <article className="template-card" key={template.name}>
              <span className="template-badge">{template.badge}</span>
              <template.icon size={27} />
              <h3>{template.name}</h3>
              <p>{template.description}</p>
              <dl><div><dt>输入</dt><dd>{template.inputs}</dd></div><div><dt>预测周期</dt><dd>{template.horizon}D</dd></div><div><dt>市场</dt><dd>CN A</dd></div></dl>
              <button onClick={() => loadTemplate(template)}>载入 Explorer <ArrowRight size={13} /></button>
            </article>)}
          </div>
        </section>
        <div className="grid cols-4 lab-summary">
          <StatCard label="评估运行" value={String(runRows.length)} />
          <StatCard label="因子版本" value={String(factorRows.length)} />
          <StatCard label="策略可用" value={String(activeFactors)} tone="ok" />
          <StatCard label="搜索预算" value="V1 启用" />
        </div>
      </>}

      {view === "explorer" && <>
        <section className="explorer-layout">
          <div className="explorer-editor">
            <div className="section-heading"><div><h2>Factor Explorer</h2><p>公式只允许白名单字段与算子，不执行任意 Python。</p></div></div>
            <label className="field-label">因子表达式<textarea className="expression-editor mono" value={evaluation.expression} onChange={(event) => setEvaluation({ ...evaluation, expression: event.target.value })} /></label>
            <div className="explorer-controls">
              <label>股票池<select value={evaluation.universe_id} onChange={(event) => setEvaluation({ ...evaluation, universe_id: event.target.value })}><option value="cn_a_all">CN A Fixture</option></select></label>
              <label>开始日期<input type="date" value={evaluation.start_date} onChange={(event) => setEvaluation({ ...evaluation, start_date: event.target.value })} /></label>
              <label>结束日期<input type="date" value={evaluation.end_date} onChange={(event) => setEvaluation({ ...evaluation, end_date: event.target.value })} /></label>
              <label>目标<select value={evaluation.target} onChange={(event) => setEvaluation({ ...evaluation, target: event.target.value })}><option value="NEXT_OPEN_TO_CLOSE">次日开盘至收盘</option><option value="NEXT_CLOSE_TO_CLOSE">收盘至收盘</option><option value="NEXT_OPEN_TO_NEXT_OPEN">开盘至开盘</option></select></label>
              <label>周期<input type="number" min="1" max="20" value={evaluation.horizon} onChange={(event) => setEvaluation({ ...evaluation, horizon: Number(event.target.value) })} /></label>
            </div>
            <div className="operator-help mono">支持：rank · delta · delay · ts_mean · ts_std · abs · open/high/low/close/volume/amount/return_1d</div>
            <button className="primary" disabled={busy} onClick={evaluate}><Play size={14} /> {busy ? "评估中…" : "Evaluate"}</button>
          </div>
          <aside className="research-guardrails"><h3>PIT Guardrails</h3><div><strong>Score time</strong><span>T 日收盘后</span></div><div><strong>Execution</strong><span>T+1 开盘</span></div><div><strong>Data access</strong><span>available_at ≤ as_of</span></div><div><strong>Code execution</strong><span>DSL whitelist only</span></div></aside>
        </section>
        {latestRun && <FactorReport run={latestRun} onRegister={() => prepareRegistration(latestRun)} />}
      </>}

      {view === "runs" && <section className="lab-section"><div className="section-heading"><div><h2>Evaluation Runs</h2><p>Definition 与 Evaluation 分开保存，每次运行保留独立口径。</p></div><button onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={14} /> 刷新</button></div><div className="panel"><DataTable columns={runColumns} rows={runRows} rowKey={(run) => run.run_id} empty="尚无评估运行。" /></div></section>}

      {view === "library" && <section className="lab-section">
        <div className="section-heading"><div><h2>Factor Library</h2><p>因子定义、版本和生命周期；评估结果通过 Run ID 保持独立血缘。</p></div><button className="primary" onClick={() => setShowCreate((value) => !value)}><Plus size={14} /> 登记因子</button></div>
        {showCreate && <form className="panel factor-form" onSubmit={createFactor}><div className="form-grid">
          <label>因子标识<input required value={factorForm.factor_key} onChange={(event) => setFactorForm({ ...factorForm, factor_key: event.target.value })} placeholder="reversal_vol_adj" /></label>
          <label>版本<input required value={factorForm.version} onChange={(event) => setFactorForm({ ...factorForm, version: event.target.value })} /></label>
          <label>显示名称<input required value={factorForm.name} onChange={(event) => setFactorForm({ ...factorForm, name: event.target.value })} /></label>
          <label>分类<input required value={factorForm.category} onChange={(event) => setFactorForm({ ...factorForm, category: event.target.value })} /></label>
          <label>负责人<input required value={factorForm.owner} onChange={(event) => setFactorForm({ ...factorForm, owner: event.target.value })} /></label>
          <label>来源引用<input required value={factorForm.code_ref} onChange={(event) => setFactorForm({ ...factorForm, code_ref: event.target.value })} /></label>
        </div><label>研究假设<textarea value={factorForm.description} onChange={(event) => setFactorForm({ ...factorForm, description: event.target.value })} /></label><div className="form-actions"><button type="button" onClick={() => setShowCreate(false)}>取消</button><button className="primary" disabled={busy}>登记 PROPOSED</button></div></form>}
        <div className="panel"><DataTable columns={factorColumns} rows={factorRows} rowKey={(factor) => factor.factor_id} empty="尚未登记因子版本。" /></div>
      </section>}
    </div>
  );
}

function FactorReport({ run, onRegister }: { run: FactorRun; onRegister: () => void }) {
  const metrics = run.metrics;
  const quantiles = Object.entries(run.quantile_returns);
  return <section className="factor-report">
    <div className="section-heading"><div><h2>Evaluation Report</h2><p className="mono">{run.run_id} · {run.start_date} → {run.end_date}</p></div><button onClick={onRegister}><Plus size={14} /> 登记为候选</button></div>
    <div className="grid cols-4">
      <StatCard label="Rank IC" value={fmt.num(numberMetric(metrics, "rank_ic"), 4)} />
      <StatCard label="ICIR" value={fmt.num(numberMetric(metrics, "icir"), 3)} />
      <StatCard label="IC 胜率" value={fmt.pct(numberMetric(metrics, "ic_win_rate"))} />
      <StatCard label="覆盖率" value={fmt.pct(numberMetric(metrics, "coverage"))} />
      <StatCard label="L/S Sharpe" value={fmt.num(numberMetric(metrics, "long_short_sharpe"), 2)} />
      <StatCard label="L/S 年化" value={fmt.pct(numberMetric(metrics, "long_short_annual_return"))} />
      <StatCard label="L/S 最大回撤" value={fmt.pct(numberMetric(metrics, "long_short_max_drawdown"))} tone="warn" />
      <StatCard label="Top 组换手" value={fmt.pct(numberMetric(metrics, "turnover"))} />
    </div>
    <div className="grid cols-2 report-charts">
      <div className="panel"><h2>IC 时序</h2><Chart height={230} option={{ tooltip: { trigger: "axis" }, grid: { left: 54, right: 16, top: 18, bottom: 30 }, xAxis: { type: "category", data: run.ic_series.map((point) => point.date), boundaryGap: false }, yAxis: { type: "value" }, series: [{ type: "line", data: run.ic_series.map((point) => point.ic), showSymbol: false, itemStyle: { color: chartColors.purple }, areaStyle: { color: areaGradient(chartColors.purple, 0.2) }, markLine: { silent: true, symbol: "none", lineStyle: { color: "#cbbdce", width: 1 }, data: [{ yAxis: 0 }] } }] }} /></div>
      <div className="panel"><h2>五分位收益</h2><Chart height={230} option={{ tooltip: {}, grid: { left: 54, right: 16, top: 18, bottom: 30 }, xAxis: { type: "category", data: quantiles.map(([key]) => key) }, yAxis: { type: "value", axisLabel: { formatter: (value: number) => `${(value * 100).toFixed(1)}%` } }, series: [{ type: "bar", data: quantiles.map(([, value]) => value), itemStyle: { color: "#e84d9b" } }] }} /></div>
      <div className="panel"><h2>IC 衰减</h2><Chart height={210} option={{ tooltip: { trigger: "axis" }, grid: { left: 54, right: 16, top: 18, bottom: 30 }, xAxis: { type: "category", data: run.decay.map((point) => `${point.horizon}D`), boundaryGap: false }, yAxis: { type: "value" }, series: [{ type: "line", data: run.decay.map((point) => point.rank_ic), itemStyle: { color: chartColors.pink }, lineStyle: { width: 2.4 }, areaStyle: { color: areaGradient(chartColors.pink, 0.18) }, markLine: { silent: true, symbol: "none", lineStyle: { color: "#cbbdce", width: 1 }, data: [{ yAxis: 0 }] } }] }} /></div>
      <div className="panel"><h2>年度稳定性</h2><Chart height={210} option={{ tooltip: {}, grid: { left: 54, right: 16, top: 18, bottom: 30 }, xAxis: { type: "category", data: Object.keys(run.yearly_ic) }, yAxis: { type: "value" }, series: [{ type: "bar", data: Object.values(run.yearly_ic), itemStyle: { color: "#8b5cf6" } }] }} /></div>
    </div>
  </section>;
}
