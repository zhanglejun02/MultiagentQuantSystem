# 因子注册表

因子注册表保存因子版本的身份、来源、验证指标与晋级历史。因子计算代码仍由代码仓库管理，
Dashboard 只登记版本和执行受控状态迁移，不提供在线代码编辑或执行入口。

## 生命周期

```text
PROPOSED -> SCREENED -> CANDIDATE -> VALIDATING -> VALIDATED -> STRATEGY_ELIGIBLE
  |            |            |             |            |              |
  +------------+------------+-------------+------------+--------------+-> DEPRECATED
```

- `PROPOSED`：已登记，尚未通过确定性筛选。
- `SCREENED`：PIT 与基础质量检查通过。
- `CANDIDATE`：OOS、多重检验与相关性去重通过，仍不可用于策略。
- `VALIDATING`：进入完整验证流水线。
- `VALIDATED`：walk-forward、purged CV/embargo、中性化、成本、容量和稳健性通过。
- `STRATEGY_ELIGIBLE`：final holdout 通过，允许 StrategyPackage 引用。
- `DEPRECATED`：停止新引用，历史血缘继续保留。

正向晋级只能由 `FactorPromotionService` 读取已注册且状态为 `VERIFIED` 的
`VALIDATION_REPORT` 后执行，报告必须绑定目标 `factor_id`。Dashboard、Agent 和通用
registry API 均无正向晋级权限；人工只可退役。每次状态变化都保存操作人、原因和时间。

final holdout 对 Agent 不可见，只由隔离的 validation job 运行并向 promotion policy
提供 PASS/FAIL。不得把 holdout 明细或指标放入 Agent 上下文。

## 版本约束

- `(factor_key, version)` 全局唯一。
- 代码引用、参数、数据快照和验证指标随版本登记，不覆盖历史版本。
- 缺失指标显示为未知，不按零值参与统计。
- Strategy Registry 强制策略包引用 `STRATEGY_ELIGIBLE` 因子，并要求
  `SignalSpec -> PortfolioConstructionSpec -> StrategyPackage` 血缘完整。

## API

- `GET /api/factors`
- `POST /api/factors`
- `POST /api/factors/{factor_id}/transition`
- `POST /api/factors/{factor_id}/promote`
- `GET /api/factors/{factor_id}/history`
- `GET /api/factor-runs`
- `POST /api/factor-runs/evaluate`

## Alpha Lab V0

Factor Explorer 使用受限 DSL，并在每个决策日通过 `available_at <= as_of` 的数据视图计算
横截面分数。当前支持：

- 字段：`open`、`high`、`low`、`close`、`volume`、`amount`、`return_1d`
- 算子：`rank`、`delta`、`delay`、`ts_mean`、`ts_std`、`abs` 和基础四则运算
- 报告：Rank IC、ICIR、IC 胜率、覆盖率、五分位收益、L/S 指标、换手、衰减与年度稳定性

DSL 不允许属性访问、导入、下标、Lambda 或任意函数调用。V0 使用本地合成 A 股数据，
不代表真实市场表现。完整验证门槛已定义，但 OOS/walk-forward/purged CV 等批处理作业仍需
接入；在这些作业产出可信 `VALIDATION_REPORT` 前，因子无法到达 `STRATEGY_ELIGIBLE`。