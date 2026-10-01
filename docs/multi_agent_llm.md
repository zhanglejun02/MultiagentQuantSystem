# 多智能体 LLM 配置

多智能体位于 Research Plane。模型服务不可用时，PIT 数据、回测、风控、OMS、账本和
PaperBroker 仍然独立运行。

## API 密钥放置位置

不要把密钥写进 Python 或 YAML。复制根目录的示例文件：

```bash
cp .env.example .env
```

在 `.env` 中填写需要使用的供应商密钥：

```dotenv
MOONSHOT_API_KEY=...
DEEPSEEK_API_KEY=...
```

`.env` 已加入 `.gitignore`。启动 Agent worker 前将其加载到环境：

```bash
set -a
source .env
set +a
python scripts/check_llm_config.py
```

检查脚本不会发送网络请求，也不会打印密钥。

## 供应商和角色位置

编辑 `configs/agents/default.yaml`：

- `providers` 定义协议、API 地址和密钥环境变量名。
- `roles` 决定每个智能体使用哪个供应商和模型。
- `research_budget` 限制一次研究运行的候选数、调用数、Token 和费用。

可以让不同角色使用不同模型，例如：

```yaml
roles:
  researcher:
    provider: kimi
    model: kimi-k3
  critic:
    provider: deepseek
    model: deepseek-reasoner
  statistician:
    provider: deepseek
    model: deepseek-chat
  orchestrator:
    provider: kimi
    model: kimi-k3
```

当前默认分工：

- Kimi：`researcher` 负责提出候选，`orchestrator` 负责流程协调。
- DeepSeek：`critic` 审查候选，`statistician` 读取确定性评估结果。

默认模型是 Kimi `kimi-k3` 和 DeepSeek `deepseek-v4-pro`。四个角色均关闭 thinking，
以便稳定地产生短 JSON；模型名应以账户实际可用列表为准。

Kimi 使用官方 OpenAI-compatible 地址 `https://api.moonshot.cn/v1`，对应环境变量
`MOONSHOT_API_KEY`。DeepSeek 使用 `https://api.deepseek.com`，对应 `DEEPSEEK_API_KEY`。

模型名称只是初始配置，应按账户实际可用模型修改。也可以添加多个同类供应商；兼容
OpenAI Chat Completions 协议的服务使用 `kind: openai_compatible`。

## 代码调用入口

```python
from quant_system.agents.config import load_agent_llm_config
from quant_system.agents.llm import LLMGateway, LLMMessage

config = load_agent_llm_config("configs/agents/default.yaml")
gateway = LLMGateway(config)
response = gateway.complete(
    "researcher",
    [
        LLMMessage(role="system", content="Only propose PIT-safe factor DSL."),
        LLMMessage(role="user", content="Propose a low-turnover price-volume factor."),
    ],
)
print(response.content)
```

## 自动研究闭环

Research 页面可以启动完整的持久化研究流程：

1. Researcher 提出一个受限 DSL 因子；本地解析器拒绝非法字段、函数、参数和未来数据表达式，
   并允许一次定向修复。
2. Critic 审查 PIT 安全、经济解释、DSL 风险和过拟合风险。
3. 本地 FactorLab 使用确定性数据计算 IC、覆盖率、换手和多空组合指标。
4. Statistician 只解释 FactorLab 已产生的指标，不生成或修改指标。
5. Orchestrator 汇总审查与本地门槛，但不能覆盖任一硬门槛。
6. 结果、调用轨迹、实验运行和 `RESEARCH_REPORT` Artifact 一并持久化。

模型输出必须通过 Pydantic JSON Schema。格式不符合时，同一角色最多进行一次格式修复；两次调用
都会计入调用数和 Token 预算。研究结论可能是 `CANDIDATE` 或 `REJECTED`。`REJECTED` 表示闭环
正常完成但证据不足，并非系统错误。

启动 API 前加载密钥：

```bash
set -a && source .env && set +a
QUANT_DB_URL="sqlite:///$PWD/var/quant.db" \
  uvicorn quant_system.api.app:factory --factory --port 8000
```

随后打开 Dashboard 的 `/research` 页面，确认模型状态为“已就绪”，填写研究主题和时间范围，
点击“启动研究闭环”。也可调用 `POST /api/agent-research/runs`。

LLM 网关使用系统 libcurl 的 Python 绑定并显式加载 `certifi` CA，连接错误最多重试三次。
错误信息和调用轨迹不会记录 API Key。

## 安全边界

整个闭环只属于 Research Plane。Agent 没有下单、修改风控、转账或激活部署的工具；LLM 服务
全部不可用时，回测、风控、OMS、账本和 Paper Gateway 仍可独立运行。