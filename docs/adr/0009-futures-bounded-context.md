# ADR-0009: Futures as an Independent Bounded Context

## Status

Accepted — 2026-09-17

## Context

A 股现金账户是 long-only、T+1，并冻结现金或可卖股票。中国商品期货需要多空双向持仓、开平标志、今昨仓、合约乘数、保证金、平今费用和逐日盯市。复用股票订单或账本会使两套不变量互相污染。

## Decision

- `quant_system.futures` 独立拥有合约、订单、成交、持仓、账本、风控、事件和回放语义。
- 股票与期货只共享 Artifact、SQLAlchemy、FastAPI、Dashboard 和全局 KillSwitch 等基础设施。
- 仿真事件是权威状态；数据库快照与事件在同一事务提交，恢复时验证 SHA-256 hash chain。
- 核心代码仅依赖 `FuturesBrokerGateway` 和事件回调端口。未来 vn.py/CTP 位于 adapter 层，不能成为领域依赖。
- V0 只允许 `PAPER`，使用确定性日线 fixture。不存在 CTP 登录、真实账户或真实下单开关。

## Consequences

接入 SimNow 时需要实现 Gateway 映射和对账，但无需改变策略、风控、OMS 或账本。股票与期货可以独立演进和回放，代价是两套明确的交易领域模型。