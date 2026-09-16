# ADR-0001: Modular Monolith over Microservices

**Status**: Accepted

## Context
个人量化系统，V1 为日频 A 股 paper trading。团队规模小，部署目标是单机。

## Decision
采用 modular monolith + ports/adapters：单进程，严格模块边界（domain 不依赖框架），外部依赖走 Protocol 端口。

## Consequences
- 零分布式运维成本；调试、测试、回放都在单进程内确定性完成。
- 模块边界（data / backtest / execution / research）即未来服务拆分候选，需要时可拆而不重写领域逻辑。
- 不引入 Kafka/Kubernetes/Ray/Redis；JobRunner 协议预留任务系统替换点，V1 只有 LocalJobRunner。
