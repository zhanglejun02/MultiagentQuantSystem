from __future__ import annotations

import pytest
from pydantic import ValidationError

from quant_system.agents.config import AgentLLMConfig, AgentRoleName, load_agent_llm_config
from quant_system.domain.exceptions import QuantSystemError


def test_default_config_routes_roles_to_different_providers(monkeypatch):
    config = load_agent_llm_config("configs/agents/default.yaml")

    researcher_provider, researcher = config.route("researcher")
    critic_provider, critic = config.route("critic")
    statistician_provider, statistician = config.route("statistician")
    orchestrator_provider, orchestrator = config.route("orchestrator")
    auditor_provider, auditor = config.route("final_auditor")
    assert researcher_provider.kind == "openai_compatible"
    assert researcher_provider.api_key_env == "MOONSHOT_API_KEY"
    assert researcher.model == "kimi-k3"
    assert researcher.temperature == 0.6
    assert researcher.thinking == "disabled"
    assert critic_provider.kind == "openai_compatible"
    assert critic.model == "deepseek-v4-pro"
    assert critic.thinking == "disabled"
    assert statistician_provider == critic_provider
    assert statistician.model == "deepseek-v4-pro"
    assert statistician.thinking == "disabled"
    assert orchestrator_provider == researcher_provider
    assert orchestrator.model == "kimi-k3"
    assert orchestrator.temperature == 0.6
    assert orchestrator.thinking == "disabled"
    assert auditor_provider == critic_provider
    assert auditor.model == "deepseek-v4-pro"
    assert auditor.model != orchestrator.model
    assert auditor.temperature == 0.0
    technical_provider, technical = config.route("technical_analyst")
    assert technical_provider == researcher_provider
    assert technical.model == orchestrator.model
    for role in ("macro_analyst", "quant_analyst", "risk_analyst"):
        provider, analyst = config.route(role)
        assert provider == critic_provider
        assert analyst.model == critic.model
        assert analyst.thinking == "disabled"

    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    with pytest.raises(QuantSystemError, match="MOONSHOT_API_KEY"):
        researcher_provider.api_key()
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    assert researcher_provider.api_key() == "test-key"


def test_config_rejects_role_with_unknown_provider():
    with pytest.raises(ValidationError, match="unknown providers"):
        AgentLLMConfig.model_validate(
            {
                "providers": {
                    "one": {
                        "kind": "openai",
                        "api_key_env": "ONE_API_KEY",
                        "base_url": "https://example.invalid",
                    }
                },
                "roles": {
                    "researcher": {"provider": "missing", "model": "model-a"},
                },
            }
        )


def test_config_accepts_namespaced_roles_and_keeps_factor_validation():
    original = load_agent_llm_config("configs/agents/default.yaml")
    payload = original.model_dump()
    payload["roles"]["macro.rates-review_v2"] = payload["roles"]["researcher"].copy()
    config = AgentLLMConfig.model_validate(payload)

    assert AgentRoleName is str
    assert config.route("macro.rates-review_v2") == config.route("researcher")
    assert config.factor_validation == original.factor_validation


@pytest.mark.parametrize("name", ["", "Researcher", "1researcher", "macro/rates", "macro rates", " x"])
def test_config_rejects_invalid_role_ids(name):
    payload = load_agent_llm_config("configs/agents/default.yaml").model_dump()
    payload["roles"] = {name: payload["roles"]["researcher"]}

    with pytest.raises(ValidationError, match="invalid role ID"):
        AgentLLMConfig.model_validate(payload)
