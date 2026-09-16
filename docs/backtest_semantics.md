# Backtest Semantics

## 执行语义（默认，日频）

```
Day T close  → 行情定版 → 特征 → 信号 → 目标组合（decision_ts = T close 之后）
Day T+1      → 公司行为(09:00) → T+1 结算释放 → 开盘事件 → 交易所模拟
			 → 成交/撤单 → 收盘估值 → 账本/PnL → 下一轮信号
```

**禁止同 bar 成交**：信号在 T 收盘后产生，最早在 T+1 开盘成交（`NextOpenFillModel`）。引擎不提供"按信号 bar 收盘价成交"的路径。测试 `test_no_same_bar_fill` 守护此语义。

## PIT 正确性

- 每条数据记录携带 `event_time / effective_time / available_at / ingested_at`。
- 任何历史查询必须带 `as_of`；`available_at > as_of` 的记录不可见（PITDataStore 强制）。
- Universe 同样 PIT：上市/退市/ST/停牌状态按区间生效，消除幸存者偏差。
- 执行用**未复权价**；复权因子仅供特征计算，且因子本身 PIT 化。
- 公司行为必须在 T+1 可用时间过滤后，于结算释放和撮合前入账。拆股同步调整 total / available / frozen / pending 与成本基准；现金分红同步增加现金并调整成本基准。

## 事件流

```
HistoricalClock → MarketDataFeed → StrategyRuntime → PortfolioTarget
→ PortfolioRebalancer → OrderIntent → PreTradeRisk
→ PaperBroker/ExchangeSimulator → Fill/PartialFill/Reject
→ AccountLedger → Position → PnL
```

13 种类型化事件（MarketEvent … AccountEvent）全部写入 EventStore（JSONL），带单调 `seq`、schema version 和 SHA-256 hash chain。BacktestResultArtifact 保存首尾事件哈希。

## 保守成交规则

- 停牌不成交。
- 一字涨停不可买入成交；一字跌停不可卖出成交。
- 超过 `participation_limit` × 决策时已知历史 ADV 的部分只能部分成交或不成交。T+1 实际全天成交量只允许事后容量诊断，绝不影响开盘成交。
- 成交受可用资金与可用持仓约束（T+1：当日买入不可卖）。

## 账本

- 资金/会计全部 `Decimal`，无浮点累加。
- 跟踪 cash / frozen_cash / total_position / available_position / market_value / realized_pnl / unrealized_pnl / fees / taxes / NAV。
- 会计不变量在测试中断言：NAV = cash + frozen_cash + market_value；每笔余额变化可由 ledger entries 解释。

## 确定性与回放

- 固定 `random_seed`，无挂钟依赖，事件顺序确定。
- `LedgerReplayService` 先验证 sequence continuity、schema 和 hash chain，再仅凭事件日志重建账本，逐项比较 cash、frozen_cash、total/available/frozen/pending position、realized PnL、fees 与 NAV。
- 当前不是完整 `SystemReplay`：事件尚不足以重建每个日内 OMS 冻结与状态迁移；不得用 LedgerReplay 的结果声称系统级回放完整。
- 删除、重排、修改事件均破坏 hash chain；即使攻击者重算整条链，也会与 BacktestResultArtifact 保存的 final hash 不符。
- 同样输入（snapshot、commit、config、seed）→ 相同输出，由确定性测试守护。

## Prediction Target

SignalEvent 明确记录 decision_time、execution_time_assumption、target_spec 与 prediction_horizon。IC 指标名包含目标与 horizon，例如 `rank_ic_next_open_to_close_h1_mean`；不得将 close-to-close IC 当作 next-open 执行收益质量。

## 双引擎定位

- **EventDrivenBacktestEngine**（已实现）：权威引擎，候选策略晋升必经。
- **VectorizedResearchEngine**（未实现，接口预留）：仅供快速探索/因子粗筛，其结果不可用于晋升。
