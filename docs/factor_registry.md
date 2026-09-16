# 因子注册表

因子注册表保存因子版本的身份、来源、验证指标与晋级历史。因子计算代码仍由代码仓库管理，
Dashboard 只登记版本和执行受控状态迁移，不提供在线代码编辑或执行入口。

## 生命周期

```text
DRAFT -> VALIDATED -> APPROVED -> ACTIVE
   |          |           |         |
   +----------+-----------+---------+-> DEPRECATED
```

- `DRAFT`：已登记，尚未通过 PIT、覆盖率和稳定性验证。
- `VALIDATED`：验证完成，等待人工审批。
- `APPROVED`：允许策略绑定，尚未启用。
- `ACTIVE`：当前允许被运行配置引用。
- `DEPRECATED`：停止新引用，历史血缘继续保留。

`ACTIVE` 可以退回 `APPROVED` 暂停使用；`DEPRECATED` 是终态。每次状态变化都保存操作人、
原因和时间，禁止直接修改数据库状态字段。

## 版本约束

- `(factor_key, version)` 全局唯一。
- 代码引用、参数、数据快照和验证指标随版本登记，不覆盖历史版本。
- 缺失指标显示为未知，不按零值参与统计。
- 当前实现提供注册和晋级控制面；策略绑定 `factor_id` 及只允许使用已批准版本的执行校验，
  应在后续策略包协议中接入。

## API

- `GET /api/factors`
- `POST /api/factors`
- `POST /api/factors/{factor_id}/transition`
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
不代表真实市场表现。自动发现、相关性去重、多重检验和 Factor Map 属于后续阶段。