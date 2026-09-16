# Trading Safety

## 平面隔离

- **Trading Plane 零 LLM 依赖**：策略运行时、风控、OMS、账本、对账不 import 任何 agent 代码。所有 LLM 服务宕机时交易平面完整运行。
- **Agent 只在 Research Plane**：产出 artifact 与 `DeploymentProposal`；激活部署需要平面外的人工/系统动作。

## 工具边界（硬性）

- 不存在工具：`send_live_order`、`change_risk_limit`、`transfer_funds`。
- ToolResult 只返回 artifact 引用与摘要指标，不返回巨型 dataframe。
- Agent 代码无法禁用风控（风控引擎不暴露可变入口给 agents 模块）。

## 事前风控（PreTradeRisk）

插件式规则，订单提交前全部通过才放行：
MaxOrderValue / MaxPositionWeight / MaxDailyTurnover / MaxActiveOrders /
DuplicateOrder / PriceDeviation / StaleMarketData / AvailableCash / AvailablePosition。

阈值全部来自 `configs/risk/default.yaml`，不硬编码。

## 运行时熔断

RuntimeRiskMonitor 监控：max daily loss、max drawdown、券商断连、行情断连、对账失败。
触发即置 **KillSwitch**：拒绝一切新订单，只允许撤单。KillSwitch 状态只能人工解除。

## 对账与 SAFE_MODE

ReconciliationService 比对券商侧（账户/持仓/订单/成交）与内部（ledger/positions/OMS）。
不匹配分类：UNKNOWN_POSITION / UNKNOWN_ORDER / CASH_MISMATCH / POSITION_MISMATCH / TRADE_MISMATCH。
关键不匹配 → TradingRuntime 进入 **SAFE_MODE**（等价 KillSwitch），PaperBroker 可注入故障以测试该路径。

## 失败策略：fail closed

关键风控/会计错误抛领域异常并中止流程，绝不吞错继续。宁可不交易，不可错交易。

## 当前范围限制

- 仅 PaperBroker，无真实券商适配器。
- 日频、long-only、现金账户、无融资融券。
- MarketRuleBook 覆盖已测试场景（涨跌停/ST/停牌/T+1/手数等），**不声称法规完备**；规则以带生效日期的配置维护，可更新而不改引擎。
