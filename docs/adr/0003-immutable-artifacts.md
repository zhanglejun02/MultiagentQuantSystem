# ADR-0003: Immutable Artifact Protocol

**Status**: Accepted

## Context
实验、模型、数据、策略、回测结果必须可复现、可追溯；可变记录会破坏审计链。

## Decision
所有产物封装为不可变 ArtifactEnvelope（ULID id、content_hash、code_commit、config_hash、parent_artifact_ids）。修改 = 创建带父血缘的新 artifact。元数据入 PostgreSQL，内容入可替换的 ArtifactStore（当前本地文件系统）。

## Consequences
- Registry 可回答 parents/children/lineage/find_by_*；血缘断裂是晋升硬失败。
- 同 (snapshot, commit, config, seed) 必产出相同结果，由确定性测试守护。
- 存储只增不改，需要定期归档策略（暂不实现）。
