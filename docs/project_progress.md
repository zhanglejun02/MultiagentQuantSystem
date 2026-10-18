# 项目进度文档（Project Progress）

> 本文档是项目的"单一事实来源"进度页：简介、架构、当前状态、数据缺口与路线图。
> 每次有里程碑级变更时更新本文档。最后更新：2026-10-17。
> 通用研究运行时、三个独立模块及公共工作台已完成本地验收；真实模型质量与线上 FRED 验收另行进行。

---

## 1. 项目简介

个人 A 股/期货量化投研系统，Python modular monolith（`src/quant_system/`）。核心理念：

- **PIT-first**：所有数据带 `available_at`，查询强制 `as_of`，杜绝前视偏差与幸存者偏差（ADR-0004）。
- **事件驱动回测为权威**：信号 T 收盘后生成 → T+1 开盘成交，严禁同 bar 成交；确定性回放 + SHA-256 事件链（ADR-0005）。
- **Agent 不进交易关键路径**：多 LLM 智能体只在 Research Plane 提案/审查因子，硬门槛由本地代码裁决，LLM 宕机不影响交易（ADR-0002）。
- **不可变 Artifact + 血缘**：内容寻址、版本追加、血缘追踪，缺失血缘 = 晋升硬失败（ADR-0003）；新增研究类型不改写旧档。
- **仅 Paper 交易**：PaperBroker，无真实券商适配；期货为独立 bounded context（ADR-0009）。

## 2. 架构总览

```
Research Plane    → ResearchRuntime + 显式任务模块 + FactorLab + Experiment Registry
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
| Agent 编排 | `src/quant_system/agents/` | 证据/观点协议、独立并发审查、有限修订和预算；黄金另有独立终审 |
| 通用研究 | `src/quant_system/research/runtime/` | models/ports/calls/storage/evidence/planning/tools/engine；不包含金融判断或 Promotion |
| 研究模块 | `src/quant_system/research/modules/` | gold/factor/macro 适配器；角色任务局部化，组合根显式注册 |
| 宏观领域 | `src/quant_system/macro/` | 独立模型、FRED/SAFE 采集解析、保守可用时间与本地分析 |
| 数据层 | `src/quant_system/data/` | PITDataStore + LocalParquetProvider，Parquet + DuckDB |
| 市场规则 | `src/quant_system/rules/` + `configs/markets/cn_a.yaml` | 配置驱动涨跌停、ST、lot size、T+1 |
| 期货 | `src/quant_system/futures/` | 双向持仓、今昨仓、逐日结算、hash chain 事件日志 |
| 控制面 | `risk/` + `execution/` | REDUCE_ONLY/CANCEL_ONLY/SAFE_MODE/HALTED/KILLED 权限门禁 |

### 多智能体投研闭环

1. **researcher**（Kimi）提出受限 DSL 因子 → 本地解析器拒绝非法表达式（允许一次定向修复）
2. **FactorLab 本地确定性计算** IC/覆盖率/换手/多空组合（LLM 不生成数字），固定候选再审查
3. **critic/statistician**（DeepSeek）并行独立审查，只看表达式、horizon和本地结果，不看研究员故事或对方意见
4. 根、子任务、planner 与终审共享调用/Token账本，并发预留，格式修复也计数，调用收据按根请求序号归档
5. **orchestrator**（Kimi）汇总，但**不能覆盖本地硬门槛**
6. 全程调用轨迹 + RESEARCH_REPORT Artifact 持久化；预算约束次数与Token，不承诺已按美元核费

黄金采用独立视角协议：本地证据 → 技术/宏观/量化/风险 → 最大分歧 → 一次作者修订 → 统筹 → 独立终审。
宏观可注入冻结事实，证据不足时本地弃权，不进行模型调用；部分来源接入不自动放宽黄金政策。
细节见 [multi_agent_llm.md](multi_agent_llm.md)、[research_modules.md](research_modules.md) 和 [ADR-0011](adr/0011-modular-research-runtime.md)。

安全边界：类型化工具默认拒绝，要求注册、任务/角色 allowlist 与 capability；禁止交易工具清单是额外护栏，非沙箱。

## 3. 当前状态（2026-10-17）

| 维度 | 状态 | 备注 |
|------|------|------|
| 核心回测引擎 | 🟢 完成 | 确定性回放、PIT 正确性有测试守护 |
| 测试 | 898项Python通过 | mypy 141文件、Ruff、diff检查通过；研究界面31项含Chrome、黄金界面73项、生产构建通过 |
| 文档 | 本轮已同步 | 新增任务手册和 ADR-0011；宏观来源口径由独立宏观文档维护 |
| Web Dashboard | 公共研究页已验收 | `/research` 模块/计划/任务树/事件/预算/收据/取消/历史；`/gold` 专属报告保留，1440/390/320夹具和1440/390真实只读检查通过 |
| 通用研究运行时 | 已实现 | 单根槽、子任务最多2并行、根模型最多3并发；计划最多6节点、不递归 |
| 任务 YAML 配置 | 已接入组合根 | 仅角色映射、收紧预算与工具权限；宏观来源配置也已接入，不动态导入代码或授予额外工具 |
| 独立宏观模块 | 已实现，真实来源验收有限 | FRED/SAFE 解析冻结、本地分析、双独立视角；无线上 FRED 或完整真实生产宏观报告验收声明 |
| 多 LLM Agent | 🟡 alpha | Kimi + DeepSeek 真实 API 跑通闭环；研究产出尚未进入生产晋级 |
| 数据层 | 🟡 **主要瓶颈** | 见下节 |
| 期货模块 | 🟡 beta | 日线 Paper 仿真完整；`fut_daily` 无权限，仅 fixture 数据 |
| 执行 | 🟡 Paper only | 按设计不接真实券商 |
| 真实历史基线研究 | 🟢 技术闭环完成 | 冻结真实数据、固定时间分区、基准/等权/成本压力、回放与复现；不代表策略有效或可部署 |

### 模块化研究运行时（2026-10-17）

- 内核 `models/ports/calls/storage/evidence/planning/tools/engine` 已落地，组合根显式注册三类模块；局部角色为命名空间字符串，不扩大全局 `Literal`，非金融文档任务夹具验证可独立扩展。
- 一个活动根槽、两个子任务并行、根模型最多三个并发。根、子任务、planner、修复与终审共用账本；planner 原始审计与用量执行时仅结转一次，未知 usage 保守记账。受管传输无隐式重试，不承诺美元硬限额。
- 计划最多6节点，仅根/子两层；注册任务/schema/InputRef/依赖/预算本地校验，无递归规划。规则计划无 LLM；模型计划可选且存档，显式确认后执行，登记后内容更改拒绝。
- `RESEARCH_RUN/EVENT/CALL/PLAN` 追加不可变保存；事件每 run 连续编号，调用 root 内全局稳定。字节/JSON 原子写入不等于文件/数据库事务；启动标记 `INTERRUPTED/UNKNOWN`，不重放，GET 不恢复写入。
- `FACT/DERIVED/OPINION`、hash、父血缘与知识截止被校验。LATEST 先采集再冻结，HISTORICAL 禁止当前采集；取消协作式，工具前后检查 deadline，不强杀可信 in-process 调用，退出前不释放活动槽。
- 黄金 `gold.collect` 后执行冻结 `gold.research`，宏观只经注入 reader 提供事实，无直接领域 import。旧 facade 使用同一 Runtime；旧 GOLD_RESEARCH_RUN 只读，恢复写入显式执行，旧报告不重写。
- 因子公共/旧 API、测试与 CLI 使用共享运行机制；旧 API 同步201保留。真实模式校验快照 hash 并绑定实际 FactorLab provider，不能仅挂 ID；fixture 明确区分，领域 acceptance/Promotion 不变。
- 宏观独立模型/FRED/SAFE/可用时间与分析；`macro.collect` 无 LLM、`macro.analyze` 本地、`macro.research` 两独立视角加确定性汇总，不提供投资建议。
- `api/research.py` 已在 `app.py` 注册模块、计划、运行、详情、事件、收据和协作取消。旧黄金202/因子201不变，任务局部检查凭据；公共 `/research` 已替换因子专属串行假进度。
- 当前黄金宏观规则例子为 `macro.collect/gold.collect/macro.analyze/gold.research` 四节点。父级只确定性汇总子状态/摘要/报告及获准 hash，不新增金融建议模型门禁；root publication 不授予部署权限，不把子意见当事实或重发被拒绝建议。

最终本地验收：898项Python测试、mypy 141个文件、Ruff及差异检查通过；研究工作台31项测试含独立Chrome，黄金73项测试及生产构建通过。
Chrome夹具覆盖1440/390/320宽度、计划确认、父子任务、取消、错误停查和未知结果版本；真实研究/黄金页面1440/390只读检查无JS错误或横溢。
API已启动于8000，Dashboard 5173；三个领域的六个任务与内部规划任务均已注册，当前无活动任务。
黄金状态另提供 `active_gold_run_id`，其他模块占用时不误轮询黄金详情；共享槽位仍阻止重复研究。
仅既有FastAPI/Starlette弃用提示及前端bundle体积警告。未调用付费模型、未新增线上FRED验收、未写虚构持仓；旧实测不代表新Runtime模型质量。

因子输入现由 `BoundFactorInput` 固定快照ID、hash、路径及新建源；包装provider的行情、交易日和复权值按冻结源校验，
评估前后与报告前复核内容完整性。回归覆盖同目录错误缓存、factory污染、伪造返回和CLI开发截止兼容。
允许可信包装做开发期截止或子集过滤；不是恶意Python插件沙箱，也不承诺捕获瞬时篡改后复原。

### 历史里程碑

以下按日期保留当时的数据能力与验收结果，不覆盖2026-10-17的当前状态。

每日数据流水线（`scripts/run_daily_market_pipeline.py`）已实测：2026-09-18 A 股日线 5209 行 + adj_factor 5565 行发布成功（质量门禁 → 不可变 DataSnapshot → MarketSnapshot）；`--assets all` 因期货权限 fail-closed。

2026-10-13 完成第一份真实历史研究基线：8 只股票、2278 根真实日线、2025 年开发/验证/留出三段；20 日周频动量留出收益 3.30%，沪深300价格收益 17.63%，不支持晋级。新增独立 `prepare/run` CLI、原始响应与源码归档、快照强制消费及基准完整性检查。操作见 [baseline_research.md](baseline_research.md)，结果及数据限制见 [baseline_research_results.md](baseline_research_results.md)。

2026-10-14 做行为保持的代码整理：共用日线归一化入口、东方财富请求去重、研究工具统一异常边界、分红准备与单场景研究验收拆分。保留现有 API、命令和冻结产物；README 补充模块边界与开发检查，编辑器排除历史归档和缓存干扰。

2026-10-14 完成真实四角色候选发现到回测：Kimi/DeepSeek 四次成功调用、2607 Token；候选 `VolPriceDivergence` 开发期 IC 门槛失败，模型均拒绝。仍完成15次审计回测，留出收益 -2.29%，不支持晋级；不是换参数改善旧基线。详情见 [agent_backtest_results.md](agent_backtest_results.md)。

### 黄金观察与配置（2026-10-15）

新增 `/gold`：人民币 Au99.99 现货 + 四只黄金 ETF，支持K线、共同日期收益比较、区间回撤、
波动率和独立手工配置。原始响应、行情、配置均内容寻址保存并建立血缘，缺价不按零估值。
上金所官方现货与腾讯公开ETF日线已实际采集；仅用于观察，不是认证PIT回测数据。
AU 辅助复用已有流水线，无数据明确显示未接入；宏观为官方查询链接，未实现宏观序列或自动交易。
同步、来源和估值口径见 [gold.md](gold.md)。

2026-10-16 黄金页升级为行情与研究工作区：账户摘要、现货/ETF价格标签与金色走势，
“刷新数据并分析”逐轮重新采集并冻结行情，复用四角色模型路由，输出双均线/波动率/止损规则、
本地下一开盘诊断、同初始仓位基准与双倍成本比较。异步阶段、历史报告、失败与原始调用审计均持久化；
仅支持本地单worker，同时一个任务；此处为V1历史记录，V2预算见下节，无美元费用承诺。

首轮真实采集截至2026-10-15，Kimi/DeepSeek四次调用、7640 tokens；518880候选当前规则权重0，
全样本诊断收益3.05%低于同初始仓位买入持有4.10%，双倍成本2.91%，最终继续观察。
没有独立样本外，固定阻止晋级，不下单；宏观/新闻仍未自动采集。详细口径和产物ID见 [gold.md](gold.md)。

同日扩展Au99.99为默认研究对象：独立现货分析、元/克指标与金价参考组合，不要求ETF成功。
每轮报告末尾由统筹者给出简短3至12个月配置建议，绑定本地中长期金价事实，含2至3条依据和复核条件。
方向受长期证据约束，不随意建议加仓；仍为低置信度、无杠杆、不下单，旧报告不补造建议。
最终真实现货验收四次调用、9287 tokens，建议等待：近252观测收益4.97%，近126观测收益-15.75%，
当前现货收盘相对MA250为-9.07%；桌面1440与手机390的元/克指标、末尾建议与换行均通过验收。

### 多智能体架构V2（2026-10-16，历史 V4 验收）

- 黄金 `gold-deliberation-v4`：固定本地MA20/60、12%波动率目标、8%止损后生成证据包；
	技术/量化/风险首轮实际并发互盲。宏观缺数据由本地以0置信度弃权，不用LLM编造解释。
- 观点与事实分开：短 `ResearchState`、证据ID及反向证据；本地校验未知/空值引用、身份和置信度。
- 分歧检测本地化，只选最大分歧、相关双方各修订一次；保留初始/修订观点与补证请求，
	缺新证据不允许加置信度。未解决或预算跳过不算共识，宏观缺口使本版配置方向仅可等待。
- 独立终审只见事实与草案，模型不同于统筹，不见前面讨论。PASS才发布，REVISE最多一次修订复审，
	REJECT/不可用不发布建议，但仍归档研究。无OOS仍固定OBSERVE，不改变交易或部署权限。
- 通常5次有效调用，完整修订链最多9次；硬上限12次/64000 tokens包含格式修复，并受全局更低预算约束。
- 因子候选仍为受限DSL，改为先本地评估、双评审独立并行；四角色API与原晋级门槛保持兼容。
- 黄金页新增四方观点、分歧、修订前后对照与证据展开；旧报告不补造审议或终审。
- 自动宏观/仓位/资金流/新闻采集、补证工具与任意领域专家路由仍未实现；不宣称数据广度已改善。

本次验证以模拟网关和本地浏览器契约为主，V1真实运行记录不能冒称新版真实模型实测。
设计、范围与后续工作统一见 [multi_agent_llm.md](multi_agent_llm.md)。

历史验收：277项Python测试、119个文件mypy、Ruff、前端72项测试与生产构建通过；仅有既有FastAPI/Starlette弃用提示和前端bundle体积警告。不是2026-10-17最终计数。
独立Chrome在1440/390/320宽度核验新观点、修订、证据展开、收据及旧报告切换；真实页面1440/390只读检查无JS错误或横溢。
API已重载，黄金status为六角色V4、12次/64000 tokens，因子status仍为四角色；未触发新版付费研究，未写虚构持仓。

## 4. 数据现状与缺口

### 已有

- A 股未复权日线（Tushare，`available_at` = 交易日 17:00）
- 复权因子 adj_factor（pipeline 可拉，尚未进研究特征管线）
- 交易日历、股票基础资料（当前快照）
- 确定性合成 fixture（含 ST/停牌/退市/涨跌停/IPO 场景）
- 独立宏观 FRED/SAFE 采集解析、冻结及离线分析能力；不等于全部生产来源已经实测可用

### 首批宏观来源与验收边界

| 精确序列 | 口径 | 当前验收边界 |
|---|---|---|
| FRED `DGS10` / `DFII10` | 美国10年名义/通胀指数实际收益率，% | Provider/解析与离线契约已实现；未声明线上 FRED 验收 |
| FRED `DTWEXBGS` | 名义广义贸易加权美元指数，2006年1月=100，不是 ICE DXY | 免费 `FRED_API_KEY` 可选，当前可用 key 未验证 |
| FRED `DEXCHUS` | 纽约午间人民币/1美元，不是人民银行中间价 | 观测频率与发布频率分开，缺值不填0 |
| SAFE `CN_GOLD_RESERVES` / `CN_GOLD_RESERVES_USD` | 中国黄金储备万金衡盎司/亿美元估值 | 源站 HTML 样本读取不能代表完整真实生产宏观报告；估值变化不是买入量或全球央行需求 |

默认请求四个 FRED 序列及 SAFE 数量；数量采集同时加入独立类型的美元估值。`available_at` 不早于实际抓取，
观测日期、发布时间、vintage、入库时间分别记录；未自动建立 ALFRED 历史档案或历史 PIT 认证。
新闻、仓位、全球资金流未覆盖；可信工具不是沙箱/分布式系统，不承诺强制中断或 exactly-once。
来源细节见 [macro.md](macro.md)，该文档由宏观模块独立维护。

### 缺口（按优先级）

| 优先级 | 数据 | 用途 | 现状 |
|--------|------|------|------|
| P0 | 指数日线（沪深300/中证500 等） | 回测基准、超额收益计算 | 沪深300基线已接腾讯；统一日流水线历史消费仍待贯通 |
| P0 | 历史 ST/停牌/退市区间 | Universe PIT 正确性（当前仅当前快照） | 未接 |
| P0 | 财务三大报表 + 业绩预告/快报（带公告日 `ann_date`） | 基本面因子；PIT 化关键 | 未接 |
| P1 | 公司行动（分红、送转、配股） | 成本基准、复权校验 | 未接 |
| P1 | 行业分类（申万，历史成分） | 行业中性化、风险模型 | 预留端口未接 |
| P1 | 期货日线（6 交易所） | 期货回测真实数据 | Token 无 `fut_daily` 权限（40203） |
| P2 | 宏观广度与历史 vintage | 宏观因子、风控情景 | 首批 FRED/SAFE 能力已接；线上 FRED、完整生产报告、ALFRED 历史认证及 PMI 等仍缺 |
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

1. **数据与证据正确性（P0）**：完善真实历史股票池、ST/停牌和公司行动支付日/税务；修复策略晋级报告跨策略绑定缺口；第一份真实基线已完成，详见结果文档
2. 基线及 FactorLab 已支持现金行动 PIT 复权信号/未复权成交与真实快照绑定；继续贯通每日冻结数据和更完整历史研究，不将 fixture 结果冒称真实证据
3. 行业分类接入 + 因子行业中性化
4. `fut_daily` 权限开通后，期货回测切换真实数据
5. VectorizedResearchEngine（批量因子初筛，事件驱动引擎仍为晋升权威）
6. Promotion 增加多重检验门槛（Deflated Sharpe / FDR 控制）
7. 完成任务 YAML 接线及新 Runtime 最终全量/浏览器验收；在明确授权后分别做免费来源与付费模型 canary，不用历史测试替代
8. P2/P3：宏观更多序列与历史 vintage、分钟线、新闻舆情（Agent 素材）；不扩展为递归自治或自动交易

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
