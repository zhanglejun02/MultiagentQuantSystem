# 冻结真实历史基线研究

## 目标与边界

用冻结的真实 A 股历史完成一个固定参数策略的研究、时间隔离验证和基准比较。收益不好也属于完成；报告不是 `VALIDATION_REPORT`，不会触发因子或策略晋级。

协议位于 [configs/research/a_share_baseline.yaml](../configs/research/a_share_baseline.yaml)。V1 固定 8 只沪市主板股票，20 日复权动量 Top-3，每个 ISO 周第一个交易日收盘决策，下一交易日开盘成交，目标仓位 90%。等权对照使用相同股票池、调仓日、撮合与成本。

开发期为 2025 年一季度，验证期为二季度，留出期为下半年。参数没有依据分区收益搜索或调整。每段从相同现金独立启动，信号可用此前历史预热；这是时间隔离的历史检验，不是前瞻实盘验证，也不是三个区间拼接的连续账户收益。

## 代码与数据布局

```text
configs/research/a_share_baseline.yaml   固定实验协议与限制
scripts/run_baseline_research.py         prepare / run 薄入口
src/quant_system/research/baseline/
  spec.py                               协议校验
  runner.py                             离线研究与复现验收
  report.py                             Markdown 报告视图
src/quant_system/data/ingestion/
  baseline.py                           原始缓存与原子归一化发布
  eastmoney.py                          公开主数据与现金分红适配器
  tencent.py                            公开沪深300价格指数适配器
src/quant_system/strategies/weekly_momentum.py
var/research/a_share_baseline/
  raw/                                  请求寻址缓存，不含凭据
  prepared/                             协议寻址准备区，不直接用于回测
  registry.db                           独立实验与 Artifact 元数据
  artifacts/data_snapshots/<sha256>/     实际消费的冻结数据与原始响应
  artifacts/backtests/<run_id>/          事件、订单、成交、净值与指标
  artifacts/research/baseline/plans/     运行前登记的协议
  artifacts/research/baseline/reports/   内容寻址 JSON + Markdown 报告
```

源码、测试和协议进入 Git；真实数据与运行产物保留在已忽略的 `var/`。备份时备份整个研究工作区，尤其是 `registry.db` 和 `artifacts/`；不要只复制报告或只保存快照 ID。路径 URI 是本机绝对路径，跨机器搬迁需要重新登记，不应直接修改旧血缘。

## 运行

在仓库根目录执行。联网准备需要本地 `TUSHARE_TOKEN`，只请求固定股票池，不调用 LLM：

```bash
set -a && source .env && set +a
.venv/bin/python scripts/run_baseline_research.py prepare
```

成功输出 `snapshot_id`、数据哈希和冻结 URI。相同协议重复准备会校验并复用已有冻结快照，不自动刷新历史数据。需要新数据版本时显式使用新的 `--workspace`，旧实验不会被覆盖。

离线研究必须显式指定准备阶段输出的 ID，不从“最新数据”隐式取数：

```bash
.venv/bin/python scripts/run_baseline_research.py run --snapshot-id <snapshot_id>
```

`--config` 可选择另一份预先固定的协议，`--workspace` 可隔离另一组实验。准备数据绑定协议哈希，修改股票池、区间或参数后不能复用原协议的数据集冒充同一次实验。

## 数据语义

- Tushare 未复权日线用于成交和账本，成交量从手转换为股，成交额从千元转换为元。
- 当前账户的 `index_daily`、`stock_basic`、`adj_factor` 存在小时级限频，因此协议显式使用腾讯沪深300日线、东方财富当前公司资料及实施分红记录，不进行隐式来源切换。
- 现金行动模式不使用下载时的前复权价格。因子从 1 起逐日累乘 `previous_close / (previous_close - cash)`，只影响除权日及之后；所有信号数据强制过滤 `available_at`。
- 现金分红在除权日入账。日线 `pre_close` 可能已经除权，归一化恢复原始前收盘，交由规则层扣除一次分红；测试防止重复扣减。
- 小幅方案/实施金额差异只在已有现金分红记录时对账，差异同时不超过每股 0.10 元及 1%。采用日线参考价隐含实施金额，原金额、采用金额与日期进入审计记录；较大差异、股票送转、无法解释的除权差异拒绝发布。
- 基准使用每段首日前收盘作为收益锚点，检查每个交易日完整覆盖，缺失或未知基准不再被静默忽略。
- 所需原始响应随冻结数据一起保存；准备目录通过临时目录原子发布，写入失败不留可复用半成品。

## 验收与限制

每段运行动量、等权和双倍手续费/滑点压力场景，共 9 次主回测；动量与等权各重复一次，共 15 次回测。每次验证事件最终哈希和账本回放，重复比较净值、指标和去除运行 ID 后的经济成交。唯一运行 ID 不要求相同。

报告记录数据/协议/代码内容哈希、Git commit、主要依赖版本、Artifact 血缘、成本、基准收益、主动收益跟踪误差和规则拦截。CLI 将实际源码、市场规则、依赖锁文件和协议归档在报告目录的 `code/`；未提交修改也由代码内容哈希及这份归档标识。只检出报告的 Git commit 并不足以恢复本次修改后的源代码。

固定股票池是今天选取的历史样本，存在选择与幸存者偏差；历史 ST 未获完整认证。缺失日线推断停牌、历史可见时间是假设而非原始发布存档。现金分红按除权日税前入账而非实际支付日，且沪深300是价格指数而非全收益指数。因此比较不是可部署 alpha 结论。完整历史 Universe、公司行动支付日/税务和正式验证作业属于后续工作，不由本报告冒充完成。