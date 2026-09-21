import { Activity, Database, ShieldCheck } from "lucide-react";
import { api, fmt } from "../api";
import Chart, { areaGradient, chartColors } from "../components/Chart";
import StatCard from "../components/StatCard";
import { equityDerived, useAsync, useBacktestSelection } from "../hooks";

export default function Overview() {
  const { id, error: btError } = useBacktestSelection();
  const { data: equity } = useAsync(() => (id ? api.equity(id) : Promise.resolve([])), [id]);
  const { data: metrics } = useAsync(() => (id ? api.metrics(id) : Promise.resolve(null)), [id]);
  const { data: status } = useAsync(() => api.systemStatus(), []);
  const { data: health } = useAsync(() => api.health(), []);
  const { data: orders } = useAsync(() => (id ? api.orders(id) : Promise.resolve([])), [id]);

  if (btError) return <div className="error-box">加载失败：{btError}（请先运行样例回测并启动 API）</div>;
  if (!id) return <div className="panel">暂无回测结果。请运行 `python scripts/run_sample_backtest.py`。</div>;

  const dates = (equity ?? []).map((p) => p.date);
  const navs = (equity ?? []).map((p) => parseFloat(p.nav));
  const { daily, cum, dd } = equityDerived(navs);
  const lastNav = navs.at(-1) ?? 0;
  const lastDaily = daily.at(-1) ?? 0;
  const lastCum = cum.at(-1) ?? 0;
  const lastDd = dd.at(-1) ?? 0;
  const cash = parseFloat(equity?.at(-1)?.cash ?? "0") + parseFloat(equity?.at(-1)?.frozen_cash ?? "0");
  const exposure = lastNav ? parseFloat(equity?.at(-1)?.market_value ?? "0") / lastNav : 0;
  const openOrders = (orders ?? []).filter((o) =>
    ["CREATED", "RISK_APPROVED", "SUBMITTING", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"].includes(o.status)
  ).length;

  return (
    <div className="overview-page">
      <header className="page-header">
        <div>
          <div className="eyebrow">PERFORMANCE MONITOR</div>
          <h1>账户总览</h1>
          <p>回测账户净值、敞口与运行状态</p>
        </div>
        <div className="run-context">
          <span className="status-dot" />
          <div><strong>模拟 / 回测</strong><span className="mono" title={id}>{id}</span></div>
        </div>
      </header>

      <section aria-labelledby="core-metrics-title">
        <div className="section-title">
          <div><h2 id="core-metrics-title">核心指标</h2><p>最新交易日账户快照</p></div>
          <span className="as-of">{dates.at(-1) ?? "暂无日期"}</span>
        </div>
        <div className="grid cols-4 metric-grid">
        <StatCard label="NAV" value={fmt.num(lastNav)} />
        <StatCard label="当日 PnL" value={fmt.pct(lastDaily)} tone={lastDaily >= 0 ? "up" : "down"} />
        <StatCard label="累计 PnL" value={fmt.pct(lastCum)} tone={lastCum >= 0 ? "up" : "down"} />
        <StatCard label="当前回撤" value={fmt.pct(lastDd)} tone={lastDd < -0.05 ? "warn" : undefined} />
        </div>
        <div className="account-strip">
          <div><span>现金余额</span><strong>{fmt.num(cash)}</strong></div>
          <div><span>仓位敞口</span><strong>{fmt.pct(exposure)}</strong></div>
          <div><span>未完结订单</span><strong>{openOrders}</strong></div>
          <div><span>运行状态</span><strong className={status?.kill_switch ? "warn" : "ok"}>{status ? (status.kill_switch ? "KILL SWITCH" : status.runtime_state) : "…"}</strong></div>
        </div>
      </section>

      <section aria-labelledby="nav-chart-title">
        <div className="section-title">
          <div><h2 id="nav-chart-title">净值走势</h2><p>策略账户 NAV 历史序列</p></div>
        </div>
        <div className="panel chart-panel">
          <Chart
            option={{
              tooltip: { trigger: "axis" },
              grid: { left: 70, right: 20, top: 20, bottom: 40 },
              xAxis: { type: "category", data: dates, boundaryGap: false },
              yAxis: { type: "value", scale: true },
              series: [{
                type: "line",
                data: navs,
                showSymbol: false,
                name: "NAV",
                itemStyle: { color: chartColors.purple },
                areaStyle: { color: areaGradient(chartColors.purple, 0.2) },
              }],
            }}
          />
        </div>
      </section>

      <div className="grid cols-2">
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>累计收益</h2>
          <Chart
            height={220}
            option={{
              tooltip: { trigger: "axis", valueFormatter: (v) => fmt.pct(v as number) },
              grid: { left: 60, right: 20, top: 10, bottom: 30 },
              xAxis: { type: "category", data: dates, boundaryGap: false },
              yAxis: { type: "value", axisLabel: { formatter: (v: number) => (v * 100).toFixed(1) + "%" } },
              series: [{
                type: "line",
                data: cum,
                showSymbol: false,
                itemStyle: { color: lastCum >= 0 ? chartColors.red : chartColors.green },
                areaStyle: { color: areaGradient(lastCum >= 0 ? chartColors.red : chartColors.green, 0.22) },
                markLine: { silent: true, symbol: "none", lineStyle: { color: "#cbbdce", width: 1 }, data: [{ yAxis: 0 }] },
              }],
            }}
          />
        </div>
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>回撤</h2>
          <Chart
            height={220}
            option={{
              tooltip: { trigger: "axis", valueFormatter: (v) => fmt.pct(v as number) },
              grid: { left: 60, right: 20, top: 10, bottom: 30 },
              xAxis: { type: "category", data: dates, boundaryGap: false },
              yAxis: { type: "value", axisLabel: { formatter: (v: number) => (v * 100).toFixed(1) + "%" } },
              series: [
                {
                  type: "line",
                  data: dd,
                  showSymbol: false,
                  itemStyle: { color: chartColors.red },
                  areaStyle: { color: areaGradient(chartColors.red, 0.28) },
                  markLine: { silent: true, symbol: "none", lineStyle: { color: "#cbbdce", width: 1 }, data: [{ yAxis: 0 }] },
                },
              ],
            }}
          />
        </div>
      </div>

      <section aria-labelledby="health-title">
        <div className="section-title"><div><h2 id="health-title">系统健康</h2><p>关键服务与风险事件</p></div></div>
        <div className="health-grid">
          <div><Database size={17} /><span>API / DB</span><strong className={health?.status === "ok" ? "ok" : "warn"}>{health ? `${health.status} / ${health.db}` : "…"}</strong></div>
          <div><Activity size={17} /><span>PaperBroker / OMS</span><strong>{status ? `${status.paper_broker} / ${status.oms}` : "…"}</strong></div>
          <div><ShieldCheck size={17} /><span>风控拦截</span><strong className="warn">{String(metrics?.risk?.n_violations ?? "-")}</strong></div>
        </div>
      </section>
    </div>
  );
}
