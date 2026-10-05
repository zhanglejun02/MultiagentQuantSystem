# 项目进度文档（Project Progress）

> 本文档是项目的"单一事实来源"进度页：简介、架构、当前状态、数据缺口与路线图。
> 每次有里程碑级变更时更新本文档。最后更新：2026-10-05。

---

## 1. 项目简介

个人 A 股/期货量化投研系统，Python modular monolith（`src/quant_system/`）。核心理念：

- **PIT-first**：所有数据带 `available_at`，查询强制 `as_of`，杜绝前视偏差与幸存者偏差（ADR-0004）。
- **事件驱动回测为权威**：信号 T 收盘后生成 → T+1 开盘成交，严禁同 bar 成交；确定性回放 + SHA-256 事件链（ADR-0005）。
- **Agent 不进交易关键路径**：多 LLM 智能体只在 Research Plane 提案/审查因子，硬门槛由本地代码裁决，LLM 宕机不影响交易（ADR-0002）。
- **不可变 Artifact + 血缘**：12 种 Artifact 内容寻址、血缘追踪，缺失血缘 = 晋升硬失败（ADR-0003）。
- **仅 Paper 交易**：PaperBroker，无真实券商适配；期货为独立 bounded context（ADR-0009）。

## 2. 架构总览

```
Research Plane    → 多 Agent + FactorLab + Experiment Registry
Validation Plane  → PIT/OOS/walk-forward/多重检验 + Promotion 6 道硬门槛
Strategy Plane    → SignalSpec + PortfolioConstructionSpec + StrategyPackage
Deployment Plane  → DRAFT→PAPER→SHADOW→CANARY→LIVE（人工审批）
Execution Plane   → OMS + SafetyGatedBrokerGateway (PaperBroker)
Global Risk Plane → NAV / gross-net / margin / 策略预算 / KillSwitch
```

关键模块：

| 模块 | 位置 | 说明 |
|------|------|------|
| 回测引擎 | `src/quant_system/backtest/` | 事件驱动、T+1、涨跌停/停牌/成交量参与率、Decimal 会计 |
| 因子注册 | `src/quant_system/research/` | PROPOSED→…→STRATEGY_ELIGIBLE 生命周期，正向晋级需 VERIFIED 验证报告 |
| Agent 编排 | `src/quant_system/agents/` | researcher/critic/statistician/orchestrator 四角色闭环 |
| 数据层 | `src/quant_system/data/` | PITDataStore + LocalParquetProvider，Parquet + DuckDB |
| 市场规则 | `src/quant_system/rules/` + `configs/markets/cn_a.yaml` | 配置驱动涨跌停、ST、lot size、T+1 |
| 期货 | `src/quant_system/futures/` | 双向持仓、今昨仓、逐日结算、hash chain 事件日志 |
| 控制面 | `risk/` + `execution/` | REDUCE_ONLY/CANCEL_ONLY/SAFE_MODE/HALTED/KILLED 权限门禁 |

### 多智能体投研闭环

1. **researcher**（Kimi）提出受限 DSL 因子 → 本地解析器拒绝非法表达式（允许一次定向修复）
2. **critic**（DeepSeek）审查 PIT 安全、经济逻辑、过拟合风险
3. **FactorLab 本地确定性计算** IC/覆盖率/换手/多空组合（LLM 不碰数字生成）
4. **statistician**（DeepSeek）解释指标
5. **orchestrator**（Kimi）汇总，但**不能覆盖本地硬门槛**
6. 全程调用轨迹 + RESEARCH_REPORT Artifact 持久化；预算上限（候选数/调用数/Token/费用）

安全边界：`FORBIDDEN_TOOL_NAMES` 硬拒下单、改风控、转账类工具。

## 3. 当前状态（2026-10-05）

| 维度 | 状态 | 备注 |
|------|------|------|
| 核心回测引擎 | 🟢 完成 | 确定性回放、PIT 正确性有测试守护 |
| 测试 | 🟢 183 通过 | unit/integration/backtest/replay 全绿；mypy 100 files 0 错误；ruff 通过 |
| 文档 | 🟢 完成 | docs/ + 9 个 ADR |
| Web Dashboard | 🟢 可用 | React+TS+Vite+ECharts，6 页 + 市场页 |
| 多 LLM Agent | 🟡 alpha | Kimi + DeepSeek 真实 API 跑通闭环；研究产出尚未进入生产晋级 |
| 数据层 | 🟡 **主要瓶颈** | 见下节 |
| 期货模块 | 🟡 beta | 日线 Paper 仿真完整；`fut_daily` 无权限，仅 fixture 数据 |
| 执行 | 🟡 Paper only | 按设计不接真实券商 |

每日数据流水线（`scripts/run_daily_market_pipeline.py`）已实测：2026-09-18 A 股日线 5209 行 + adj_factor 5565 行发布成功（质量门禁 → 不可变 DataSnapshot → MarketSnapshot）；`--assets all` 因期货权限 fail-closed。

## 4. 数据现状与缺口

### 已有

- A 股未复权日线（Tushare，`available_at` = 交易日 17:00）
- 复权因子 adj_factor（pipeline 可拉，尚未进研究特征管线）
- 交易日历、股票基础资料（当前快照）
- 确定性合成 fixture（含 ST/停牌/退市/涨跌停/IPO 场景）

### 缺口（按优先级）

| 优先级 | 数据 | 用途 | 现状 |
|--------|------|------|------|
| P0 | 指数日线（沪深300/中证500 等） | 回测基准、超额收益计算 | 未接 |
| P0 | 历史 ST/停牌/退市区间 | Universe PIT 正确性（当前仅当前快照） | 未接 |
| P0 | 财务三大报表 + 业绩预告/快报（带公告日 `ann_date`） | 基本面因子；PIT 化关键 | 未接 |
| P1 | 公司行动（分红、送转、配股） | 成本基准、复权校验 | 未接 |
| P1 | 行业分类（申万，历史成分） | 行业中性化、风险模型 | 预留端口未接 |
| P1 | 期货日线（6 交易所） | 期货回测真实数据 | Token 无 `fut_daily` 权限（40203） |
| P2 | 宏观数据（利率/汇率/PMI 等） | 宏观因子、风控情景 | 未接 |
| P2 | 分钟线 | 日内执行模拟精细化 | 未接（当前 V0 不需要） |
| P3 | 新闻/舆情/公告 | Agent 研究素材（不进交易路径） | 未接 |

### 数据源调研结论（2026-10-05）

| 方案 | 费用 | 覆盖 | 适配本项目 |
|------|------|------|-----------|
| **Tushare 2000 积分**（约 ¥200/年，1:10 兑换） | 200 次/分、10 万次/接口/日 | 指数日线、财务报表（含 ann_date）、分红、namechange/suspend_d、`fut_daily`、申万行业 | ✅ **首选主干**：一个 token 补齐 P0/P1 大部分；财务接口天然带公告日期，PIT 友好 |
| Tushare 5000 积分（约 ¥500/年） | 500 次/分、常规无上限 | 同上 + 更高频次 | 数据量大/多年回填时再升级 |
| Tushare 独立权限 | 分钟线 ¥2000 一次性；新闻 ¥1000/年；公告 ¥1000/年 | 分钟/新闻/公告 | ⏸ P2/P3 再议 |
| **AKShare**（免费、开源爬虫） | 无 token，受目标网站改版影响 | 指数、行业分类、宏观、新闻均有接口 | ✅ **备援 + 交叉验证**：适合宏观/行业等低频数据；注意无 SLA、无 PIT 时间戳，入库时需自行打 `available_at` |
| BaoStock（免费） | 低频 | A 股日线/财务 | ⚠️ 维护趋缓，仅作对照 |
| JQData / RQData | 个人版收费较高（数千/年起） | 机构级质量、分钟线、PIT 财务 | ⏸ 规模扩大后再评估 |
| Yahoo Finance / qlib 社区数据 | 免费 | A 股日线（质量一般） | ❌ 时间戳语义不符 PIT 要求，不采用 |

**决策建议**：Tushare 升 2000 积分作为唯一主干写入源（保持现有 PIT Parquet 契约），AKShare 做宏观/行业补充与交叉验证；每类新数据入库时必须定义 `available_at` 规则（行情=当日 17:00，财务=公告日次日 09:00 等）并写入 `docs/tushare_data.md`。

## 5. 第三方评估摘要（2026-10-05 对标）

对标对象：TradingAgents（109k★，LangGraph 多角色辩论）、Microsoft Qlib/RD-Agent（49k★，LLM 自动因子挖掘）、ai-hedge-fund（64k★，教育向）。

**相对优势**（多数开源框架不具备）：

1. **LLM 与交易路径硬隔离** + 禁止工具清单——TradingAgents/ai-hedge-fund 让 LLM 直接产出交易决策，复现性差且无审计链；本项目 LLM 只提案，数字全部本地确定性计算。
2. **PIT 纪律**——四时间戳 + 强制 `as_of`，对标框架普遍只有粗糙的"按日期过滤"（TradingAgents 的 Yahoo 财务甚至无法判断历史可见性）。
3. **不可变 Artifact 血缘 + 确定性回放**——同类开源项目几乎没有晋升门槛与复现契约。
4. **A 股市场规则建模**（涨跌停/ST/T+1/lot size）远比以美股为中心的框架完整。

**相对短板 / 风险**：

1. **数据广度不足**（本文档第 4 节）——对标框架标配新闻/基本面/宏观多模态输入，本项目 researcher 只有量价日线可用，因子假设空间受限。
2. **因子 DSL 表达力有限**——RD-Agent 可生成任意代码因子（含模型），本项目受限 DSL 更安全但天花板更低；可控放开需配 sandbox。
3. **验证统计深度**——多重检验（Deflated Sharpe、White's Reality Check 等）在 Promotion 门槛中尚未落地，候选因子量产后过拟合风控会成为瓶颈。
4. **无向量化研究引擎**——事件驱动引擎正确但慢，因子批量筛选时研究吞吐受限（VectorizedResearchEngine 预留未实现）。
5. **单人维护风险**——无 CI 流水线记录、依赖个人记忆；本文档即为缓解措施之一。

## 6. 路线图

1. **数据补齐（P0）**：Tushare 升 2000 积分 → 指数日线 + 历史 ST/停牌回填 + 财务报表（PIT 化 `ann_date`）→ 扩展 `run_daily_market_pipeline.py` 与质量门禁
2. 复权因子进入特征管线（研究用后复权、执行用未复权的双轨契约已在 docs 定义）
3. 行业分类接入 + 因子行业中性化
4. `fut_daily` 权限开通后，期货回测切换真实数据
5. VectorizedResearchEngine（批量因子初筛，事件驱动引擎仍为晋升权威）
6. Promotion 增加多重检验门槛（Deflated Sharpe / FDR 控制）
7. P2/P3：宏观、分钟线、新闻舆情（Agent 素材）

## 7. 常用命令

```bash
# 测试 / 类型 / lint
.venv/bin/pytest && .venv/bin/mypy src && .venv/bin/ruff check

# API（SQLite）
QUANT_DB_URL="sqlite:///$PWD/var/quant.db" uvicorn quant_system.api.app:factory --factory --port 8000

# 前端
cd web/dashboard && npm run dev   # 5173, 代理 /api → 8000

# 数据流水线（A 股 only）
.venv/bin/python scripts/run_daily_market_pipeline.py --assets equity --trade-date 20260918
```
