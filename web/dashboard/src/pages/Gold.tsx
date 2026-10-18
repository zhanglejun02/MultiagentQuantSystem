import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, CandlestickChart, Coins, Database, ExternalLink, LoaderCircle, RefreshCw,
  Save, SlidersHorizontal, Sparkles, TriangleAlert } from "lucide-react";
import type { EChartsOption } from "echarts";
import { api, GoldDashboard } from "../api";
import Chart, { areaGradient } from "../components/Chart";
import GoldResearch from "./GoldResearch";
import { allocationFromDraft, closePoints, commonSeries, createDraft, display, drawdowns,
  GOLD_ETFS, GOLD_INSTRUMENTS, GoldDraft, goldTimestamp as timestamp, inPeriod, number,
  OFFICIAL_SOURCES, Period, riskMetrics, shouldHydrateGoldDraft } from "./goldModel";
import "./gold.css";

const MODES = ["K线", "区间收益", "回撤"] as const;
const PERIODS: Period[] = ["1M", "3M", "1Y", "全部"];
const TABS = ["观察与研究", "我的配置", "信息来源"] as const;
const COLORS = ["#aa8535", "#587b68", "#89719a", "#44828a", "#ac6572"];
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);

function Empty({ text = "数据未发布" }: { text?: string }) {
  return <div className="gold-empty"><Database size={25} /><strong>{text}</strong>
    <a href={OFFICIAL_SOURCES[0].url} target="_blank" rel="noreferrer">上金所官方行情 <ExternalLink size={12} /></a></div>;
}

export default function Gold() {
  const [data, setData] = useState<GoldDashboard | null>(null);
  const [draft, setDraft] = useState(createDraft);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState<(typeof TABS)[number]>("观察与研究");
  const [symbol, setSymbol] = useState("Au99.99");
  const [mode, setMode] = useState<(typeof MODES)[number]>("K线");
  const [period, setPeriod] = useState<Period>("3M");
  const requestId = useRef(0);
  const revision = useRef(0);
  const dirtyRef = useRef(false);
  const busy = useRef(false);
  const pendingRefresh = useRef(false);
  const chartHost = useRef<HTMLDivElement>(null);

  async function request(record?: ReturnType<typeof allocationFromDraft>, background = false) {
    if (busy.current) { if (background) pendingRefresh.current = true; return; }
    if (!record && dirtyRef.current && !background) return;
    busy.current = true;
    const current = ++requestId.current;
    const version = revision.current;
    const dirtyAtStart = dirtyRef.current;
    if (record) setSaving(true);
    else if (background) setRefreshing(true);
    else setLoading(true);
    setError("");
    if (!background) setNotice("");
    try {
      const response = await (record ? api.saveGoldAllocation(record) : api.goldDashboard());
      if (current !== requestId.current) return;
      setData(response);
      if (shouldHydrateGoldDraft(version, revision.current, dirtyAtStart, Boolean(record))) {
        setDraft(createDraft(response.allocation.record));
        dirtyRef.current = false;
        setDirty(false);
      }
      if (record) setNotice("配置已保存");
    } catch (failure) {
      if (current === requestId.current) setError(`${record ? "保存" : "读取"}失败：${errorText(failure)}`);
    } finally {
      if (current === requestId.current) {
        busy.current = false;
        setLoading(false);
        setSaving(false);
        setRefreshing(false);
        if (pendingRefresh.current) { pendingRefresh.current = false; void request(undefined, true); }
      }
    }
  }

  useEffect(() => {
    void request();
    return () => { requestId.current += 1; busy.current = false; pendingRefresh.current = false; };
  }, []);

  useEffect(() => {
    if (!dirty) return;
    const prevent = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", prevent);
    return () => window.removeEventListener("beforeunload", prevent);
  }, [dirty]);

  useEffect(() => {
    if (!chartHost.current) return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    });
    observer.observe(chartHost.current);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [tab, loading]);

  function edit(next: GoldDraft) {
    revision.current += 1;
    dirtyRef.current = true;
    setDraft(next);
    setDirty(true);
    setNotice("");
  }

  function save() {
    if (!data || busy.current || !dirty) return;
    try { void request(allocationFromDraft(draft)); }
    catch (failure) { setError(errorText(failure)); }
  }

  function openResearch() {
    setTab("观察与研究");
    requestAnimationFrame(() => document.getElementById("gold-research-symbol")?.focus());
  }

  const quotes = data?.snapshot?.quotes ?? [];
  const quote = quotes.find((item) => item.symbol === symbol);
  const selectedName = quote?.name ?? GOLD_INSTRUMENTS.find((item) => item.symbol === symbol)?.name ?? symbol;
  const observations = closePoints(quote);
  const latest = observations.at(-1), previous = observations.at(-2);
  const dayDelta = latest && previous ? latest.value - previous.value : null;
  const dayReturn = latest && previous ? latest.value / previous.value - 1 : null;
  const deltaClass = dayDelta === null ? "" : dayDelta > 0 ? "gold-rise" : dayDelta < 0 ? "gold-fall" : "";
  const signed = (value: number | null, percent = false) => `${value !== null && value > 0 ? "+" : ""}${display(value, percent, percent || symbol === "Au99.99" ? 2 : 3)}`;
  const points = inPeriod(observations, period);
  const last = points.at(-1);
  const metrics = riskMetrics(points);
  const comparisonSymbols = ["Au99.99", ...draft.watchlist];
  const comparisonQuotes = comparisonSymbols.map((code) => quotes.find((item) => item.symbol === code));
  const comparison = commonSeries(comparisonQuotes, period);
  const missing = GOLD_INSTRUMENTS.filter((item) => !closePoints(quotes.find((itemQuote) => itemQuote.symbol === item.symbol)).length);
  const stale = quotes.filter((item) => item.stale);
  const missingComparison = comparisonSymbols.filter((_, index) => !closePoints(comparisonQuotes[index]).length);
  const candles = points.length > 0 && points.every((point) => {
    const open = number(point.open), high = number(point.high), low = number(point.low);
    return open !== null && high !== null && low !== null && low > 0
      && low <= Math.min(open, point.value) && high >= Math.max(open, point.value);
  });
  const comparing = mode === "区间收益";
  const dates = comparing ? comparison.dates : points.map((point) => point.date);
  const enough = comparing ? dates.length >= 2 : mode === "回撤" ? dates.length >= 2 : dates.length > 0;
  const option: EChartsOption = {
    animation: false,
    color: COLORS,
    textStyle: { color: "#77717b", fontFamily: '"Avenir Next Rounded", "Avenir Next", "PingFang SC", sans-serif' },
    grid: { left: 8, right: 18, top: comparing ? 62 : 24, bottom: 48, containLabel: true },
    tooltip: { trigger: "axis", confine: true, borderColor: "#bfd3c7", backgroundColor: "#ffffff",
      textStyle: { color: "#294237" }, axisPointer: { type: "cross" } },
    legend: { show: comparing, type: "scroll", top: 6, left: 0, right: 4, textStyle: { color: "#53675e", fontSize: 11 } },
    xAxis: { type: "category", data: dates, boundaryGap: mode === "K线" && candles,
      axisLine: { lineStyle: { color: "#cbd9d0" } }, axisLabel: { color: "#65786d", hideOverlap: true } },
    yAxis: { type: "value", scale: true, axisLabel: { color: "#65786d", formatter: comparing || mode === "回撤" ? "{value}%" : "{value}" },
      splitLine: { lineStyle: { color: "#e6ede8", type: "dashed" } } },
    dataZoom: [{ type: "inside", zoomOnMouseWheel: false }, { type: "slider", height: 15, bottom: 4,
      borderColor: "#dae5dd", fillerColor: "rgba(34,116,92,0.12)", brushSelect: false }],
    series: comparing ? comparison.values.map((values, index) => ({
      name: comparisonQuotes[index]?.name ?? comparisonSymbols[index], type: "line", data: values,
      smooth: false, showSymbol: false, lineStyle: { width: 2, type: index % 2 ? "dashed" : "solid" },
    })) : mode === "K线" && candles ? [{
      name: selectedName, type: "candlestick", data: points.map((point) => [Number(point.open), point.value, Number(point.low), Number(point.high)]),
      itemStyle: { color: "#c65552", color0: "#278460", borderColor: "#c65552", borderColor0: "#278460" },
    }, {
      name: "收盘价", type: "line", smooth: false, showSymbol: false, data: points.map((point) => point.value),
      lineStyle: { color: COLORS[0], width: 1.5 }, itemStyle: { color: COLORS[0] },
    }] : [{ name: mode === "回撤" ? "回撤 (%)" : "收盘价", type: "line", smooth: false, showSymbol: false,
      data: mode === "回撤" ? drawdowns(points).map((value) => value * 100) : points.map((point) => point.value),
      lineStyle: { color: mode === "回撤" ? COLORS[1] : COLORS[0], width: 2 },
      areaStyle: { color: areaGradient(mode === "回撤" ? COLORS[1] : COLORS[0], 0.12) } }],
  };
  const summary = data?.summary;
  const held = data?.allocation.record.holdings.filter((row) => (number(row.quantity) ?? 0) > 0) ?? [];
  const au = data?.futures.contracts.filter((contract) => contract.product.toUpperCase() === "AU") ?? [];
  const sources = [...(data?.sources ?? []), ...OFFICIAL_SOURCES.filter((source) =>
    !data?.sources.some((item) => item.url === source.url))];
  const disabled = loading || saving || !data;
  const quoteDate = latest?.date;

  return <div className="gold-page" aria-busy={loading || saving}>
    <header className="gold-header">
      <h1><Coins size={22} />黄金观察与配置</h1>
      <div className="gold-actions">
        <span className="gold-badge">EOD · 收盘数据</span>
        <button className="icon-button" aria-label="重新读取本地数据" title={dirty ? "有未保存配置，刷新已禁用" : "重新读取本地数据"}
          disabled={loading || saving || refreshing || dirty} onClick={() => void request()}><RefreshCw size={16} className={refreshing ? "gold-spin" : ""} /></button>
        <button onClick={openResearch}><Sparkles size={15} />分析</button>
        <button onClick={() => setTab("我的配置")}><SlidersHorizontal size={15} />我的配置</button>
        <button className="icon-button" onClick={save} disabled={disabled || refreshing || !dirty} aria-label="保存手工配置"
          title={saving ? "保存中" : refreshing ? "行情更新中，请稍后保存" : "保存手工配置"}><Save size={15} /></button>
      </div>
    </header>
    <div className="gold-meta"><span>快照抓取 {timestamp(data?.snapshot?.fetched_at)}</span>
      <span className={dirty ? "gold-warning-text" : ""}>{dirty ? "配置未保存" : `配置更新 ${timestamp(data?.allocation.updated_at)}`}</span>
      {refreshing && <span role="status">行情更新中</span>}</div>
    <section className="gold-account-strip" aria-label="已保存手工配置摘要">
      <div><span>黄金市值 <small>元</small></span><strong>{summary?.has_holdings ? display(summary.gold_value) : "未录入"}</strong></div>
      <div><span>持有盈亏 <small>元 · 未实现</small></span><strong className={(number(summary?.unrealized_pnl) ?? 0) > 0 ? "gold-rise" : (number(summary?.unrealized_pnl) ?? 0) < 0 ? "gold-fall" : ""}>
        {summary?.has_holdings ? display(summary.unrealized_pnl) : "未录入"}</strong></div>
      <div><span>目标占比 <small>手工配置</small></span><strong>{data?.allocation.artifact_id ? display(summary?.target_weight, true) : "未录入"}</strong></div>
    </section>
    {dirty && <p className="gold-meta gold-warning-text">有未保存配置 · 摘要仍按已保存记录估值</p>}
    {error && <div className="gold-error" role="alert"><TriangleAlert size={16} /><span>{error}</span></div>}
    {notice && <p className="gold-success" role="status">{notice}</p>}
    {loading && <p className="gold-loading" role="status"><LoaderCircle size={16} />正在读取本地数据</p>}
    {data && <>
      {missing.length > 0 && <div className="gold-warning"><Database size={15} /><span>缺失报价：{missing.map((item) => item.symbol).join("、")} · 数据未发布</span></div>}
      {stale.length > 0 && <div className="gold-warning"><TriangleAlert size={15} /><span>陈旧报价：{stale.map((item) => `${item.symbol}（${closePoints(item).at(-1)?.date ?? "无日期"}）`).join("、")}</span></div>}
      {!!data.snapshot?.warnings.length && <details className="gold-warnings"><summary>快照警告（{data.snapshot.warnings.length}）</summary>
        <ul>{data.snapshot.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></details>}
    </>}
    <nav className="gold-tabs" aria-label="黄金视图">{TABS.map((item) => <button key={item} aria-pressed={tab === item}
      className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}</nav>

    <div className="gold-workspace" hidden={tab !== "观察与研究"}>
      <div className="gold-market-workarea">
      <section className="gold-section gold-quote-section" aria-label="黄金行情">
        <div className="gold-instrument-tabs" role="tablist" aria-label="黄金标的">{GOLD_INSTRUMENTS.map((item) =>
          <button key={item.symbol} role="tab" id={`gold-instrument-${item.symbol}`} aria-controls="gold-quote-panel" aria-selected={symbol === item.symbol}
            tabIndex={symbol === item.symbol ? 0 : -1} onKeyDown={(event) => {
              const index = GOLD_INSTRUMENTS.findIndex((entry) => entry.symbol === item.symbol);
              const next = event.key === "ArrowRight" ? (index + 1) % GOLD_INSTRUMENTS.length
                : event.key === "ArrowLeft" ? (index + GOLD_INSTRUMENTS.length - 1) % GOLD_INSTRUMENTS.length
                  : event.key === "Home" ? 0 : event.key === "End" ? GOLD_INSTRUMENTS.length - 1 : null;
              if (next === null) return;
              event.preventDefault();
              setSymbol(GOLD_INSTRUMENTS[next].symbol);
              document.getElementById(`gold-instrument-${GOLD_INSTRUMENTS[next].symbol}`)?.focus();
            }}
            onClick={() => setSymbol(item.symbol)}><span>{item.symbol === "Au99.99" ? "现货" : item.name}</span><small>{item.symbol}</small></button>)}</div>
        <div className="gold-quote-overview" id="gold-quote-panel" role="tabpanel" aria-labelledby={`gold-instrument-${symbol}`}>
          <div><div className="gold-quote-label">{selectedName} <span>收盘价</span></div>
            <div className="gold-price"><strong>{display(latest?.value, false, symbol === "Au99.99" ? 2 : 3)}</strong><span>{symbol === "Au99.99" ? "元/克" : "元/份"}</span></div>
            <div className={`gold-day-delta ${deltaClass}`}><strong>{signed(dayDelta)}</strong><strong>{signed(dayReturn, true)}</strong><span>较前收盘</span></div>
          </div><div className="gold-quote-provenance"><span className="gold-badge">行情日期 {quoteDate ?? "未发布"}</span>
            {quote && <><a href={quote.source_url} target="_blank" rel="noreferrer">{quote.source} <ExternalLink size={11} /></a><small>抓取 {timestamp(quote.fetched_at)}</small></>}
            {quote?.stale && <span className="gold-warning-text">陈旧报价</span>}</div>
        </div>
        <div className="gold-controls">
          <div className="gold-segment" role="group" aria-label="图表模式">{MODES.map((item) => <button key={item}
            aria-pressed={mode === item} onClick={() => setMode(item)}>{item === "K线" && <CandlestickChart size={13} />}{item}</button>)}</div>
          <div className="gold-segment" role="group" aria-label="观察区间">{PERIODS.map((item) => <button key={item}
            aria-pressed={period === item} onClick={() => setPeriod(item)}>{item}</button>)}</div>
        </div>
        <div className="gold-meta gold-chart-meta">
          <span>{comparing ? "收盘价收益 · 不含分红 / 未复权" : mode === "回撤" ? "区间内收盘价回撤" : candles ? "日 K线 · 金线为收盘价" : "收盘价（OHLC 不完整）"}
            {!comparing && quote && ` · ${quote.unit === "CNY/g" ? "元/克" : "元/份"}`}</span>
          <span>{comparing ? `共同区间 ${dates[0] ?? "—"} 至 ${dates.at(-1) ?? "—"} · ${dates.length} 个共同观测`
            : `行情日期 ${quoteDate ?? "未发布"}`}</span>
        </div>
        <div className="gold-chart" ref={chartHost}>
          {enough ? <Chart option={option} height={320} /> : <Empty text={comparing && missingComparison.length
            ? `数据未发布：${missingComparison.join("、")}` : dates.length ? "观测不足" : comparing && !missingComparison.length ? "无共同交易日期" : "数据未发布"} />}
        </div>
        <div className="gold-meta"><span>{points.length} 个收盘观测 · 未复权</span><span>前收盘观测 {previous?.date ?? "未发布"}</span></div>
        {comparing && <ul className="gold-comparison-sources">{comparisonQuotes.map((item, index) => <li key={comparisonSymbols[index]}>
          {comparisonSymbols[index]} · {item ? <><a href={item.source_url} target="_blank" rel="noreferrer">{item.source} <ExternalLink size={10} /></a>
            {` · 行情 ${closePoints(item).at(-1)?.date ?? "未发布"} · 抓取 ${timestamp(item.fetched_at)}`}</> : "数据未发布"}</li>)}</ul>}
      </section>
      <section className="gold-section" aria-labelledby="gold-risk-title">
        <h2 id="gold-risk-title">{selectedName} · 区间风险</h2>
        <div className="gold-risk">{[["收盘价收益", display(metrics.range, true)], ["年化波动 · 252日 / 样本", display(metrics.volatility, true)],
          ["最大回撤", display(metrics.drawdown, true)], ["末次观测", last?.date ?? "未发布"]].map(([label, value]) =>
          <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
        <p className="gold-meta">{points[0]?.date ?? "—"} 至 {last?.date ?? "—"} · {points.length} 个收盘观测 · 未复权</p>
      </section>
      </div>
      <GoldResearch dashboard={data} dirty={dirty} allocationBusy={loading || saving} onRefresh={() => void request(undefined, true)} onConfigure={() => setTab("我的配置")} />
      <div className="gold-market-details">
      <section className="gold-section" aria-labelledby="gold-etf-title">
        <h2 id="gold-etf-title">黄金ETF <small>收盘价 · 元/份</small></h2>
        <div className="gold-table-wrap"><table className="gold-table"><thead><tr><th>关注</th><th>标的</th><th>行情日期</th><th>收盘</th><th>日涨跌</th><th>成交额（元）</th><th>来源 / 抓取</th><th aria-label="查看行情" /></tr></thead>
          <tbody>{GOLD_ETFS.map((item) => {
            const current = quotes.find((entry) => entry.symbol === item.symbol);
            const observations = closePoints(current), latest = observations.at(-1), previous = observations.at(-2);
            const change = latest && previous ? latest.value / previous.value - 1 : null;
            return <tr key={item.symbol} className={symbol === item.symbol ? "selected" : ""}>
              <td><input type="checkbox" aria-label={`关注${item.name}`} checked={draft.watchlist.includes(item.symbol)} disabled={disabled}
                onChange={(event) => edit({ ...draft, watchlist: event.target.checked ? [...draft.watchlist, item.symbol] : draft.watchlist.filter((code) => code !== item.symbol) })} /></td>
              <td><strong>{item.name}</strong><small>{item.symbol}{current?.stale ? " · 陈旧" : ""}</small></td>
              <td>{latest?.date ?? "数据未发布"}</td><td>{display(latest?.value, false, 3)}</td>
              <td className={change === null ? "" : change > 0 ? "gold-rise" : change < 0 ? "gold-fall" : ""}>{display(change, true)}</td>
              <td>{display(latest?.amount)}</td><td>{current ? <><a href={current.source_url} target="_blank" rel="noreferrer">{current.source} <ExternalLink size={10} /></a><small>{timestamp(current.fetched_at)}</small></> : "未发布"}</td>
              <td><button className="icon-button" title={`查看${item.name}行情`} aria-label={`查看${item.name}行情`} onClick={() => { setSymbol(item.symbol); setMode("K线"); }}><ArrowUpRight size={15} /></button></td>
            </tr>;
          })}</tbody></table></div>
      </section>
      <div className="gold-bottom">
        <section className="gold-section"><h2>AU 期货主力 <small>既有日线流水线</small></h2>
          {!au.length ? <p className="gold-muted">未接入</p> : <><p className="gold-meta">行情日期 {data?.futures.trade_date ?? "未记录"} · 元/克</p>
            <div className="gold-table-wrap"><table className="gold-table"><thead><tr><th>合约</th><th>收盘</th><th>结算</th><th>涨跌</th><th>持仓量</th></tr></thead><tbody>{au.map((contract) => <tr key={contract.symbol}><td>{contract.symbol}</td><td>{display(contract.close)}</td><td>{display(contract.settlement)}</td><td>{display(contract.return, true)}</td><td>{display(contract.open_interest, false, 0)}</td></tr>)}</tbody></table></div></>}
        </section>
        <section className="gold-section"><h2>官方与宏观来源</h2><ul className="gold-sources">{OFFICIAL_SOURCES.slice(3).map((item) =>
          <li key={item.url}><a href={item.url} target="_blank" rel="noreferrer">{item.name} <ExternalLink size={12} /></a><small>{item.description}</small></li>)}</ul></section>
      </div>
      </div>
    </div>

    {tab === "我的配置" && <>
      <section className="gold-section"><div className="gold-heading"><h2>手工资产配置</h2><span className="gold-badge">独立记录 · 非券商账户</span></div>
        <div className="gold-summary">{[["黄金市值（元）", summary?.has_holdings ? display(summary.gold_value) : "未录入"],
          ["总资产（元）", display(summary?.total_assets)], ["黄金占比", display(summary?.weight, true)],
          ["目标比例", display(summary?.target_weight, true)], ["偏离目标（百分点）", display(number(summary?.deviation) === null ? null : Number(summary?.deviation) * 100)],
          ["未实现盈亏（元）", summary?.has_holdings ? display(summary.unrealized_pnl) : "未录入"]].map(([label, value]) =>
          <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
        <p className="gold-meta">估值对应已保存配置 · 配置更新 {timestamp(data?.allocation.updated_at)}</p>
        {!summary?.has_holdings && <p className="gold-muted">持仓未录入</p>}
        {!!summary?.unpriced_symbols.length && <p className="gold-warning">无法估值：{summary.unpriced_symbols.join("、")} · 缺少报价</p>}
        {held.map((holding) => {
          const valuation = quotes.find((item) => item.symbol === holding.symbol);
          const valuationDate = closePoints(valuation).at(-1)?.date;
          return <p className={`gold-meta ${!valuationDate || valuation?.stale ? "gold-warning-text" : ""}`} key={holding.symbol}>
            {holding.symbol} · 估值日期 {valuationDate ?? "无报价，无法估值"}{valuation?.stale ? " · 陈旧报价估值" : ""}</p>;
        })}
      </section>
      <form className="gold-section gold-form" noValidate onSubmit={(event) => { event.preventDefault(); save(); }}>
        <fieldset disabled={disabled}><legend>配置记录</legend>
          <div className="gold-fields"><label>其他资产（元，不含黄金）<input type="number" min="0" step="any" value={draft.other_assets}
            onChange={(event) => edit({ ...draft, other_assets: event.target.value })} /></label>
            <label>目标黄金比例（%）<input type="number" min="0" max="100" step="0.1" value={draft.target_percent}
              onChange={(event) => edit({ ...draft, target_percent: event.target.value })} /></label></div>
          <div className="gold-holdings-head"><span>黄金ETF</span><span>数量（份）</span><span>平均成本（元/份）</span></div>
          {draft.holdings.map((holding, index) => <div className="gold-holding" key={holding.symbol}>
            <div><strong>{GOLD_ETFS[index].name}</strong><small>{holding.symbol}</small></div>
            {(["quantity", "average_cost"] as const).map((key) => <input type="number" min="0" step="any" key={key}
              aria-label={`${GOLD_ETFS[index].name}${key === "quantity" ? "数量（份）" : "平均成本（元/份）"}`} value={holding[key]}
              onChange={(event) => edit({ ...draft, holdings: draft.holdings.map((row, rowIndex) => rowIndex === index ? { ...row, [key]: event.target.value } : row) })} />)}
          </div>)}
          <label className="gold-note">备注<textarea rows={3} value={draft.note} onChange={(event) => edit({ ...draft, note: event.target.value })} /></label>
          <div className="gold-form-footer"><span>{dirty ? "配置未保存" : "无未保存更改"}{refreshing ? " · 行情更新中" : ""}</span>
            <button type="submit" disabled={!dirty || refreshing}><Save size={15} />{saving ? "保存中" : "保存"}</button></div>
        </fieldset>
      </form>
    </>}

    {tab === "信息来源" && <section className="gold-section"><h2>信息来源</h2><ul className="gold-sources gold-source-grid">{sources.map((source, index) =>
      <li key={`${source.url}-${index}`}><a href={source.url} target="_blank" rel="noreferrer">{source.name} <ExternalLink size={13} /></a><small>{source.description}</small></li>)}</ul>
      <dl className="gold-provenance"><dt>快照抓取时间</dt><dd>{timestamp(data?.snapshot?.fetched_at)}</dd><dt>快照标识</dt><dd>{data?.snapshot?.artifact_id ?? "未发布"}</dd>
        <dt>配置标识</dt><dd>{data?.allocation.artifact_id ?? "未录入"}</dd></dl></section>}
  </div>;
}