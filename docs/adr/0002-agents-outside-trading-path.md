# ADR-0002: Agents Outside the Trading Critical Path

**Status**: Accepted

## Context
系统未来会引入 Multi-Agent 量化研究，但 LLM 服务不可靠且不可审计。

## Decision
Agent 只存在于 Research Plane：通过受限 Tool Protocol 产出 artifact 与 DeploymentProposal。Trading Plane（策略运行时→风控→OMS→账本→对账）零 LLM 依赖。

## Consequences
- 所有 LLM 服务宕机时交易平面独立运行。
- 不存在 send_live_order / change_risk_limit / transfer_funds 工具；agent 无法激活部署或绕过风控。
- 任何 agent 复杂度必须通过 Eval 证明改善 performance/latency/cost/reliability 才允许保留。
