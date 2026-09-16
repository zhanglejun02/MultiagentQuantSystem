import { api, fmt } from "../api";
import DataTable, { Column } from "../components/DataTable";
import StatCard from "../components/StatCard";
import { useAsync, useBacktestSelection } from "../hooks";

interface PosRow {
  symbol: string;
  total: number;
  available: number;
  avg_cost: number;
  last_price: number;
  market_value: number;
  weight: number;
  unrealized: number;
}

export default function Portfolio() {
  const { data: doc, error } = useAsync(() => api.latestPortfolio(), []);
  const { id } = useBacktestSelection();
  const { data: metrics } = useAsync(() => (id ? api.metrics(id) : Promise.resolve(null)), [id]);

  if (error) return <div className="error-box">加载失败：{error}</div>;
  if (!doc) return <div className="panel">加载中…</div>;

  const nav = parseFloat(doc.nav);
  const rows: PosRow[] = Object.entries(doc.positions)
    .map(([symbol, p]) => {
      const last = parseFloat(p.last_price);
      const cost = parseFloat(p.avg_cost);
      const mv = last * p.total;
      return {
        symbol,
        total: p.total,
        available: p.available,
        avg_cost: cost,
        last_price: last,
        market_value: mv,
        weight: nav ? mv / nav : 0,
        unrealized: (last - cost) * p.total,
      };
    })
    .sort((a, b) => b.market_value - a.market_value);

  const columns: Column<PosRow>[] = [
    { key: "symbol", title: "证券", render: (r) => <span className="mono">{r.symbol}</span> },
    { key: "total", title: "持仓", align: "right", render: (r) => r.total.toLocaleString() },
    { key: "available", title: "可卖 (T+1)", align: "right", render: (r) => r.available.toLocaleString() },
    { key: "avg_cost", title: "成本", align: "right", render: (r) => fmt.num(r.avg_cost) },
    { key: "last_price", title: "现价", align: "right", render: (r) => fmt.num(r.last_price) },
    { key: "market_value", title: "市值", align: "right", render: (r) => fmt.num(r.market_value) },
    { key: "weight", title: "权重", align: "right", render: (r) => fmt.pct(r.weight) },
    {
      key: "unrealized",
      title: "浮动盈亏",
      align: "right",
      render: (r) => (
        <span style={{ color: r.unrealized >= 0 ? "var(--up)" : "var(--down)" }}>{fmt.num(r.unrealized)}</span>
      ),
    },
  ];

  return (
    <div>
      <h1>Portfolio <span className="muted mono">(来源: {doc.source_artifact_id})</span></h1>
      <div className="grid cols-4">
        <StatCard label="NAV" value={fmt.num(nav)} />
        <StatCard label="市值" value={fmt.num(doc.market_value)} />
        <StatCard label="现金 (含冻结)" value={fmt.num(parseFloat(doc.cash) + parseFloat(doc.frozen_cash))} />
        <StatCard
          label="已实现 / 浮动盈亏"
          value={`${fmt.num(doc.realized_pnl)} / ${fmt.num(doc.unrealized_pnl)}`}
        />
        <StatCard label="费用 / 税" value={`${fmt.num(doc.fees)} / ${fmt.num(doc.taxes)}`} />
        <StatCard label="年化换手率" value={fmt.num(metrics?.execution?.annualized_turnover)} />
        <StatCard label="行业敞口" value="待接入行业分类数据" />
        <StatCard label="持仓数" value={String(rows.length)} />
      </div>
      <h2>持仓明细</h2>
      <div className="panel">
        <DataTable columns={columns} rows={rows} rowKey={(r) => r.symbol} />
      </div>
    </div>
  );
}
