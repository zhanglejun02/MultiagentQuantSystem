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
运行时状态为 `NORMAL(RUNNING) / REDUCE_ONLY / CANCEL_ONLY / SAFE_MODE / HALTED / KILLED`。
`REDUCE_ONLY` 与 `SAFE_MODE` 只允许降低风险，`CANCEL_ONLY` 仅允许撤单，`HALTED`
彻底冻结，硬 KillSwitch 拒绝提交但仍允许撤单。KillSwitch 状态只能人工解除。

安全判断在 OMS 和 Execution Gateway 两处执行：前者阻止创建非法新订单，后者在真正
产生 broker side effect 前再次检查。策略激活也读取同一个 `TradingRuntime`。

## 对账与 SAFE_MODE

ReconciliationService 比对券商侧（账户/持仓/订单/成交）与内部（ledger/positions/OMS）。
不匹配分类：UNKNOWN_POSITION / UNKNOWN_ORDER / CASH_MISMATCH / POSITION_MISMATCH / TRADE_MISMATCH。
关键不匹配 → TradingRuntime 进入 **SAFE_MODE**；对账只是安全状态的一个输入源，
不是 KillSwitch 的下游终点。PaperBroker 可注入故障以测试该路径。

## 全局组合风险

`PortfolioRiskControlPlane` 接收股票与期货的标准化 `RiskContribution`，按账户去重 NAV，
按策略和资产类别聚合 gross/net exposure 与 margin。`CapitalAllocator` 执行策略预算，
组合级 gross leverage、net exposure 和 margin ratio 在资产域风控之后、订单提交之前检查。

## 失败策略：fail closed

关键风控/会计错误抛领域异常并中止流程，绝不吞错继续。宁可不交易，不可错交易。

## 当前范围限制

- 仅本地 PaperBroker，无真实券商、vn.py 或 CTP 适配器。
- A 股为日频、long-only、现金账户、无融资融券。
- 期货为日线 fixture 仿真，支持多空、开平、今昨仓、保证金和逐日盯市；不支持真实行情、自动换月、分钟/Tick 撮合或实盘下单。
- `/api/futures` 创建接口只接受 `mode=PAPER`。未来 CTP 凭据只能通过未提交的环境变量注入，不能进入配置文件、Artifact 或事件 payload。
- MarketRuleBook 覆盖已测试场景（涨跌停/ST/停牌/T+1/手数等），**不声称法规完备**；规则以带生效日期的配置维护，可更新而不改引擎。
