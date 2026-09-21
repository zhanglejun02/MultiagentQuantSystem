import { api, fmt } from "../api";
import Chart, { areaGradient, chartColors } from "../components/Chart";
import StatCard from "../components/StatCard";
import { equityDerived, useAsync, useBacktestSelection } from "../hooks";

export default function Backtest() {
  const { backtests, id, setSelected, error } = useBacktestSelection();
  const { data: equity } = useAsync(() => (id ? api.equity(id) : Promise.resolve([])), [id]);
  const { data: metrics } = useAsync(() => (id ? api.metrics(id) : Promise.resolve(null)), [id]);
  const { data: orders } = useAsync(() => (id ? api.orders(id) : Promise.resolve([])), [id]);
  const { data: trades } = useAsync(() => (id ? api.trades(id) : Promise.resolve([])), [id]);

  if (error) return <div className="error-box">加载失败：{error}</div>;
  if (!id) return <div className="panel">暂无回测。请运行 `python scripts/run_sample_backtest.py`。</div>;

  const dates = (equity ?? []).map((p) => p.date);
  const navs = (equity ?? []).map((p) => parseFloat(p.nav));
  const { dd } = equityDerived(navs);
  const perf = metrics?.performance ?? {};
  const exec = metrics?.execution ?? {};
  const monthly = Object.entries(perf.monthly_returns ?? {}) as [string, number][];
  const yearly = Object.entries(perf.yearly_returns ?? {}) as [string, number][];
  const rejects = (orders ?? []).filter((o) => o.status === "REJECTED").length;
  // violations = 回测中的风控拒绝/规则约束记录（预期行为），非晋升校验结果
  const violations: string[] = metrics?.violations ?? [];
  const icMeanKey = Object.keys(perf).find((key) => key.startsWith("rank_ic_") && key.endsWith("_mean"));
  const icirKey = Object.keys(perf).find((key) => key.startsWith("rank_ic_") && key.endsWith("_icir"));

  return (
    <div>
      <h1>Backtest</h1>
      <div className="toolbar">
        <select value={id} onChange={(e) => setSelected(e.target.value)}>
          {backtests.map((b) => (
            <option key={b.artifact_id} value={b.artifact_id}>
              {b.artifact_id} ({b.created_at.slice(0, 19)})
            </option>
          ))}
        </select>
        <span className={`badge ${violations.length === 0 ? "ok" : "warn"}`}>
          风控/规则拦截 {violations.length} 条（回测预期行为）
        </span>
      </div>

      <div className="grid cols-4">
        <StatCard label="总收益" value={fmt.pct(perf.total_return)} tone={(perf.total_return ?? 0) >= 0 ? "up" : "down"} />
        <StatCard label="年化收益" value={fmt.pct(perf.annualized_return)} />
        <StatCard label="Sharpe" value={fmt.num(perf.sharpe)} />
        <StatCard label="Sortino" value={fmt.num(perf.sortino)} />
        <StatCard label="最大回撤" value={fmt.pct(perf.max_drawdown)} tone="warn" />
        <StatCard label="Calmar" value={fmt.num(perf.calmar)} />
        <StatCard label="年化波动" value={fmt.pct(perf.annualized_volatility)} />
        <StatCard
          label={`RankIC / ICIR (${perf.prediction_target ?? "unspecified"}, h=${perf.prediction_horizon ?? "-"})`}
          value={`${fmt.num(icMeanKey ? perf[icMeanKey] : undefined, 4)} / ${fmt.num(icirKey ? perf[icirKey] : undefined, 3)}`}
        />
        <StatCard label="年化换手" value={fmt.num(exec.annualized_turnover)} />
        <StatCard label="交易成本" value={fmt.num(exec.transaction_cost)} />
        <StatCard label="成交率" value={fmt.pct(exec.fill_ratio)} />
        <StatCard label="订单/成交/拒绝" value={`${(orders ?? []).length} / ${(trades ?? []).length} / ${rejects}`} />
      </div>

      <h2>Equity 与回撤</h2>
      <div className="panel">
        <Chart
          height={380}
          option={{
            tooltip: { trigger: "axis" },
            legend: { data: ["NAV", "Drawdown"], textStyle: { color: "#8493a8" } },
            grid: [
              { left: 70, right: 20, top: 30, height: 200 },
              { left: 70, right: 20, top: 270, height: 70 },
            ],
            xAxis: [
              { type: "category", data: dates, gridIndex: 0, show: false, boundaryGap: false },
              { type: "category", data: dates, gridIndex: 1, boundaryGap: false },
            ],
            yAxis: [
              { type: "value", scale: true, gridIndex: 0 },
              { type: "value", gridIndex: 1, axisLabel: { formatter: (v: number) => (v * 100).toFixed(0) + "%" } },
            ],
            series: [
              {
                name: "NAV", type: "line", data: navs, showSymbol: false, xAxisIndex: 0, yAxisIndex: 0,
                itemStyle: { color: chartColors.purple }, areaStyle: { color: areaGradient(chartColors.purple, 0.18) },
              },
              {
                name: "Drawdown", type: "line", data: dd, showSymbol: false,
                xAxisIndex: 1, yAxisIndex: 1, areaStyle: { color: areaGradient(chartColors.red, 0.3) },
                itemStyle: { color: chartColors.red },
              },
            ],
          }}
        />
      </div>

      <div className="grid cols-2">
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>月度收益</h2>
          <Chart
            height={220}
            option={{
              tooltip: { valueFormatter: (v) => fmt.pct(v as number) },
              grid: { left: 60, right: 20, top: 10, bottom: 30 },
              xAxis: { type: "category", data: monthly.map(([m]) => m) },
              yAxis: { type: "value", axisLabel: { formatter: (v: number) => (v * 100).toFixed(1) + "%" } },
              series: [{
                type: "bar",
                data: monthly.map(([, v]) => ({ value: v, itemStyle: { color: v >= 0 ? "#e05a5a" : "#2fa46a" } })),
              }],
            }}
          />
        </div>
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>年度收益</h2>
          <Chart
            height={220}
            option={{
              tooltip: { valueFormatter: (v) => fmt.pct(v as number) },
              grid: { left: 60, right: 20, top: 10, bottom: 30 },
              xAxis: { type: "category", data: yearly.map(([y]) => y) },
              yAxis: { type: "value", axisLabel: { formatter: (v: number) => (v * 100).toFixed(1) + "%" } },
              series: [{
                type: "bar",
                data: yearly.map(([, v]) => ({ value: v, itemStyle: { color: v >= 0 ? "#e05a5a" : "#2fa46a" } })),
              }],
            }}
          />
        </div>
      </div>

      {violations.length > 0 && (
        <>
          <h2>风控/规则拦截记录（前 50）</h2>
          <div className="panel mono" style={{ fontSize: 12, maxHeight: 260, overflowY: "auto" }}>
            {violations.slice(0, 50).map((v, i) => (
              <div key={i} style={{ padding: "3px 0", color: "var(--warn)" }}>{v}</div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
