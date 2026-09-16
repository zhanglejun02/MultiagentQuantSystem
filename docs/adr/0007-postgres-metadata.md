# ADR-0007: PostgreSQL for Metadata and Trading State

**Status**: Accepted

## Context
Artifact 元数据、实验状态机、订单/成交需要事务一致性与并发安全；研究数据则是批量列式访问。

## Decision
PostgreSQL（Docker Compose 提供）存元数据/实验/交易状态，SQLAlchemy + Alembic 管理。开发与测试可降级 SQLite（同一套 ORM），环境变量 `QUANT_DATABASE_URL` 切换。

## Consequences
- 状态变更有事务保证；实验状态机的非法迁移在 DB 之前即被服务层拒绝。
- 不用 Postgres 存行情大表 —— 那是 Parquet/DuckDB 的职责（ADR-0008）。
