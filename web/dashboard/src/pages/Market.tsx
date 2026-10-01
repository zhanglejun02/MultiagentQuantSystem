import { Activity, CalendarClock, Database, ShieldCheck } from "lucide-react";
import { DominantFuturesContract, api, fmt } from "../api";
import DataTable, { Column } from "../components/DataTable";
import StatCard from "../components/StatCard";
import { useAsync } from "../hooks";

export default function Market() {
  const { data: status, error: statusError, loading } = useAsync(() => api.marketDataStatus(), []);
  const { data: response, error: snapshotError } = useAsync(
    () => api.latestMarketSnapshot().catch((error) => {
      if (String(error).includes(": 404 ")) return null;
      throw error;
    }),
    [],
  );
  const snapshot = response?.snapshot;
  const error = statusError || snapshotError;

  const futuresColumns: Column<DominantFuturesContract>[] = [
    { key: "product", title: "品种", render: (row) => <strong>{row.product.toUpperCase()}</strong> },
    { key: "symbol", title: "主力合约", render: (row) => <span className="mono">{row.symbol}</span> },
    { key: "settlement", title: "结算价", align: "right", render: (row) => fmt.num(row.settlement) },
    {
      key: "return",
      title: "涨跌",
      align: "right",
      render: (row) => <span className={row.return >= 0 ? "market-up" : "market-down"}>{fmt.pct(row.return)}</span>,
    },
    { key: "volume", title: "成交量", align: "right", render: (row) => fmt.num(row.volume, 0) },
    { key: "open_interest", title: "持仓量", align: "right", render: (row) => fmt.num(row.open_interest, 0) },
  ];

  if (error) return <div className="error-box">市场数据加载失败：{error}</div>;
  if (loading) return <div className="panel">正在读取市场快照…</div>;
  if (!status?.ready || !snapshot) {
    return (
      <div className="market-page">
        <header className="page-header">
          <div><div className="eyebrow">MARKET DATA</div><h1>每日市场</h1><p>A 股与国内期货收盘数据</p></div>
        </header>
        <div className="market-empty panel">
          <Database size={28} />
          <h2>尚无已发布市场快照</h2>
          <p>运行日度数据流水线且通过质量门禁后，此处会展示真实 EOD 数据。</p>
        </div>
      </div>
    );
  }

  const equity = snapshot.equity;
  const breadthTotal = Math.max(1, equity.advancers + equity.decliners + equity.unchanged);

  return (
    <div className="market-page">
      <header className="page-header">
        <div>
          <div className="eyebrow">MARKET DATA</div>
          <h1>每日市场</h1>
          <p>A 股宽度与国内期货主力合约收盘快照</p>
        </div>
        <div className={`market-quality ${status.stale ? "stale" : ""}`}>
          {status.stale ? <CalendarClock size={17} /> : <ShieldCheck size={17} />}
          <div><strong>{status.stale ? "数据已陈旧" : "质量门禁通过"}</strong><span>{snapshot.trade_date}</span></div>
        </div>
      </header>

      <div className="grid cols-4 metric-grid">
        <StatCard label="覆盖股票" value={fmt.num(equity.symbols, 0)} />
        <StatCard label="上涨家数" value={fmt.num(equity.advancers, 0)} tone="up" />
        <StatCard label="下跌家数" value={fmt.num(equity.decliners, 0)} tone="down" />
        <StatCard label="收益中位数" value={fmt.pct(equity.median_return)} tone={equity.median_return >= 0 ? "up" : "down"} />
      </div>

      <section className="market-breadth panel" aria-labelledby="breadth-title">
        <div className="section-heading">
          <div><h2 id="breadth-title">A 股市场宽度</h2><p>全市场日线横截面</p></div>
          <strong>{fmt.num(equity.total_amount / 100_000_000, 2)} 亿元</strong>
        </div>
        <div className="breadth-bar" aria-label="上涨、平盘与下跌家数比例">
          <span className="advancing" style={{ width: `${equity.advancers / breadthTotal * 100}%` }} />
          <span className="flat" style={{ width: `${equity.unchanged / breadthTotal * 100}%` }} />
          <span className="declining" style={{ width: `${equity.decliners / breadthTotal * 100}%` }} />
        </div>
        <div className="breadth-legend">
          <span><i className="advancing" />上涨 {equity.advancers}</span>
          <span><i className="flat" />平盘 {equity.unchanged}</span>
          <span><i className="declining" />下跌 {equity.decliners}</span>
        </div>
      </section>

      <div className="section-title">
        <div><h2>期货主力</h2><p>按持仓量、成交量和合约代码稳定选择</p></div>
        <span className="as-of"><Activity size={13} /> {snapshot.futures.contracts} 个合约</span>
      </div>
      <div className="panel">
        <DataTable
          columns={futuresColumns}
          rows={snapshot.futures.dominant_contracts}
          rowKey={(row) => row.symbol}
          empty="暂无期货主力数据"
        />
      </div>

      <div className="market-lineage">
        <span>Market Snapshot <b className="mono">{status.market_snapshot_id}</b></span>
        <span>Data Snapshot <b className="mono">{status.data_snapshot_id}</b></span>
        <span>Quality Report <b className="mono">{status.quality_report_id}</b></span>
      </div>
    </div>
  );
}