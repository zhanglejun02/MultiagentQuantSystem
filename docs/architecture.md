# Architecture

## 总体形态：Modular Monolith + Ports/Adapters

单进程单仓库，但保持严格的模块边界（见 ADR-0001）。领域层（`domain/`）不 import FastAPI、SQLAlchemy ORM、UI。所有外部依赖（数据源、券商、存储、任务系统）通过 `Protocol` 定义端口，注入适配器。

```text
Research -> Validation -> Strategy -> Deployment -> Execution
                                |             |             |             |
                                +-------------+-------------+-------------+
                                                            Global Risk Plane
```

- **Research Plane**：Agent 提案、Factor DSL、FactorLab、Experiment/Artifact Registry。
- **Validation Plane**：PIT、OOS、walk-forward、purged CV/embargo、多重检验、去冗余、
    中性化、换手、成本、容量、regime/stress 与 final holdout 均由确定性作业产出
    `VALIDATION_REPORT`。
- **Strategy Plane**：`Factor -> SignalSpec -> PortfolioConstructionSpec -> StrategyPackage`。
    因子只有到 `STRATEGY_ELIGIBLE` 才能被策略引用。
- **Deployment Plane**：`DRAFT -> BACKTESTED -> VALIDATED -> APPROVED -> PAPER -> SHADOW
    -> CANARY -> LIVE`。人工审批不可省略，运行时非 `RUNNING` 时不能激活。
- **Execution Plane**：共享策略逻辑，仅替换 clock、market data、broker 和 execution model。
    OMS 与 Execution Gateway 均执行安全门禁。
- **Global Risk Plane**：聚合股票与期货贡献，统一检查 NAV、gross/net exposure、margin、
    leverage 与 strategy allocation；各资产域仍保留自己的规则和账本。

股票与期货仍是独立 bounded context，不共享订单、持仓、账本或回放模型；但共享
`TradingRuntime`、KillSwitch、CapitalAllocator 和 PortfolioRiskControlPlane。

## 模块边界

| 模块 | 职责 | 依赖 |
|---|---|---|
| `domain` | 类型、枚举、领域模型、异常、JobRunner 协议 | 仅 pydantic |
| `artifacts` | 不可变 ArtifactEnvelope、Store、Registry、Lineage | domain, infra(持久化) |
| `data` | PITDataStore、Provider 协议、LocalParquetProvider、PIT Universe | domain |
| `rules` | MarketRuleBook 协议 + AShareRuleBook（configs/markets/cn_a.yaml 驱动） | domain |
| `backtest` | 事件驱动引擎、时钟、Feed、Exchange、Fill/Slippage/Fee、Ledger、EventStore、Replay | domain, rules, data, risk, execution |
| `risk` | 资产域 PreTradeRisk、PortfolioRiskControlPlane、CapitalAllocator、TradingRuntime、KillSwitch | domain |
| `execution` | OMS 状态机、BrokerGateway 协议、PaperBroker、Reconciliation | domain, risk |
| `futures` | 期货合约、双向持仓、保证金账本、Paper 仿真、回测、事件回放和 Gateway 端口 | artifacts, infra, risk |
| `research` | Experiment/Factor/Strategy 状态机、Scorecard、确定性 Promotion Policy | domain, artifacts |
| `agents` | ToolCall/ToolResult 协议、研究工具集 | research, artifacts, backtest |
| `api` | FastAPI 只读查询 + 少量控制端点 | 以上全部（组合根） |

## 关键数据流（每日）

```
Day T 18:30:  Tushare EOD → staging → quality gates → immutable DataSnapshot
              → deterministic MarketSnapshot → read-only API/Dashboard
Day T close:  verified snapshot → features → signal → PortfolioTarget
Day T+1:      generate orders → global + asset pretrade risk → OMS → gated gateway
              → PaperBroker/ExchangeSim
              → fills → ledger → PnL → reconciliation
```

质量失败只生成 `DATA_QUALITY_REPORT(FAILED)`，不会生成可供研究或交易使用的
`DATA_SNAPSHOT`。发布内容按哈希寻址，同一数据重复调度可幂等复用；源 staging 后续变化不会
影响已登记快照。市场摘要由结构化数据确定性计算，新闻或 LLM 不能覆盖行情事实，也不进入
交易路径。

严禁"用 T 收盘价生成信号又按 T 收盘价成交"。引擎断言 decision timestamp 在成交 bar 之前（见 docs/backtest_semantics.md）。

## 持久化

- **PostgreSQL**（可降级 SQLite）：artifact 元数据、实验状态、订单/成交记录。SQLAlchemy Core/ORM + Alembic。
- **期货事件日志**：`futures_events` 是仿真账户的权威记录；`futures_simulations.snapshot` 是同事务更新的读取快照，加载时校验 hash chain 和事件头。
- **Parquet + PyArrow + DuckDB**：研究数据（行情、特征），PIT 字段随行存储。
- **本地文件系统 ArtifactStore**：内容按 `content_hash` 校验；接口可换 S3/MinIO。

## 可观测性

结构化 JSON 日志（`observability/logging.py`），contextvar 绑定 `trace_id / experiment_id / backtest_run_id / strategy_id / order_id`。Metrics/Tracing 留有接口位，后续接 OpenTelemetry/Prometheus 不改领域代码。

## 未来演进路径

- 拆服务：模块边界即服务边界候选（data、backtest、execution 各自可独立）。
- JobRunner 协议已定义，`LocalJobRunner` 可替换为分布式任务系统而不动领域逻辑。
- Multi-Agent 编排只允许在通过 Eval 证明收益后加入，且永不进入交易关键路径。

ADR 索引见 [adr/](adr/)。
