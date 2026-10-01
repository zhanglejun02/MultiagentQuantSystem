"""Multi-provider LLM configuration for the research plane only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_system.domain.exceptions import QuantSystemError

ProviderKind = Literal["openai", "anthropic", "gemini", "openai_compatible"]
AgentRoleName = Literal["researcher", "critic", "statistician", "orchestrator"]


class LLMProviderConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ProviderKind
    api_key_env: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)

    def api_key(self) -> str:
        value = os.environ.get(self.api_key_env, "").strip()
        if not value:
            raise QuantSystemError(
                f"missing LLM credential: set environment variable {self.api_key_env}"
            )
        return value


class AgentRoleConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float = Field(default=0.1, ge=0, le=2)
    max_output_tokens: int = Field(default=4096, gt=0, le=100_000)
    thinking: Literal["enabled", "disabled"] | None = None


class ResearchBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_candidates_per_run: int = Field(default=100, gt=0)
    max_llm_calls_per_run: int = Field(default=30, gt=0)
    max_total_tokens_per_run: int = Field(default=200_000, gt=0)
    max_cost_usd_per_run: float = Field(default=20.0, gt=0)


class FactorValidationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_ic_days: int = Field(default=20, gt=0)
    min_coverage: float = Field(default=0.8, ge=0, le=1)
    min_abs_rank_ic: float = Field(default=0.01, ge=0, le=1)


class AgentLLMConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    providers: dict[str, LLMProviderConfig]
    roles: dict[AgentRoleName, AgentRoleConfig]
    research_budget: ResearchBudget = ResearchBudget()
    factor_validation: FactorValidationConfig = FactorValidationConfig()

    @model_validator(mode="after")
    def _known_providers(self) -> AgentLLMConfig:
        unknown = {
            role.provider for role in self.roles.values() if role.provider not in self.providers
        }
        if unknown:
            raise ValueError(f"roles reference unknown providers: {sorted(unknown)}")
        return self

    def route(self, role: AgentRoleName) -> tuple[LLMProviderConfig, AgentRoleConfig]:
        try:
            role_config = self.roles[role]
        except KeyError as exc:
            raise QuantSystemError(f"LLM role is not configured: {role}") from exc
        return self.providers[role_config.provider], role_config

    def credential_status(self) -> dict[str, bool]:
        return {
            name: bool(os.environ.get(provider.api_key_env, "").strip())
            for name, provider in self.providers.items()
        }


def load_agent_llm_config(path: str | Path) -> AgentLLMConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise QuantSystemError(f"agent LLM config not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text())
    if not isinstance(raw, dict):
        raise QuantSystemError(f"agent LLM config must be a mapping: {config_path}")
    return AgentLLMConfig.model_validate(raw)