# Artifact Protocol

## 不变性

Artifact 一经注册即不可变（`ArtifactEnvelope` 为 frozen pydantic model；Registry 拒绝重复 `artifact_id` 写入）。任何"修改"都是创建一个新 artifact，并通过 `parent_artifact_ids` 指向前驱。

## Envelope 字段

| 字段 | 说明 |
|---|---|
| `artifact_id` | ULID 风格（时间有序、全局唯一），带类型前缀（如 `bt_`、`ds_`） |
| `artifact_type` | 12 种类型之一（见下） |
| `schema_version` | envelope 结构版本 |
| `created_at` | UTC，timezone-aware |
| `producer` | 产生者（engine/service/agent 标识） |
| `parent_artifact_ids` | 血缘上游 |
| `data_snapshot_id` | 可选，绑定数据快照 |
| `code_commit` | 产生时的 git commit |
| `config_hash` | 配置的稳定哈希（排序 JSON → SHA-256） |
| `content_hash` | 内容文件的 SHA-256 |
| `uri` | 内容位置（当前为本地文件系统路径） |
| `metadata` | 类型相关的附加元数据 |
| `status` | REGISTERED / DEPRECATED 等 |

## Artifact 类型

`DataSnapshot` `Universe` `FeatureSet` `Model` `Signal` `PortfolioTarget`
`BacktestSpec` `BacktestResult` `ValidationReport` `StrategyPackage`
`Deployment` `ResearchReport`

## 服务

- **ArtifactStore**：内容存取，写入时计算 `content_hash`，读取时可校验。本地文件系统实现；接口可替换 S3/MinIO。
- **ArtifactRegistry**：元数据持久化（PostgreSQL/SQLite），支持 `parents()` / `children()` / `lineage()` / `find_by_type()` / `find_by_experiment()`。
- **ArtifactLineageService**：递归血缘遍历，检测断裂血缘（晋升硬门槛之一）。

## 复现契约

同一 `(data_snapshot_id, code_commit, config_hash, random_seed)` 的回测必须产出相同的核心输出（equity、trades、metrics）。这由确定性测试（`tests/backtest/test_determinism.py`）守护。
