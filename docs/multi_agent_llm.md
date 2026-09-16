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
- DeepSeek：`critic` 使用推理模型审查候选，`statistician` 读取确定性评估结果。

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

当前完成的是模型供应商网关与角色路由。Researcher → Critic → Validator → Statistician
的持久化编排循环尚未自动启动；接入密钥后仍需实现该编排器，模型不会自动进入交易路径。