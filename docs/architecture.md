# Architecture

## 总体形态：Modular Monolith + Ports/Adapters

单进程单仓库，但保持严格的模块边界（见 ADR-0001）。领域层（`domain/`）不 import FastAPI、SQLAlchemy ORM、UI。所有外部依赖（数据源、券商、存储、任务系统）通过 `Protocol` 定义端口，注入适配器。

```
┌─────────────────────────── Research Plane ────────────────────────────┐
│  agents/tools ─→ experiments ─→ backtest ─→ scorecard ─→ promotion    │
│        (LLM 可选，全部离线可运行)                                        │
└──────────────────────────────┬────────────────────────────────────────┘
                               │ StrategyPackage / DeploymentProposal
                               │ (只能提案，不能激活)
┌──────────────────────────────▼────────────────────────────────────────┐
│                          Trading Plane                                 │
│  strategy runtime → rebalancer → pretrade risk → OMS → PaperBroker    │
│  → ledger → reconciliation → SAFE_MODE / KillSwitch                   │
│        (零 LLM 依赖，独立运行)                                           │
└────────────────────────────────────────────────────────────────────────┘
```

## 模块边界

| 模块 | 职责 | 依赖 |
|---|---|---|
| `domain` | 类型、枚举、领域模型、异常、JobRunner 协议 | 仅 pydantic |
| `artifacts` | 不可变 ArtifactEnvelope、Store、Registry、Lineage | domain, infra(持久化) |
| `data` | PITDataStore、Provider 协议、LocalParquetProvider、PIT Universe | domain |
| `rules` | MarketRuleBook 协议 + AShareRuleBook（configs/markets/cn_a.yaml 驱动） | domain |
| `backtest` | 事件驱动引擎、时钟、Feed、Exchange、Fill/Slippage/Fee、Ledger、EventStore、Replay | domain, rules, data, risk, execution |
| `risk` | PreTradeRiskEngine（插件规则）、RuntimeRiskMonitor、KillSwitch | domain |
| `execution` | OMS 状态机、BrokerGateway 协议、PaperBroker、Reconciliation | domain, risk |
| `research` | Experiment 状态机、Scorecard、PromotionPolicy | domain, artifacts |
| `agents` | ToolCall/ToolResult 协议、研究工具集 | research, artifacts, backtest |
| `api` | FastAPI 只读查询 + 少量控制端点 | 以上全部（组合根） |

## 关键数据流（每日）

```
Day T close:  market data finalized → features → signal → PortfolioTarget
Day T+1:      generate orders → pretrade risk → PaperBroker/ExchangeSim
              → fills → ledger → PnL → reconciliation
```

严禁"用 T 收盘价生成信号又按 T 收盘价成交"。引擎断言 decision timestamp 在成交 bar 之前（见 docs/backtest_semantics.md）。

## 持久化

- **PostgreSQL**（可降级 SQLite）：artifact 元数据、实验状态、订单/成交记录。SQLAlchemy Core/ORM + Alembic。
- **Parquet + PyArrow + DuckDB**：研究数据（行情、特征），PIT 字段随行存储。
- **本地文件系统 ArtifactStore**：内容按 `content_hash` 校验；接口可换 S3/MinIO。

## 可观测性

结构化 JSON 日志（`observability/logging.py`），contextvar 绑定 `trace_id / experiment_id / backtest_run_id / strategy_id / order_id`。Metrics/Tracing 留有接口位，后续接 OpenTelemetry/Prometheus 不改领域代码。

## 未来演进路径

- 拆服务：模块边界即服务边界候选（data、backtest、execution 各自可独立）。
- JobRunner 协议已定义，`LocalJobRunner` 可替换为分布式任务系统而不动领域逻辑。
- Multi-Agent 编排只允许在通过 Eval 证明收益后加入，且永不进入交易关键路径。

ADR 索引见 [adr/](adr/)。
