# ADR-0004: Point-in-Time-First Data Layer

**Status**: Accepted

## Context
未来函数与幸存者偏差是回测虚假收益的最大来源。

## Decision
每条记录携带 event_time / effective_time / available_at / ingested_at。所有历史查询强制 `as_of`，`available_at > as_of` 的数据不可见。Universe（上市/退市/ST/停牌/板块）同样按区间 PIT 化。执行用未复权价，复权因子仅供特征且 PIT 化。

## Consequences
- Provider（AKShare/Tushare/Qlib 等）只能以适配器接入，必须补齐 PIT 字段。
- PIT 违规是晋升硬失败；`tests/unit/test_pit.py` 守护查询正确性。
- 查询略慢于裸表访问，可接受。
