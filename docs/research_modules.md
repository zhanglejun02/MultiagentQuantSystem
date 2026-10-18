# 模块化研究任务

2026-10-17：通用运行时、黄金/因子迁移、独立宏观模块、公共 API 与研究页面已落地。
本文记录任务契约与扩展方法；完成度、验收和剩余工作统一见
[project_progress.md](project_progress.md)，不在此维护另一套测试计数。

## 内核职责

| 拥有者 | 实际职责 |
|---|---|
| [models.py](../src/quant_system/research/runtime/models.py) | `TaskDescriptor/RunRequest/ResearchPlan/ResearchRun/TaskResult/EvidenceRef/Publication` |
| [ports.py](../src/quant_system/research/runtime/ports.py) | `ResearchTask` 与显式 `ModuleRegistry`；请求 schema、执行函数、可引用输出字段 |
| [calls.py](../src/quant_system/research/runtime/calls.py) | 唯一 `BudgetLedger/StructuredCaller`、并发预留、一次格式修复、稳定收据 |
| [storage.py](../src/quant_system/research/runtime/storage.py) | 不可变 run/event/call/plan、哈希与身份校验、幂等索引和恢复 |
| [evidence.py](../src/quant_system/research/runtime/evidence.py) | 内容/父链/时间校验、证据清单及 `FACT/DERIVED/OPINION` 分类 |
| [planning.py](../src/quant_system/research/runtime/planning.py) | 注册配方或模型计划提议、任务/schema/依赖/预算校验；无金融关键词分支 |
| [tools.py](../src/quant_system/research/runtime/tools.py) | 类型化工具、默认拒绝、任务/角色 allowlist 与 capability 检查 |
| [engine.py](../src/quant_system/research/runtime/engine.py) | 生命周期、单根槽、有限 DAG、统一截止、协作取消和确定性父汇总 |

依赖方向是组合根注册适配器，适配器调用领域服务和内核端口；内核不 import 黄金、宏观、
FactorLab、Promotion、OMS 或 UI。角色是任务局部字符串，例如 `macro.rates`、`macro.risks`，
由 `TaskDescriptor.roles` 显式映射到 provider/model 路由，不修改全局业务角色 `Literal`。

## 已注册任务

| 任务 | 输入与行为 | 角色 / 工具归属 |
|---|---|---|
| `gold.collect` | 重新采集并冻结黄金行情，输出 `snapshot_id`；不调用模型 | 黄金领域采集器；非任意网络工具 |
| `gold.research` | 必须给冻结 `snapshot_id`，可给 `macro_snapshot_id`；黄金独立审议与隔离终审 | 黄金六个既有逻辑角色；黄金领域决定证据充分性与建议发布 |
| `factor.research` | `data_mode=fixture` 或 `frozen_snapshot`；单DSL、本地FactorLab、双独立审查 | 既有四角色；Experiment/Promotion 仍是领域服务 |
| `macro.collect` | 精确序列与日期范围，冻结原始响应/规范化快照；不需 LLM 凭据 | `system` 仅可用 `macro.sources.collect`，capability 为 `macro.network.read` |
| `macro.analyze` | 指定宏观快照，本地计算指标、覆盖与缺口；不调用模型 | `system` 仅可用 `macro.snapshot.read` |
| `macro.research` | 同一冻结事实的利率/汇率视角与证据风险视角，独立并行，最后本地汇总 | `macro.rates`、`macro.risks`；模型无网络/工具和同伴观点，不提供投资建议 |
| `research.plan` | 根级模型计划预览；不执行提议节点，不可作为计划子任务 | `research.planner` 路由，未单独配置时兼容映射统筹路由 |

黄金通过组合根注入的 `MacroEvidenceReader` 消费宏观快照事实，不直接 import 宏观服务，
也不把 `macro.research` 的模型意见当作新事实。只有部分宏观值不代表补齐所有证据，
不能因此自动允许配置。黄金仍固定 `OBSERVE`、`deployment_eligible=false`。

因子真实模式不能只填写 `data_snapshot_id`：须解析不可变快照、校验内容 hash，
并把对应 provider 注入实际 FactorLab；fixture 模式明确标记为合成数据，不能混挂真实快照 ID。
原 acceptance 与 Factor/Strategy Promotion 不因运行时完成而改变。
`BoundFactorInput` 固定快照ID、hash、路径与新建冻结源，实际消费的行情/交易日/复权值按该源核验，
并在评估前后、报告前复查快照。兼容可信开发期截止和子集包装，不将其视为任意Python代码的沙箱。

## 一次有限计划

单机、单 API worker、一个活动根槽；最多两个子任务并行，根模型调用最多三个并发。
计划 `tasks` 为1至6个节点，仅根/子两层，禁止递归规划。节点只接受
`task_id/task_type/payload/inputs/depends_on/required/budget`；输入引用只含 `task_id/output_key`，
必须显式依赖已注册输出且类型兼容。不得塞入 Python、动态工具定义或嵌套计划。

下面是当前规则 planner 可接受的预览请求，发送到 `POST /api/research/plans`，不执行研究：

```json
{
  "question": "黄金宏观联合研究",
  "use_model": false,
  "payload": {
    "macro.collect": {"series_ids": ["CN_GOLD_RESERVES"], "start_date": "2026-01-01"},
    "gold.collect": {"symbol": "Au99.99"},
    "gold.research": {"symbol": "Au99.99"}
  }
}
```

结果为四个节点，不额外插入模型统筹或根金融终审：

```text
macro_collect: macro.collect ----> macro_analyze: macro.analyze
            |
            +---- macro_snapshot_id ----+
                                       v
gold_collect: gold.collect ------> gold_research: gold.research
                     snapshot_id
```

两个采集任务完成后冻结统一 `knowledge_cutoff`；`macro.analyze` 与 `gold.research`
都读取冻结事实，不互相读取首轮意见。规则路由只做本地匹配，没有模型费用。
可选 `use_model=true` 会调用并存储 planner，最多一次格式修复；先预览，
再显式把完整返回 `plan` 提交到 `POST /api/research/runs`。不是预览后自动执行。
登记后的 MODEL 计划必须原样匹配；更改内容会拒绝，不能借确认步骤扩大任务或预算。

根与子任务共享同一账本：planner 历史在执行根只结转一次，包含原始审计，后续序号续接。
子预算只能收紧，不能重置根额度；修复、作者修订、统筹和终审都计数。
受管传输没有不可见重试；未知 usage 按保守预留结算，不承诺美元限额。

父结果是确定性 `summaries/report_artifact_ids/approved_publication_hashes`，不是新模型意见。
required 技术失败令根失败，optional 失败保留 `PARTIAL`；领域拒绝可以是正常完成。
`run.status`、领域 `outcome`、证据质量、`Publication` 分开。
父级只能引用模块许可内容，不能把被拒绝建议重新措辞发布或将子观点冒充事实；
根 `Publication` 不提供交易、晋级或部署权限。

## API 与页面

| 入口 | 语义 |
|---|---|
| `GET /api/research/modules` | 显式任务目录、schema、局部角色与缺失凭据；不要求无关模块的密钥 |
| `POST /api/research/plans` | 规则/模型计划预览并存档，模型调用计预算 |
| `POST /api/research/runs` | 显式任务或完整计划，202；活动根冲突/同幂等键异参为409 |
| `GET /api/research/runs`、`/{run_id}` | 分页根历史与运行详情，含子任务 ID |
| `GET /api/research/runs/{run_id}/events`、`/calls` | 分页事件和调用收据；事件按各 run 编号，调用按 root 稳定排序 |
| `POST /api/research/runs/{run_id}/cancel` | 协作取消；尚在调用中不冒充完成或释放活动根槽 |

路由由 [api/research.py](../src/quant_system/api/research.py) 定义并在
[api/app.py](../src/quant_system/api/app.py) 注册。旧黄金提交202、因子同步201仍走同一 Runtime，
旧状态与结果 DTO 保持兼容，不重复 dispatch；旧报告按所属模块读取。
`/research` 已替换因子专属串行展示及伪进度，提供模块/计划预览、运行树、真实事件、预算、
收据、取消与历史；`/gold` 保留行情、手工配置与专属报告。前端不把 root 和 child 用量相加重复计费。

## 新模块配方

新增普通 Python 适配器及请求 schema，在 API/测试组合根显式注册；无需改内核 import、
全局角色枚举或加入领域 `if`。下面的非金融任务不需要模型、工具或交易服务：

```python
from pydantic import BaseModel, ConfigDict

from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.engine import RunContext
from quant_system.research.runtime.models import TaskDescriptor, TaskResult
from quant_system.research.runtime.ports import ModuleRegistry, ResearchTask


class InspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


def inspect_document(context: RunContext, request: BaseModel) -> TaskResult:
    typed = InspectRequest.model_validate(request.model_dump())
    context.advance("inspect")
    return TaskResult(outcome="INSPECTED", summary="Local inspection completed.",
                      payload={"length": len(typed.text)})


def register_documents(registry: ModuleRegistry) -> None:
    registry.register(ResearchTask(
        descriptor=TaskDescriptor(task_type="documents.inspect", module_id="documents",
                                  title="Inspect document",
                                  default_budget=BudgetLimits(max_calls=0, max_tokens=0)),
        request_type=InspectRequest,
        execute=inspect_document,
        output_fields={"length": int},
    ))
```

现有非金融注册测试见 [test_research_contracts.py](../tests/unit/test_research_contracts.py)；
[test_research_runtime.py](../tests/unit/test_research_runtime.py) 的 `runtime_for_test` 使用临时
store/registry 和一旦调用即报错的离线 gateway，验证幂等、重复 dispatch、恢复与协作取消。
这验证的是无金融依赖的扩展与运行语义，不是生产数据或真实模型质量。
需要模型时声明局部 role binding；需要工具时注册类型化 `ResearchTool` 并显式授权，默认无权限。
公共调用器本身也检查任务角色白名单，不能通过 `context.caller` 绕过任务授权。
任务 YAML 配置已由组合根加载，只用于角色映射、预算收紧与工具权限收窄。
可选多任务配方通过 `ResearchRecipe` 显式注册；金融关键词与黄金/宏观模板归
[recipes.py](../src/quant_system/research/modules/recipes.py)，内核不识别具体领域。
提交时保存已校验的 `execution_plan`，执行不重新路由；原始请求保持不变，供幂等与审计使用。

## 来源与边界

宏观领域独立维护精确来源：FRED `DGS10`（10年名义收益率，%）、`DFII10`（10年实际收益率，%）、
`DTWEXBGS`（广义贸易加权美元指数，不是 DXY）、`DEXCHUS`（纽约午间人民币/美元，不是中间价）；
SAFE `CN_GOLD_RESERVES`（万金衡盎司）与 `CN_GOLD_RESERVES_USD`（亿美元估值，不是购金量）。
FRED 可配置免费 `FRED_API_KEY`，缺失只影响对应来源；不宣称当前已有可用 key 或线上 FRED 验收。
默认请求含四个 FRED 序列与 SAFE 数量；采集器在数量请求中自动加入独立类型的美元估值，
不把两者混为购金量。来源细节见 [macro.md](macro.md)。

当前下载的 `available_at` 不早于实际抓取，观测期、发布时间、vintage 与入库时间分开；
ALFRED 历史版本归档/自动历史 PIT 认证未完成。SAFE 仅有源站 HTML 样本读取依据，
不据此宣称完整真实生产宏观报告已验收。新闻、仓位、ETF/全球资金流仍未覆盖。
工具是可信 in-process 代码，检查调用前后 deadline，不强制打断阻塞函数；
不是沙箱、分布式系统或外部副作用 exactly-once。启动只追加中断/未知记录，不自动重放。
新协议未进行付费真实模型 canary；本地完整验收与准确计数见项目进度页。