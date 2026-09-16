# ADR-0006: MarketRuleBook Instead of Hardcoded A-Share Rules

**Status**: Accepted

## Context
A 股规则随交易所、板块、证券状态、日期变化（主板 ±10%、创业板/科创板 ±20%、ST ±5%、北交所 ±30%、新股首日无涨跌幅等），散落在引擎里会腐烂。

## Decision
MarketRuleBook 协议：`get_rules(exchange, board, security_type, symbol, timestamp) -> TradingRules`。规则定义在 `configs/markets/cn_a.yaml`，带生效日期。引擎只消费 TradingRules，不感知具体规则来源。

## Consequences
- 规则更新改配置不改引擎；费用/税率同样版本化配置。
- 不声称法规完备，覆盖以测试为准（涨跌停/ST/停牌/T+1/手数/tick size 等）。
- 新市场 = 新 RuleBook 适配器。
