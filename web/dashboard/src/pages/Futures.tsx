import { FormEvent, useEffect, useState } from "react";
import { FastForward, Play, Plus, RefreshCw, Send, X } from "lucide-react";
import {
  FuturesBacktestArtifact,
  FuturesOrder,
  FuturesOrderCreate,
  FuturesPosition,
  FuturesSettlement,
  FuturesTrade,
  api,
  fmt,
} from "../api";
import DataTable, { Column } from "../components/DataTable";
import StatCard from "../components/StatCard";
import { useAsync } from "../hooks";

export default function Futures() {
  const [refresh, setRefresh] = useState(0);
  const [simulationId, setSimulationId] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [newName, setNewName] = useState("商品期货日线仿真");
  const [initialBalance, setInitialBalance] = useState("100000");
  const [symbol, setSymbol] = useState("rb2701.SHFE");
  const [direction, setDirection] = useState<"LONG" | "SHORT">("LONG");
  const [offset, setOffset] = useState<FuturesOrderCreate["offset"]>("OPEN");
  const [orderType, setOrderType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [quantity, setQuantity] = useState(1);
  const [limitPrice, setLimitPrice] = useState("");

  const { data: contracts, error: contractsError } = useAsync(() => api.futuresContracts(), []);
  const { data: simulations } = useAsync(() => api.futuresSimulations(), [refresh]);
  const { data: backtests } = useAsync(() => api.futuresBacktests(), [refresh]);

  useEffect(() => {
    if (!simulationId && simulations?.[0]) setSimulationId(simulations[0].simulation_id);
  }, [simulationId, simulations]);

  const { data: snapshot, error: snapshotError } = useAsync(
    () => simulationId ? api.futuresSnapshot(simulationId) : Promise.resolve(null),
    [simulationId, refresh],
  );
  const { data: orders } = useAsync(
    () => simulationId ? api.futuresOrders(simulationId) : Promise.resolve([]),
    [simulationId, refresh],
  );
  const { data: trades } = useAsync(
    () => simulationId ? api.futuresTrades(simulationId) : Promise.resolve([]),
    [simulationId, refresh],
  );
  const { data: settlements } = useAsync(
    () => simulationId ? api.futuresSettlements(simulationId) : Promise.resolve([]),
    [simulationId, refresh],
  );

  function reload() {
    setRefresh((value) => value + 1);
  }

  async function run(action: () => Promise<unknown>, success: string) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await action();
      setMessage(success);
      reload();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function createSimulation() {
    setBusy(true);
    setError("");
    try {
      const created = await api.createFuturesSimulation(newName, initialBalance);
      setSimulationId(created.simulation_id);
      setMessage("仿真账户已创建");
      reload();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function submitOrder(event: FormEvent) {
    event.preventDefault();
    if (!simulationId) return;
    const body: FuturesOrderCreate = {
      client_order_id: crypto.randomUUID(),
      symbol,
      direction,
      offset,
      quantity,
      order_type: orderType,
      ...(orderType === "LIMIT" ? { limit_price: limitPrice } : {}),
    };
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const order = await api.submitFuturesOrder(simulationId, body);
      setMessage(order.status === "REJECTED" ? `委托被拒绝：${order.reject_reason}` : "委托已进入仿真队列");
      reload();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  const positionColumns: Column<FuturesPosition>[] = [
    { key: "symbol", title: "合约", render: (row) => <span className="mono">{row.symbol}</span> },
    { key: "long_today", title: "多今", align: "right" },
    { key: "long_yesterday", title: "多昨", align: "right" },
    { key: "short_today", title: "空今", align: "right" },
    { key: "short_yesterday", title: "空昨", align: "right" },
    { key: "long_settlement_price", title: "多头结算基准", align: "right", render: (row) => fmt.num(row.long_settlement_price) },
    { key: "short_settlement_price", title: "空头结算基准", align: "right", render: (row) => fmt.num(row.short_settlement_price) },
  ];
  const orderColumns: Column<FuturesOrder>[] = [
    { key: "created_at", title: "时间", render: (row) => row.created_at.slice(0, 19).replace("T", " ") },
    { key: "symbol", title: "合约", render: (row) => <span className="mono">{row.symbol}</span> },
    { key: "direction", title: "持仓方向" },
    { key: "offset", title: "开平" },
    { key: "quantity", title: "手数", align: "right" },
    { key: "filled_quantity", title: "已成交", align: "right" },
    { key: "average_fill_price", title: "成交均价", align: "right", render: (row) => row.average_fill_price ? fmt.num(row.average_fill_price) : "-" },
    { key: "status", title: "状态", render: (row) => <span className={`badge ${row.status}`}>{row.status}</span> },
    { key: "reject_reason", title: "原因", render: (row) => <span className="muted">{row.reject_reason || "-"}</span> },
    {
      key: "action",
      title: "",
      render: (row) => row.status === "ACCEPTED" || row.status === "PARTIALLY_FILLED" ? (
        <button
          className="icon-button"
          title="撤销委托"
          aria-label="撤销委托"
          disabled={busy}
          onClick={() => run(() => api.cancelFuturesOrder(simulationId, row.order_id), "委托已撤销")}
        ><X size={14} /></button>
      ) : null,
    },
  ];
  const tradeColumns: Column<FuturesTrade>[] = [
    { key: "trading_day", title: "交易日" },
    { key: "symbol", title: "合约", render: (row) => <span className="mono">{row.symbol}</span> },
    { key: "direction", title: "持仓方向" },
    { key: "offset", title: "开平" },
    { key: "quantity", title: "手数", align: "right" },
    { key: "price", title: "成交价", align: "right", render: (row) => fmt.num(row.price) },
    { key: "fee", title: "手续费", align: "right", render: (row) => fmt.num(row.fee) },
    { key: "trade_id", title: "成交编号", render: (row) => <span className="mono muted">{row.trade_id.slice(-8)}</span> },
  ];
  const settlementColumns: Column<FuturesSettlement>[] = [
    { key: "trading_day", title: "交易日" },
    { key: "equity", title: "权益", align: "right", render: (row) => fmt.num(row.equity) },
    { key: "available", title: "可用资金", align: "right", render: (row) => fmt.num(row.available) },
    { key: "margin", title: "占用保证金", align: "right", render: (row) => fmt.num(row.margin) },
    { key: "risk_ratio", title: "风险度", align: "right", render: (row) => fmt.pct(row.risk_ratio) },
  ];
  const backtestColumns: Column<FuturesBacktestArtifact>[] = [
    { key: "created_at", title: "运行时间", render: (row) => row.created_at.slice(0, 19).replace("T", " ") },
    { key: "run_id", title: "运行", render: (row) => <span className="mono">{row.metadata.run_id}</span> },
    { key: "final_equity", title: "期末权益", align: "right", render: (row) => fmt.num(row.metadata.metrics.final_equity) },
    { key: "total_return", title: "总收益", align: "right", render: (row) => fmt.pct(row.metadata.metrics.total_return) },
    { key: "max_drawdown", title: "最大回撤", align: "right", render: (row) => fmt.pct(row.metadata.metrics.max_drawdown) },
    { key: "trade_count", title: "成交", align: "right", render: (row) => row.metadata.metrics.trade_count },
    { key: "final_event_hash", title: "事件根", render: (row) => <span className="mono muted" title={row.metadata.final_event_hash}>{row.metadata.final_event_hash.slice(0, 12)}</span> },
  ];

  return (
    <div className="futures-page">
      <header className="futures-header">
        <div>
          <div className="eyebrow">FUTURES DESK</div>
          <h1>期货仿真</h1>
          <p>{snapshot?.current_trading_day ?? "尚未推进"} · 下一交易日 {snapshot?.next_trading_day ?? "-"}</p>
        </div>
        <div className="futures-header-actions">
          <span className="paper-flag">PAPER · LOCAL</span>
          <button title="刷新" aria-label="刷新" onClick={reload}><RefreshCw size={15} /></button>
          <button
            className="primary"
            disabled={busy || !simulationId || !snapshot?.next_trading_day}
            onClick={() => run(() => api.advanceFuturesSimulation(simulationId), "交易日已推进并完成结算")}
          ><FastForward size={15} /> 推进交易日</button>
        </div>
      </header>

      {(error || contractsError || snapshotError) && <div className="error-box">{error || contractsError || snapshotError}</div>}
      {message && <div className="futures-message">{message}</div>}

      <div className="futures-runbar">
        <select value={simulationId} onChange={(event) => setSimulationId(event.target.value)}>
          <option value="">选择仿真账户</option>
          {(simulations ?? []).map((simulation) => (
            <option key={simulation.simulation_id} value={simulation.simulation_id}>
              {simulation.name} · {simulation.status}
            </option>
          ))}
        </select>
        <input value={newName} onChange={(event) => setNewName(event.target.value)} aria-label="新账户名称" />
        <input type="number" min="10000" step="10000" value={initialBalance} onChange={(event) => setInitialBalance(event.target.value)} aria-label="初始资金" />
        <button disabled={busy} onClick={createSimulation}><Plus size={15} /> 新建仿真</button>
      </div>

      <div className="grid cols-4 metric-grid">
        <StatCard label="账户权益" value={snapshot ? `¥ ${fmt.num(snapshot.account.equity)}` : "-"} />
        <StatCard label="可用资金" value={snapshot ? `¥ ${fmt.num(snapshot.account.available)}` : "-"} />
        <StatCard label="占用保证金" value={snapshot ? `¥ ${fmt.num(snapshot.account.margin)}` : "-"} />
        <StatCard label="风险度" value={snapshot ? fmt.pct(snapshot.account.risk_ratio) : "-"} tone={snapshot && Number(snapshot.account.risk_ratio) >= 0.7 ? "warn" : "ok"} />
      </div>

      <div className="futures-workspace">
        <form className="futures-ticket" onSubmit={submitOrder}>
          <div className="section-heading"><div><h2>委托票</h2><p>{snapshot?.status ?? "NO ACCOUNT"}</p></div></div>
          <label>合约
            <select value={symbol} onChange={(event) => setSymbol(event.target.value)}>
              {(contracts ?? []).map((contract) => <option key={contract.symbol} value={contract.symbol}>{contract.symbol} · {contract.name}</option>)}
            </select>
          </label>
          <label>持仓方向
            <span className="segmented">
              <button type="button" className={direction === "LONG" ? "active" : ""} onClick={() => setDirection("LONG")}>多头</button>
              <button type="button" className={direction === "SHORT" ? "active" : ""} onClick={() => setDirection("SHORT")}>空头</button>
            </span>
          </label>
          <div className="ticket-row">
            <label>开平
              <select value={offset} onChange={(event) => setOffset(event.target.value as FuturesOrderCreate["offset"])}>
                <option value="OPEN">开仓</option><option value="CLOSE">平仓</option>
                <option value="CLOSE_TODAY">平今</option><option value="CLOSE_YESTERDAY">平昨</option>
              </select>
            </label>
            <label>类型
              <select value={orderType} onChange={(event) => setOrderType(event.target.value as "MARKET" | "LIMIT")}>
                <option value="MARKET">市价</option><option value="LIMIT">限价</option>
              </select>
            </label>
          </div>
          <div className="ticket-row">
            <label>手数<input type="number" min="1" max="20" value={quantity} onChange={(event) => setQuantity(Number(event.target.value))} /></label>
            <label>限价<input type="number" disabled={orderType !== "LIMIT"} value={limitPrice} onChange={(event) => setLimitPrice(event.target.value)} required={orderType === "LIMIT"} /></label>
          </div>
          <div className="ticket-mark">参考价 <strong>{snapshot ? fmt.num(snapshot.marks[symbol]) : "-"}</strong></div>
          <button className="primary ticket-submit" disabled={busy || !simulationId} type="submit"><Send size={15} /> 提交仿真委托</button>
        </form>

        <section className="futures-contracts">
          <div className="section-heading"><div><h2>合约规则</h2><p>Fixture 参数 · 接入前由期货公司参数源覆盖</p></div></div>
          <div className="contract-list">
            {(contracts ?? []).map((contract) => (
              <div key={contract.symbol} className={contract.symbol === symbol ? "selected" : ""} onClick={() => setSymbol(contract.symbol)}>
                <span><strong>{contract.symbol}</strong><small>{contract.name} · {contract.exchange}</small></span>
                <span><b>{fmt.num(snapshot?.marks[contract.symbol] ?? "-")}</b><small>{contract.multiplier} 吨/手 · 保证金 {fmt.pct(contract.long_margin_rate)}</small></span>
              </div>
            ))}
          </div>
        </section>
      </div>

      <div className="section-title">
        <div><h2>趋势样例回测</h2><p>D 日收盘决策 · D+1 开盘成交 · 每日结算</p></div>
        <button
          disabled={busy}
          onClick={() => run(() => api.runFuturesBacktest(symbol), `${symbol} 回测已完成`)}
        ><Play size={14} /> 运行当前合约</button>
      </div>
      <div className="panel">
        <DataTable columns={backtestColumns} rows={backtests ?? []} rowKey={(row) => row.artifact_id} />
      </div>

      <h2>多空持仓</h2>
      <div className="panel"><DataTable columns={positionColumns} rows={snapshot?.positions ?? []} rowKey={(row) => row.symbol} /></div>
      <h2>委托</h2>
      <div className="panel"><DataTable columns={orderColumns} rows={[...(orders ?? [])].reverse()} rowKey={(row) => row.order_id} /></div>
      <div className="grid cols-2 futures-history">
        <section><h2>成交</h2><div className="panel"><DataTable columns={tradeColumns} rows={[...(trades ?? [])].reverse()} rowKey={(row) => row.trade_id} /></div></section>
        <section><h2>每日结算</h2><div className="panel"><DataTable columns={settlementColumns} rows={[...(settlements ?? [])].reverse()} rowKey={(row) => row.trading_day} /></div></section>
      </div>
    </div>
  );
}