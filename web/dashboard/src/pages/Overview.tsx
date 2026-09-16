import { api, fmt } from "../api";
import Chart from "../components/Chart";
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
    <div>
      <h1>Overview <span className="muted mono">({id}，paper/backtest 数据)</span></h1>
      <div className="grid cols-4">
        <StatCard label="NAV" value={fmt.num(lastNav)} />
        <StatCard label="当日 PnL" value={fmt.pct(lastDaily)} tone={lastDaily >= 0 ? "up" : "down"} />
        <StatCard label="累计 PnL" value={fmt.pct(lastCum)} tone={lastCum >= 0 ? "up" : "down"} />
        <StatCard label="当前回撤" value={fmt.pct(lastDd)} tone={lastDd < -0.05 ? "warn" : undefined} />
        <StatCard label="现金" value={fmt.num(cash)} />
        <StatCard label="仓位敞口" value={fmt.pct(exposure)} />
        <StatCard label="未完结订单" value={String(openOrders)} />
        <StatCard
          label="风控 / 系统"
          value={status ? (status.kill_switch ? "KILL SWITCH" : status.runtime_state) : "…"}
          tone={status?.kill_switch ? "warn" : "ok"}
        />
      </div>

      <h2>NAV 曲线</h2>
      <div className="panel">
        <Chart
          option={{
            tooltip: { trigger: "axis" },
            grid: { left: 70, right: 20, top: 20, bottom: 40 },
            xAxis: { type: "category", data: dates },
            yAxis: { type: "value", scale: true },
            series: [{ type: "line", data: navs, showSymbol: false, name: "NAV", lineStyle: { width: 1.5 } }],
          }}
        />
      </div>

      <div className="grid cols-2">
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>累计收益</h2>
          <Chart
            height={220}
            option={{
              tooltip: { trigger: "axis", valueFormatter: (v) => fmt.pct(v as number) },
              grid: { left: 60, right: 20, top: 10, bottom: 30 },
              xAxis: { type: "category", data: dates },
              yAxis: { type: "value", axisLabel: { formatter: (v: number) => (v * 100).toFixed(1) + "%" } },
              series: [{ type: "line", data: cum, showSymbol: false, areaStyle: { opacity: 0.15 } }],
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
              xAxis: { type: "category", data: dates },
              yAxis: { type: "value", axisLabel: { formatter: (v: number) => (v * 100).toFixed(1) + "%" } },
              series: [
                { type: "line", data: dd, showSymbol: false, areaStyle: { opacity: 0.25 }, itemStyle: { color: "#e05a5a" } },
              ],
            }}
          />
        </div>
      </div>

      <h2>系统健康</h2>
      <div className="grid cols-4">
        <StatCard label="API / DB" value={health ? `${health.status} / ${health.db}` : "…"} tone={health?.status === "ok" ? "ok" : "warn"} />
        <StatCard label="PaperBroker" value={status?.paper_broker ?? "…"} />
        <StatCard label="OMS" value={status?.oms ?? "…"} />
        <StatCard label="风险违规数" value={String(metrics?.risk?.n_violations ?? "-")} tone="warn" />
      </div>
    </div>
  );
}
