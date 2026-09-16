# quant-system

面向 A 股的个人量化交易系统：PIT 正确的数据层、事件驱动权威回测、不可变 Artifact 血缘、Paper Trading 优先。

**核心原则**

1. Agent 只做研究，不碰真钱订单执行。
2. Trading Plane 在所有 LLM 服务不可用时可独立运行。
3. 回测正确性 / PIT 正确性 / 交易规则 / 风控 / 可回放性优先于 Agent。
4. 严禁使用未来信息（`available_at > as_of` 的数据不可见）。
5. 一切实验、模型、数据、策略、回测结果可复现、可追溯。

详细设计见 [docs/architecture.md](docs/architecture.md)，因子登记与晋级规则见
[docs/factor_registry.md](docs/factor_registry.md)，多模型配置见
[docs/multi_agent_llm.md](docs/multi_agent_llm.md)。

## 安装

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # 本地开发（SQLite 即可跑全部测试）
pip install -e ".[dev,postgres]" # 需要 PostgreSQL 时
```

## 启动 PostgreSQL（可选，元数据持久化）

```bash
docker compose up -d postgres
```

默认连接串见 `src/quant_system/infra/settings.py`，可用环境变量 `QUANT_DB_URL` 覆盖。
样例回测脚本未设置时自动回退到 SQLite（`var/quant.db`），所有功能与测试均可运行。

## 运行数据库迁移

```bash
alembic upgrade head
```

## 生成 fixture 数据

```bash
python scripts/generate_fixtures.py
```

生成确定性合成 A 股数据到 `var/fixtures/cn_a/`，覆盖：正常股票、ST、停牌、退市、涨跌停、次新股、低流动性等场景。**合成数据，不代表任何真实市场表现。**

## 运行测试

```bash
pytest
```

## 运行样例回测

```bash
python scripts/run_sample_backtest.py
```

产物（equity curve / orders / trades / positions / event log / metrics）写入 `var/artifacts/backtests/<backtest_id>/`，并在 Artifact Registry 中注册血缘。

示例策略为横截面动量 Top-K，**仅用于验证框架管线，不构成任何盈利声明**。

## 启动 API

```bash
# 使用样例回测的 SQLite 元数据库：
QUANT_DB_URL="sqlite:///$PWD/var/quant.db" \
  uvicorn quant_system.api.app:factory --factory --port 8000
# 或使用 docker compose 的 PostgreSQL（默认）：
# uvicorn quant_system.api.app:factory --factory --port 8000
```

打开 http://127.0.0.1:8000/docs 查看接口。

## 启动 Dashboard

```bash
cd web/dashboard
npm install
npm run dev
```

打开 http://127.0.0.1:5173（开发服务器已代理 `/api` 到 8000 端口）。先运行一次样例回测以便有数据可看。

## 目录结构

```
configs/          市场规则 / 风控 / 回测 / 策略 / 晋升门槛（全部版本化配置）
migrations/       Alembic 迁移
scripts/          fixture 生成、样例回测
src/quant_system/
  domain/         领域模型、枚举、异常（不依赖 FastAPI/DB/UI）
  artifacts/      不可变 Artifact 协议、存储、注册表、血缘
  data/           PIT 数据存储、Provider 适配器、Universe
  rules/          MarketRuleBook（A 股规则，配置驱动、生效日期化）
  backtest/       事件驱动回测引擎、账本、成交/滑点/费用、回放
  risk/           事前风控、运行时熔断、KillSwitch
  execution/      OMS、BrokerGateway、PaperBroker、对账
  research/       实验生命周期、Scorecard、晋升校验
  agents/         Tool Protocol（研究工具边界）
  api/            FastAPI
  observability/  结构化日志
web/dashboard/    React + TypeScript + Vite + ECharts
tests/            unit / integration / backtest / replay
docs/             架构文档与 ADR
```

## 安全边界

- 不存在 `send_live_order` / `change_risk_limit` / `transfer_funds` 工具。
- Agent 可以产生 `DeploymentProposal`，但无法激活实盘部署。
- 未实现任何真实券商适配器；只有 `PaperBrokerGateway`。
- 详见 [docs/trading_safety.md](docs/trading_safety.md)。
