# ADR-0008: Parquet + DuckDB for Research Data

**Status**: Accepted

## Context
行情/特征数据是批量、列式、追加型访问，体量远超元数据；放关系库既慢又贵。

## Decision
研究数据存 Parquet（PyArrow 读写），分析查询用 DuckDB 直查 Parquet。PIT 字段（available_at 等）随行存储。DataSnapshot artifact 通过 content_hash 锁定数据版本。

## Consequences
- 零服务运维，文件即数据集；快照可整目录哈希固化。
- LocalParquetProvider 供测试与 fixture；真实数据源以 Provider 适配器接入。
- 未来体量增长可平滑迁到对象存储 + 分区 Parquet，不改查询接口。
