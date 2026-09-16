import { useState } from "react";
import { OrderRow, TradeRow, api, fmt } from "../api";
import DataTable, { Column } from "../components/DataTable";
import StatCard from "../components/StatCard";
import { useAsync, useBacktestSelection } from "../hooks";

function slippageBps(o: OrderRow): number | null {
  if (!o.avg_fill_price || !o.reference_price) return null;
  const fill = parseFloat(o.avg_fill_price);
  const ref = parseFloat(o.reference_price);
  if (!ref || !fill) return null;
  const signed = o.side === "BUY" ? fill / ref - 1 : ref / fill - 1;
  return signed * 10000;
}

export default function OrdersTrades() {
  const { backtests, id, setSelected, error } = useBacktestSelection();
  const [statusFilter, setStatusFilter] = useState("");
  const { data: orders } = useAsync(() => (id ? api.orders(id) : Promise.resolve([])), [id]);
  const { data: trades } = useAsync(() => (id ? api.trades(id) : Promise.resolve([])), [id]);

  if (error) return <div className="error-box">加载失败：{error}</div>;

  const all = orders ?? [];
  const filtered = statusFilter ? all.filter((o) => o.status === statusFilter) : all;
  const rejected = all.filter((o) => o.status === "REJECTED");
  const statuses = [...new Set(all.map((o) => o.status))].sort();

  const orderCols: Column<OrderRow>[] = [
    { key: "order_id", title: "订单", render: (r) => <span className="mono">{r.order_id.slice(-8)}</span> },
    { key: "created_at", title: "时间", render: (r) => r.created_at.slice(0, 10) },
    { key: "strategy_id", title: "策略" },
    { key: "symbol", title: "证券", render: (r) => <span className="mono">{r.symbol}</span> },
    { key: "side", title: "方向" },
    { key: "quantity", title: "数量", align: "right", render: (r) => r.quantity.toLocaleString() },
    { key: "filled_quantity", title: "成交量", align: "right", render: (r) => r.filled_quantity.toLocaleString() },
    { key: "avg_fill_price", title: "均价", align: "right", render: (r) => (r.avg_fill_price ? fmt.num(r.avg_fill_price) : "-") },
    {
      key: "slippage",
      title: "滑点(bps)",
      align: "right",
      render: (r) => {
        const s = slippageBps(r);
        return s === null ? "-" : s.toFixed(1);
      },
    },
    { key: "status", title: "状态", render: (r) => <span className={`badge ${r.status}`}>{r.status}</span> },
    { key: "reject_reason", title: "拒绝原因", render: (r) => <span className="muted">{r.reject_reason || "-"}</span> },
  ];

  const tradeCols: Column<TradeRow>[] = [
    { key: "fill_id", title: "成交", render: (r) => <span className="mono">{r.fill_id.slice(-8)}</span> },
    { key: "trade_date", title: "日期" },
    { key: "symbol", title: "证券", render: (r) => <span className="mono">{r.symbol}</span> },
    { key: "side", title: "方向" },
    { key: "quantity", title: "数量", align: "right", render: (r) => r.quantity.toLocaleString() },
    { key: "price", title: "价格", align: "right", render: (r) => fmt.num(r.price) },
    { key: "commission", title: "佣金", align: "right", render: (r) => fmt.num(r.commission) },
    { key: "stamp_tax", title: "印花税", align: "right", render: (r) => fmt.num(r.stamp_tax) },
    { key: "transfer_fee", title: "过户费", align: "right", render: (r) => fmt.num(r.transfer_fee) },
    { key: "order_id", title: "订单", render: (r) => <span className="mono muted">{r.order_id.slice(-8)}</span> },
  ];

  return (
    <div>
      <h1>Orders &amp; Trades</h1>
      <div className="toolbar">
        <select value={id ?? ""} onChange={(e) => setSelected(e.target.value)}>
          {backtests.map((b) => (
            <option key={b.artifact_id} value={b.artifact_id}>
              {b.artifact_id} ({b.created_at.slice(0, 19)})
            </option>
          ))}
        </select>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">全部状态</option>
          {statuses.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="grid cols-4">
        <StatCard label="订单总数" value={String(all.length)} />
        <StatCard label="成交笔数" value={String((trades ?? []).length)} />
        <StatCard label="拒绝数" value={String(rejected.length)} tone={rejected.length ? "warn" : undefined} />
        <StatCard
          label="部分成交"
          value={String(all.filter((o) => o.status === "PARTIALLY_FILLED").length)}
        />
      </div>

      <h2>订单（{filtered.length}）</h2>
      <div className="panel">
        <DataTable columns={orderCols} rows={filtered.slice(0, 500)} rowKey={(r) => r.order_id} />
      </div>

      <h2>成交（{(trades ?? []).length}）</h2>
      <div className="panel">
        <DataTable columns={tradeCols} rows={(trades ?? []).slice(0, 500)} rowKey={(r) => r.fill_id} />
      </div>
    </div>
  );
}
