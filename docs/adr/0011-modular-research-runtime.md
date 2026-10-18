# ADR-0011：模块化研究运行时与领域任务分离

- 日期：2026-10-17
- 状态：已接受；通用内核、三类模块及公共入口已实现，任务 YAML 接线与最终全量验收待主实现更新
- 相关：[ADR-0002](0002-agents-outside-trading-path.md)、[ADR-0003](0003-immutable-artifacts.md)、[ADR-0010](0010-evidence-deliberation.md)

## 背景

黄金与因子原先重复维护生命周期、调用预算、格式修复、审计和恢复。
把黄金六角色或因子四角色提炼成全局团队，会让新领域继续依赖金融方向与封闭角色枚举；
仅记录快照 ID、逻辑调用数或运行完成，也不足以证明实际数据消费、费用上限或建议可发布。
需要一个有边界的本地运行时，而非通用自治 Agent 平台。

## 决策

采用 `research/runtime` 的 `models/ports/calls/storage/evidence/planning/tools/engine`。
组合根显式注册 `ResearchTask`，任务适配器调用所属领域服务；内核不 import gold、macro、
FactorLab、Promotion、OMS 或页面。局部命名空间 role 字符串映射到现有模型路由，
新增模块不修改全局业务角色 `Literal`。非金融文档任务夹具验证这一扩展方式。

保留单机单 API worker，一个活动根槽、最多两个子任务并行、根级最多三个模型并发。
一次计划最多6个节点，仅根/子两层，验证注册任务、请求 schema、类型化 InputRef、
显式依赖、无环与预算；禁止递归规划。规则规划不调用模型；模型规划可选、记账存档，
预览后须显式确认提交，变更已登记 MODEL 计划拒绝执行。

根、子任务、planner、格式修复和领域终审共用 `BudgetLedger/StructuredCaller`。
planner 历史仅结转一次，保留原始消息/响应与序号；子预算不可重置根预算。
调用前预留并持久化意图，结构响应最多修复一次；受管传输无隐式重试，
未知 usage 按保守预留结算。此机制不承诺美元硬上限或供应商 exactly-once。

复用 ArtifactStore/Registry，追加 `RESEARCH_RUN/EVENT/CALL/PLAN`。
事件每 run 连续编号，调用在 root 内有稳定唯一序号。字节/JSON 临时文件加硬链接原子发布，
不覆盖已有目标，也不等于文件/数据库事务。
启动将未完成运行记为 `INTERRUPTED`、未结束调用记为 `UNKNOWN`，不重放付费步骤；GET 不做恢复写入。
取消为协作式，检查后续模型/工具与发布；in-process 调用未退出不释放活动根槽。

证据校验内容 hash、schema、父血缘和知识截止；`FACT/DERIVED/OPINION` 不可随意改标。
LATEST 先采集再冻结，HISTORICAL 禁止当前采集，不能自动获得历史 PIT 认证。
工具默认 deny，校验类型化参数、任务/角色 allowlist、capability 和前后 deadline；
可信本地代码仍可执行任意 Python，因此这不是不可信插件沙箱或强制中断环境。

技术 `run.status`、领域 outcome、证据质量、publication 分离。
父级只确定性汇总子状态/摘要、报告引用及模块批准 hash，不新增金融建议模型门禁，
不能改写被拒绝建议发布或把模型意见当事实。root publication 不授予部署/晋级/交易权限。

## 领域兼容

- 黄金 `gold.collect` 后执行冻结快照 `gold.research`；宏观事实由组合根注入 reader，不直接 import 宏观领域。旧 facade 复用同一 Runtime，旧运行只读兼容，恢复写入显式执行；保留 V4 证据政策、隔离终审、OBSERVE 与不部署。
- 因子 API、测试及 CLI 复用共享调用机制；公共与旧 API 使用同一 Runtime，旧同步201结果不变。真实模式校验不可变快照内容并绑定实际 provider，fixture 明确标记；原 acceptance/Promotion 不变。
- 宏观独立建模、FRED/SAFE 采集解析与保守可用时间；`macro.collect` 无 LLM、`macro.analyze` 本地计算、`macro.research` 双独立视角。只描述证据，不提供投资建议。
- 公共 `/research` 提供模块/计划预览、任务树、真实事件、预算/收据、取消与历史；`/gold` 保留专属报告。公共路由独立定义并在 API 组合根注册，凭据就绪按任务计算。

当前规则联合计划为 `macro.collect`、`gold.collect`、`macro.analyze`、`gold.research` 四节点，
不是额外调用根级投资统筹。精确请求与扩展配方见 [research_modules.md](../research_modules.md)。
任务 YAML 仅用于显式 role binding、收紧预算及工具权限，不授权新增动态代码或工具。

## 结果与限制

获得可审计的有限跨模块运行与领域独立性，不引入 LangGraph、微服务、Redis、分布式队列、
插件市场、通用工作流 DSL 或自动 Promotion。代价是单根吞吐、协作取消及显式适配器维护。
通用内核落地不证明投资结论正确；旧 V4 的277项测试是历史记录，不是本次最终验收。

首批来源精确到 DGS10、DFII10、DTWEXBGS、DEXCHUS 与 SAFE 黄金数量/美元估值。
免费 FRED key 可选配置但未核实当前可用性，未做线上 FRED 或新付费模型 canary；
源站 SAFE HTML 样本不等于完整生产宏观报告。未完成 ALFRED 历史归档/自动 PIT 认证，
新闻、仓位及全球资金流未覆盖。最终验收待最终全量回归更新，进度以
[project_progress.md](../project_progress.md) 为唯一事实来源。