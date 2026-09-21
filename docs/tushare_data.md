# Tushare Pro 日线数据

## 选择什么外部数据库

个人阶段不需要购买或运维一个“外部行情数据库”。推荐组合是：

- **Tushare Pro**：外部数据 API，负责提供 A 股行情与基础资料。
- **Parquet + DuckDB**：本地行情仓库，回测从本地快照读取，不在运行时请求 Tushare。
- **SQLite**：本地开发时保存实验、Artifact 和运行状态。
- **PostgreSQL**：部署或多进程并发后替代 SQLite，不用于存海量日线行情。

这能避免 API 网络波动影响回测，也能用 DataSnapshot 固定每次实验的数据版本。

## 注册和权限

1. 在 <https://tushare.pro/register> 注册账号。
2. 登录后在个人中心取得 Token。
3. 查看权限中心与积分。当前官方文档显示 `stock_basic` 和 `trade_cal` 需要 **2000 积分**；
   权限规则可能调整，应以账户权限页为准。
4. 在本地 `.env` 添加：

   ```dotenv
   TUSHARE_TOKEN=你的_token
   ```

不要把 Token 写入 Python、YAML 或 `.env.example`，也不要提交 `.env`。

## 首次同步

先用一个交易日做连通性和权限检查：

```bash
set -a
source .env
set +a
python scripts/sync_tushare_daily.py --start 2024-01-02 --end 2024-01-02
```

成功后再扩大区间：

```bash
python scripts/sync_tushare_daily.py --start 2020-01-01 --end 2024-12-31
```

默认输出目录是 `var/data/tushare/cn_a_daily/`。再次同步会按 `symbol + trade_date`
增量合并和覆盖，不产生重复日线；`--overwrite` 会替换原有日线。

同步器调用官方 HTTPS REST API，不依赖 pandas 或 Tushare SDK。它按交易日拉取全市场数据，
并执行以下归一化：

- `.SH` → `.SSE`，`.SZ` → `.SZSE`。
- `vol` 从手转换为股（乘 100）。
- `amount` 从千元转换为人民币元（乘 1000）。
- 每根日线 `available_at` 为交易日 17:00（Asia/Shanghai）。
- 上市和退市信息保存为可按 `as_of` 选择的主数据版本。

## 使用真实日线回测

同步完成后显式指定数据目录和回测区间：

```bash
QUANT_DATA_DIR="$PWD/var/data/tushare/cn_a_daily" \
QUANT_BACKTEST_START=2024-01-02 \
QUANT_BACKTEST_END=2024-06-28 \
python scripts/run_sample_backtest.py
```

脚本会先创建不可变 DataSnapshot，再运行现有事件驱动回测。未设置 `QUANT_DATA_DIR` 时仍使用
合成 fixture，避免混淆两类结果。

## V0 能力边界

当前同步器是可靠日线接入的第一步，不是完整可投资数据集：

- 日线是**未复权价格**。
- 默认只纳入上交所和深交所；北交所待市场规则配置完整后接入。
- 尚未同步历史 ST 区间和每日停复牌状态。
- 尚未同步复权因子、分红送转等公司行动。
- `stock_basic` 只能构建上市/退市的保守版本，不能替代完整历史证券主数据。

因此可用于验证真实数据管线和初步价格策略，但在补齐复权因子、历史 ST/停牌和公司行动前，
不应将长期收益结果用于策略晋级或实盘判断。