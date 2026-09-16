# ADR-0005: Event-Driven Backtest as the Authoritative Engine

**Status**: Accepted

## Context
向量化回测快但难以正确表达 T+1、停牌、涨跌停、部分成交、资金冻结等 A 股微观约束。

## Decision
EventDrivenBacktestEngine 是唯一权威引擎：候选策略晋升必须通过它。全事件流（13 种类型化事件）写入 append-only EventStore，回测可由事件日志完整回放并逐日核账。VectorizedResearchEngine 仅预留接口，用于粗筛，结果不可用于晋升。

## Consequences
- 慢于向量化引擎，但正确性与可回放性优先。
- Decimal 记账 + 确定性种子 → 同输入同输出。
- 信号 T 收盘后生成、最早 T+1 成交，杜绝同 bar 未来成交。
