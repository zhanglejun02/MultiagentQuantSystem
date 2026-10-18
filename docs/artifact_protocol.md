# Artifact Protocol

## 不变性

Artifact 一经注册即不可变（`ArtifactEnvelope` 为 frozen pydantic model；Registry 拒绝重复 `artifact_id` 写入）。任何"修改"都是创建一个新 artifact，并通过 `parent_artifact_ids` 指向前驱。

## Envelope 字段

| 字段 | 说明 |
|---|---|
| `artifact_id` | ULID 风格（时间有序、全局唯一），Envelope 默认前缀为 `art_`，不与业务 run ID 混用 |
| `artifact_type` | 已注册的 `ArtifactType`，不以固定类型数量作为协议 |
| `schema_version` | envelope 结构版本 |
| `created_at` | UTC，timezone-aware |
| `producer` | 产生者（engine/service/agent 标识） |
| `parent_artifact_ids` | 血缘上游 |
| `data_snapshot_id` | 可选，只指实际原始 `DATA_SNAPSHOT`；规范化黄金/宏观快照放在父引用或证据列表中 |
| `code_commit` | 产生时的 git commit |
| `config_hash` | 配置的稳定哈希（排序 JSON → SHA-256） |
| `content_hash` | 内容文件的 SHA-256 |
| `uri` | 内容位置（当前为本地文件系统路径） |
| `metadata` | 类型相关的附加元数据 |
| `status` | REGISTERED / DEPRECATED 等 |

## Artifact 类型

`DataSnapshot` `Universe` `FeatureSet` `Model` `Signal` `PortfolioTarget`
`BacktestSpec` `BacktestResult` `ValidationReport` `StrategyPackage`
`Deployment` `ResearchReport` `SignalSpec` `PortfolioConstructionSpec` `StrategySpec`

研究仓储实际写入 `RESEARCH_RUN`、`RESEARCH_EVENT`、`RESEARCH_CALL`、`RESEARCH_PLAN`；
枚举另有 `RESEARCH_EVIDENCE`，当前 manifest 在运行 `usage.evidence_manifest` 中保存，
不宣称它已单独写成该类型的 Artifact。领域保留黄金/宏观快照及报告类型。完整枚举以
[domain/enums.py](../src/quant_system/domain/enums.py) 为准，不为新增类型改写旧 Artifact。

策略链路要求 `StrategyPackage` 的直接父节点同时包含 `SignalSpec` 与
`PortfolioConstructionSpec`。订单可沿 StrategyPackage、SignalSpec、Factor、Experiment、
DatasetSnapshot 和 code commit 追溯；缺失该血缘时部署晋级 fail closed。

## 服务

- **ArtifactStore**：内容存取，写入时计算 `content_hash`，读取时可校验。本地文件系统实现；接口可替换 S3/MinIO。
- **ArtifactRegistry**：元数据持久化（PostgreSQL/SQLite），支持 `parents()` / `children()` / `lineage()` / `find_by_type()` / `find_by_experiment()`。
- **ArtifactLineageService**：递归血缘遍历，检测断裂血缘（晋升硬门槛之一）。

本地 `put_bytes` / `put_json` 及内容寻址 JSON 使用临时文件、同步落盘与硬链接原子发布，
不覆盖已有目标；同内容寻址对象复用前核验 hash。
这不是文件系统与 Registry 数据库的跨资源事务，也不承诺目录写入、分布式或付费请求的 exactly-once。
只有成功注册的 Artifact 才是仓储提交事实；孤立文件不是成功运行记录。

## 通用研究记录（2026-10-17）

契约分别由 [runtime/models.py](../src/quant_system/research/runtime/models.py)、
[runtime/calls.py](../src/quant_system/research/runtime/calls.py) 与
[runtime/storage.py](../src/quant_system/research/runtime/storage.py) 定义。

| 类型 | 内容与索引语义 |
|---|---|
| `RESEARCH_RUN` | `ResearchRun` 加规范化 `request_hash`；含根/父/子关系、任务与结果 schema 版本、状态、结果、用量和递增 `revision`；新 revision 指向前一版本 |
| `RESEARCH_EVENT` | `ResearchEvent`：`run_id/sequence/event_type/stage/at/data`；每个 run 自己连续编号并链接前一事件，不把整个任务树伪装成一条事件序列 |
| `RESEARCH_CALL` | `CallReceipt` 的不可变版本；以所属 run、稳定 `call_id` 和 revision 索引；调用序号在同一 root 内跨子任务唯一稳定，终态收据不得覆写 |
| `RESEARCH_PLAN` | `research-plan-v1` 计划及 planner 收据；绑定计划身份与内容，保留模型规划的预算与审计，不只存一份可修改的任务清单 |

`COMPLETED` 仅表示技术执行完成。领域 `outcome` 与 `Publication` 独立：
`PASS` 必须绑定获准内容的 hash；`WITHHELD` / `NOT_APPLICABLE` 不等于允许部署或下单。
根报告只能汇总模块许可的内容，不得重新包装被拒绝的投资意见。

仓储加载会校验内容 hash、元数据身份、父链、revision、事件连续性与调用序号冲突。
启动时显式恢复：未结束调用的 `STARTED` 收据追加为 `UNKNOWN`，`QUEUED/RUNNING`
运行追加为 `INTERRUPTED`，不自动重发模型或继续执行。普通 GET 不触发恢复写入。
请求幂等键绑定去掉幂等键后的规范化请求 hash；同键异参拒绝，不能当作重放付费步骤的机制。

## 证据与领域兼容

`EvidenceRef` 保存 `namespace/artifact_id/content_hash/schema_version/record_key/available_at`
及 `FACT/DERIVED/OPINION` 分类。大序列与原文保存在 Artifact，模型只收到授权上下文；
观点不能变成另一个独立首轮的事实。证据必须满足知识截止，计算时间不能冒充源发布时间。

黄金继续保留 `GOLD_RESEARCH_REPORT`；旧 `GOLD_RESEARCH_RUN` 仅用于历史兼容读取，
新任务统一写通用运行记录。旧报告不补造新协议、审计或证据认证。
多源任务用父引用和证据清单表达血缘，不能只填一个 snapshot ID 就宣称实际消费并验证了数据。

## 复现契约

同一 `(data_snapshot_id, code_commit, config_hash, random_seed)` 的回测必须产出相同的核心输出（equity、trades、metrics）。这由确定性测试（`tests/backtest/test_determinism.py`）守护。
