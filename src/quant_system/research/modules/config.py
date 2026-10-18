"""Apply declarative policy to explicitly constructed research tasks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from quant_system.agents.config import AgentLLMConfig
from quant_system.domain.exceptions import QuantSystemError
from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.ports import ResearchTask

__all__ = ["TaskProfile", "load_task_profiles", "apply_task_profiles"]


class TaskProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(default=True, strict=True)
    roles: dict[str, str] | None = None
    budget: BudgetLimits | None = None
    tools: dict[str, list[str]] | None = None
    capabilities: list[str] | None = None
    independent_roles: list[tuple[str, str]] | None = None


class _TaskProfiles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: dict[str, TaskProfile]


def load_task_profiles(path: str | Path) -> dict[str, TaskProfile]:
    """Read safe YAML; an absent optional file leaves module defaults intact."""
    config_path = Path(path)
    try:
        content = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    raw = yaml.safe_load(content)
    if not isinstance(raw, dict):
        raise QuantSystemError(f"research task config must be a mapping: {config_path}")
    return _TaskProfiles.model_validate(raw).tasks


def _apply_profile(task: ResearchTask, profile: TaskProfile, config: AgentLLMConfig) -> ResearchTask:
    descriptor = task.descriptor
    name = descriptor.task_type
    roles = descriptor.roles if profile.roles is None else profile.roles
    if descriptor.roles and set(roles) != set(descriptor.roles):
        raise QuantSystemError(f"task profile {name} must retain the exact logical role keys")

    tools = descriptor.tools if profile.tools is None else profile.tools
    for role, allowed in tools.items():
        if role not in descriptor.tools or not set(allowed) <= set(descriptor.tools[role]):
            raise QuantSystemError(f"task profile {name} cannot grant unadvertised tools for {role}")
    capabilities = descriptor.capabilities if profile.capabilities is None else profile.capabilities
    if not set(capabilities) <= set(descriptor.capabilities):
        raise QuantSystemError(f"task profile {name} cannot grant unadvertised capabilities")

    budget = descriptor.default_budget
    if profile.budget is not None:
        overrides = profile.budget.model_dump(exclude_unset=True)
        for field, limit in overrides.items():
            if limit > getattr(budget, field):
                raise QuantSystemError(f"task profile {name} cannot increase budget.{field}")
        budget = BudgetLimits.model_validate({**budget.model_dump(), **overrides})

    pairs = profile.independent_roles or []
    for first, second in pairs:
        if first == second or first not in roles or second not in roles:
            raise QuantSystemError(f"task profile {name} requires distinct declared independent roles")

    reasons = []
    if not profile.enabled:
        reasons.append("disabled by task profile")
    if not descriptor.ready:
        reasons.append(descriptor.unavailable_reason or "module task is unavailable")
    missing = sorted({route for route in roles.values() if route not in config.roles})
    if missing:
        reasons.append(f"LLM role routes are not configured: {', '.join(missing)}")
    for first, second in pairs:
        first_route = config.roles.get(roles[first])
        second_route = config.roles.get(roles[second])
        if first_route is not None and second_route is not None and first_route.model == second_route.model:
            reasons.append(f"independent roles {first} and {second} require different model names")

    updated = descriptor.model_copy(
        update={
            "roles": roles,
            "tools": tools,
            "capabilities": capabilities,
            "default_budget": budget,
            "ready": not reasons,
            "unavailable_reason": "; ".join(reasons) if reasons else descriptor.unavailable_reason,
        },
    ).model_copy(deep=True)
    return replace(task, descriptor=updated)


def apply_task_profiles(
    tasks: list[ResearchTask],
    profiles: dict[str, TaskProfile],
    config: AgentLLMConfig,
) -> list[ResearchTask]:
    """Return policy-constrained copies before the composition root registers them."""
    unknown = sorted(set(profiles) - {task.descriptor.task_type for task in tasks})
    if unknown:
        raise QuantSystemError(f"task profiles reference unregistered tasks: {', '.join(unknown)}")
    return [
        _apply_profile(task, profiles.get(task.descriptor.task_type, TaskProfile()), config) for task in tasks
    ]
