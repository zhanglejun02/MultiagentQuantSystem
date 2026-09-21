# 期货仿真

## V0 范围

期货板块提供本地日线 PAPER 仿真和确定性回测，首批 fixture 产品为螺纹钢、豆粕和甲醇。合约参数位于 `configs/futures/contracts.yaml`，行情由 `FixtureFuturesData` 生成。

这些数据、费率、保证金率和合约规则仅用于工程验证，不代表交易所或期货公司的当前参数，不可用于实盘决策。

## 运行

```bash
alembic upgrade head
python scripts/run_sample_futures_backtest.py

QUANT_DB_URL="sqlite:///$PWD/var/quant.db" \
  uvicorn quant_system.api.app:factory --factory --port 8000
```

Dashboard 的“期货仿真”页面可以创建账户、提交多空及开平委托、撤单、推进交易日，并查看保证金、今昨仓、成交、结算和回测结果。所有写入口固定为 `PAPER`。

## 核心语义

- `Direction` 表示被操作的持仓侧：`LONG + OPEN` 开多，`LONG + CLOSE_*` 平多；空头同理。
- `CLOSE_TODAY` 与 `CLOSE_YESTERDAY` 严格隔离；`CLOSE` 按今仓优先消费，仅用于不要求指定仓位来源的仿真规则。
- 开仓按合约乘数和方向保证金率占用保证金，手续费立即从余额扣除。
- 每日使用 settlement 划转浮动盈亏，重置持仓结算基准并将今仓转为昨仓。
- 订单在提交和成交前各执行一次风控。保证金预估不足、无可平仓、价格偏离、KillSwitch 或 MARGIN_CALL 都会 fail closed。
- 事件记录使用 canonical JSON 和 SHA-256 链。加载账户时同时验证事件链、事件头和最新状态快照。

## vn.py / CTP 适配边界

未来 `VnpyCtpGateway` 实现 `FuturesBrokerGateway`，负责以下映射：

| vn.py 数据 | 核心类型 |
|---|---|
| `ContractData` | `FuturesContract` |
| `BarData` / `TickData` | 行情 Provider 事件 |
| `OrderData` | `FuturesOrder` 状态更新 |
| `TradeData` | `FuturesTrade` |
| `AccountData` | 经纪商账户快照 |
| `PositionData` | 按方向和今昨仓的持仓快照 |

Adapter 必须实现连接状态、订阅、下单、撤单、账户/持仓/订单/成交查询和异步回调。连接 SimNow 前还必须完成：交易所日历、夜盘交易日映射、合约参数同步、平今费用核对、网关断线 SAFE_MODE、启动对账和人工 KillSwitch 演练。

账号、密码、Broker ID、App ID 和 Auth Code 只能来自本地环境变量或密钥管理服务，不得写入 YAML、日志、Artifact 或事件。