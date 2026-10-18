# quant-system

面向 A 股与中国商品期货的个人量化交易系统：PIT 正确的数据层、事件驱动权威回测、不可变 Artifact 血缘、Paper Trading 优先。

**核心原则**

1. Agent 只做研究，不碰真钱订单执行。
2. Trading Plane 在所有 LLM 服务不可用时可独立运行。
3. 回测正确性 / PIT 正确性 / 交易规则 / 风控 / 可回放性优先于 Agent。
4. 严禁使用未来信息（`available_at > as_of` 的数据不可见）。
5. 一切实验、模型、数据、策略、回测结果可复现、可追溯。

详细设计见 [docs/architecture.md](docs/architecture.md)，因子登记与晋级规则见
[docs/factor_registry.md](docs/factor_registry.md)，多模型配置见
[docs/multi_agent_llm.md](docs/multi_agent_llm.md)，真实日线同步见
[docs/tushare_data.md](docs/tushare_data.md)，期货仿真语义见
[docs/futures.md](docs/futures.md)。模块任务与公共 API 见
[docs/research_modules.md](docs/research_modules.md)，当前实现/验收以
[docs/project_progress.md](docs/project_progress.md) 为单一事实来源。

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

## 同步 Tushare Pro 日线

注册 Tushare Pro 并取得 Token 后，将 `TUSHARE_TOKEN` 写入本地 `.env`，再执行：

```bash
set -a && source .env && set +a
python scripts/sync_tushare_daily.py --start 2024-01-01 --end 2024-06-30
```

同步器输出未复权沪深日线到 `var/data/tushare/cn_a_daily/`。首次使用、积分要求、PIT
边界和真实数据回测命令见 [docs/tushare_data.md](docs/tushare_data.md)。

日常运行推荐使用 A 股 + 国内期货统一 EOD 流水线：

```bash
set -a && source .env && set +a
python scripts/run_daily_market_pipeline.py --trade-date auto --assets equity
```

命令先执行质量门禁，再发布不可变 `DATA_SNAPSHOT` 和确定性 `MARKET_SNAPSHOT`；失败不会
产生可供回测使用的数据版本。Dashboard 的“市场”页通过只读 API 展示最近快照。

## 黄金观察与配置

```bash
.venv/bin/python scripts/sync_gold.py
```

Dashboard 的 `/gold` 展示人民币现货、四只黄金 ETF 的真实历史行情、风险比较及独立手工配置。
“刷新数据并分析”会每次重新采集，先本地计算固定规则，再由独立视角审议、针对分歧修订和不同模型终审，
展示真实阶段并保存完整审计报告；需要加载模型凭据，可能产生 API 费用。单机 API 使用 `--workers 1`。
研究对象默认 Au99.99 现货，也可选ETF；现货按元/克分析，不代表银行积存金或金条成交报价。
仅终审PASS才在报告末尾发布简短的3至12个月建议、事实依据和复核条件，数字绑定本轮快照，不保证收益。
宏观事实可从独立模块的冻结快照注入；缺口未齐时明确弃权，不把链接或模型意见当事实。
四方观点、修订和审计记录均可展开核验，首批来源接入不放宽黄金发布政策。
行情无需账号；上海18:00之前最多纳入昨天，之后可纳入当天，页面注明源站实际日期。
来源、首次真实结果和诊断限制见 [docs/gold.md](docs/gold.md)。当前无独立样本外，结论仅研究观察，不自动晋级。
AU 期货复用已有市场快照；不执行黄金交易，不把股票回测账户作为个人持仓。

## 模块化研究（2026-10-17）

`/research` 已是黄金、宏观、因子及联合计划的公共工作区：模块目录、计划预览与显式确认、
根/子任务树、后台真实事件、预算、调用收据、协作取消和历史。`/gold` 保留黄金行情、配置和专属报告。
公共接口为 `/api/research/modules`、`/plans`、`/runs` 及运行详情/事件/收据/取消；
旧黄金提交202、旧因子同步201保留，使用同一个 Runtime，不重复执行。

通用内核已实现 models/ports/calls/storage/evidence/planning/tools/engine。
单 API worker，一个活动根槽、最多两个并行子任务、根级最多三个模型调用并发；
计划最多6个节点，不递归规划。规则计划零模型调用，模型计划可选且计费存档，须显式确认。
根/子任务/planner/修复/终审共享预算；受管传输无隐式重试，未知 usage 保守计入，不承诺美元硬限额。
启动只标记中断/未知，不自动重放；工具是默认拒绝的可信 in-process 能力，不是沙箱或强制中断环境。
父级只确定性汇总模块结果和授权 hash，不新增金融建议门禁或部署权限。

`gold.collect` 后研究冻结快照；`factor.research` 显式选择 fixture 或校验并绑定真实快照 provider，
原 FactorLab/Promotion 不变；`macro.collect` 无 LLM、`macro.analyze` 本地、`macro.research`
为双独立视角，不提供投资建议。首批精确来源为 FRED DGS10/DFII10/DTWEXBGS/DEXCHUS 与
SAFE 黄金数量及美元估值；广义美元不是 DXY，中国储备不是全球央行需求。
免费 `FRED_API_KEY` 可选，当前可用 key/线上 FRED 未验收，SAFE HTML 样本不代表完整生产宏观报告；
实际抓取可用时间不等于历史 PIT，未完成 ALFRED 自动历史归档/认证。来源见 [docs/macro.md](docs/macro.md)。
任务 YAML 配置由主实现继续接线与验收，只做角色映射、预算收紧、工具权限收窄。
未新增付费真实模型 canary；最终验收待最终全量回归更新。

## 运行测试

```bash
.venv/bin/python -m pytest
.venv/bin/python -m mypy src scripts/run_baseline_research.py
.venv/bin/python -m ruff check .
```

提交前运行以上三项。代码格式使用 Ruff，日常只格式化本次修改的文件：

```bash
.venv/bin/python -m ruff format <本次修改的Python文件>
```

## 运行样例回测

```bash
python scripts/run_sample_backtest.py
```

产物（equity curve / orders / trades / positions / event log / metrics）写入 `var/artifacts/backtests/<backtest_id>/`，并在 Artifact Registry 中注册血缘。

示例策略为横截面动量 Top-K，**仅用于验证框架管线，不构成任何盈利声明**。

## 冻结真实历史研究

固定股票池的周频动量基线支持联网准备、冻结快照、离线开发/验证/留出回测、等权与沪深300基准比较，以及账本回放和重复运行验收：

```bash
.venv/bin/python scripts/run_baseline_research.py prepare
.venv/bin/python scripts/run_baseline_research.py run --snapshot-id <准备输出的快照ID>
```

准备阶段需要加载本地 `TUSHARE_TOKEN`。代码、数据和产物分层、数据来源、复现方式及能力限制见 [docs/baseline_research.md](docs/baseline_research.md)。这是研究报告，不自动晋级或部署策略。

已完成的真实实验见 [结果摘要](docs/baseline_research_results.md)。

真实 Kimi/DeepSeek 四角色发现候选并接入冻结数据回测的入口为 [scripts/run_agent_backtest.py](scripts/run_agent_backtest.py)，调用证据、实际收益和限制见 [完整流程结果](docs/agent_backtest_results.md)。这一路径会产生模型API费用。

## 运行期货样例

期货样例回测复用 Dashboard 的期货 OMS、风控、撮合、账本和结算逻辑：

```bash
python scripts/run_sample_futures_backtest.py
```

三个商品期货合约和日线均为确定性 fixture，只用于验证多空、开平、保证金和逐日盯市。

## 启动 API

```bash
# 启用多智能体研究时先加载本地 .env：
set -a && source .env && set +a
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

公共研究入口位于 http://127.0.0.1:5173/research，按模块预览计划后确认执行。
因子四角色只生成及审查研究 Artifact，本地 FactorLab 和硬门槛决定是否通过；
黄金和宏观使用自己的任务/角色契约，所有 Agent 均不进入交易执行路径。

## 目录结构

```
configs/          市场规则 / 风控 / 回测 / 策略 / 研究协议 / 晋升门槛
migrations/       Alembic 迁移
scripts/          命令入口：数据同步、样例回测、冻结研究；领域逻辑放在 src/
src/quant_system/
  domain/         领域模型、枚举、异常（不依赖 FastAPI/DB/UI）
  artifacts/      不可变 Artifact 协议、存储、注册表、血缘
  data/           PIT 数据存储、Provider、Universe；ingestion/ 负责外部采集
  rules/          MarketRuleBook（A 股规则，配置驱动、生效日期化）
  backtest/       事件驱动回测引擎、账本、成交/滑点/费用、回放
  risk/           事前风控、运行时熔断、KillSwitch
  execution/      OMS、BrokerGateway、PaperBroker、对账
  futures/        独立期货领域、OMS、保证金账本、仿真、回测与 Gateway 端口
  gold/           黄金公开行情、配置估值、固定规则诊断、独立审议与建议终审
  macro/          独立宏观模型、FRED/SAFE 采集冻结与本地分析
  research/       实验生命周期、Scorecard、晋升校验；baseline/ 冻结历史评估
    runtime/      通用任务、调用预算、Artifact 仓储、证据、计划、工具与调度
    modules/      黄金/因子/宏观薄适配器，显式注册，不把金融语义写入内核
  strategies/     信号与组合决策，不直接采集数据或执行交易
  agents/         模型路由/网关、领域审议协议与兼容编排，不进入交易路径
  api/            FastAPI
  observability/  结构化日志
web/dashboard/    React + TypeScript + Vite + ECharts
tests/            unit / integration / backtest / replay
docs/             架构文档与 ADR
var/              本地数据、元数据库、不可变报告及源码归档（不进入 Git）
```

新增代码优先放入已有职责模块；共用逻辑由所属模块提供明确函数，不跨模块调用私有方法。
股票和期货的会计与规则不同，不为消除表面相似而合并领域。生成目录（环境、缓存、依赖、构建输出）不作为源码维护。
VS Code 搜索和 Python 分析排除 `var/`，避免历史归档出现重复定义；数据仍可从文件树查看。

## 安全边界

- 不存在 `send_live_order` / `change_risk_limit` / `transfer_funds` 工具。
- Agent 可以产生 `DeploymentProposal`，但无法激活实盘部署。
- 未实现任何真实券商或 CTP 适配器；只有股票与期货本地 Paper Gateway。
- 详见 [docs/trading_safety.md](docs/trading_safety.md)。
