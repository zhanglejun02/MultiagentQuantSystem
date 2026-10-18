from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from quant_system.agents.config import AgentLLMConfig, LLMProviderConfig, load_agent_llm_config
from quant_system.agents.orchestration import AgentResearchOrchestrator
from quant_system.domain.exceptions import QuantSystemError
from quant_system.gold.service import GoldService
from quant_system.macro.service import MacroService
from quant_system.research.modules.config import TaskProfile, apply_task_profiles, load_task_profiles
from quant_system.research.modules.factor import FactorTaskRequest, factor_tasks
from quant_system.research.modules.gold import gold_tasks
from quant_system.research.modules.macro import macro_tasks
from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.engine import RunContext
from quant_system.research.runtime.models import TaskDescriptor, TaskResult
from quant_system.research.runtime.ports import ModuleRegistry, ResearchTask


class Request(BaseModel):
    question: str


@pytest.fixture(autouse=True)
def forbid_secret_reads(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("task profile configuration must not read credentials")

    monkeypatch.setattr(LLMProviderConfig, "api_key", forbidden)
    monkeypatch.setattr(AgentLLMConfig, "credential_status", forbidden)


def _execute(context: RunContext, request: BaseModel) -> TaskResult:
    raise AssertionError("applying configuration must not execute a task")


def _task(task_type: str = "example.research", *, roles: dict[str, str] | None = None) -> ResearchTask:
    return ResearchTask(
        descriptor=TaskDescriptor(
            task_type=task_type,
            module_id=task_type.split(".", 1)[0],
            title="Explicitly registered task",
            version="module-v2",
            result_schema_version="result-v3",
            roles={"writer": "team.writer", "reviewer": "team.reviewer"} if roles is None else roles,
            tools={"system": ["example.read", "example.calculate"], "writer": ["example.read"]},
            capabilities=["example.read", "example.calculate"],
            default_budget=BudgetLimits(
                max_calls=6,
                max_tokens=12000,
                timeout_seconds=120,
                max_parallel_calls=2,
            ),
            request_schema=Request.model_json_schema(),
            result_schema={"type": "object", "required": ["outcome"]},
        ),
        request_type=Request,
        execute=_execute,
        output_fields={"outcome": str},
    )


@pytest.fixture()
def config() -> AgentLLMConfig:
    return AgentLLMConfig.model_validate(
        {
            "providers": {
                "writer": {
                    "kind": "openai_compatible",
                    "api_key_env": "TASK_CONFIG_TEST_WRITER_KEY",
                    "base_url": "https://writer.invalid",
                },
                "reviewer": {
                    "kind": "openai_compatible",
                    "api_key_env": "TASK_CONFIG_TEST_REVIEWER_KEY",
                    "base_url": "https://reviewer.invalid",
                },
            },
            "roles": {
                "team.writer": {"provider": "writer", "model": "model-writer"},
                "team.reviewer": {"provider": "reviewer", "model": "model-reviewer"},
            },
        }
    )


@pytest.mark.parametrize("as_string", [False, True])
def test_missing_optional_config_uses_module_defaults(tmp_path: Path, as_string: bool):
    path = tmp_path / "custom-config-root" / "research" / "tasks.yaml"
    assert load_task_profiles(str(path) if as_string else path) == {}


def test_loads_typed_profiles_from_temporary_yaml(tmp_path: Path):
    path = tmp_path / "tasks.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tasks": {
                    "example.research": {
                        "enabled": False,
                        "roles": {"writer": "other.writer", "reviewer": "other.reviewer"},
                        "budget": {"max_calls": 2},
                        "tools": {"system": ["example.read"]},
                        "capabilities": ["example.read"],
                        "independent_roles": [["writer", "reviewer"]],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile = load_task_profiles(path)["example.research"]

    assert isinstance(profile, TaskProfile)
    assert profile.enabled is False
    assert profile.roles == {"writer": "other.writer", "reviewer": "other.reviewer"}
    assert profile.budget is not None
    assert profile.budget.model_dump(exclude_unset=True) == {"max_calls": 2}
    assert profile.tools == {"system": ["example.read"]}
    assert profile.capabilities == ["example.read"]
    assert profile.independent_roles == [("writer", "reviewer")]


@pytest.mark.parametrize(
    "field",
    ["module", "import", "execute", "request_schema", "result_schema", "version", "result_schema_version"],
)
def test_profile_cannot_select_code_or_change_contracts(tmp_path: Path, field: str):
    path = tmp_path / "tasks.yaml"
    path.write_text(yaml.safe_dump({"tasks": {"example.research": {field: "untrusted.module"}}}))

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_task_profiles(path)


@pytest.mark.parametrize(
    "document", ["", "null", "[]", "tasks: []", "tasks: {example.research: null}", "modules: {}"]
)
def test_invalid_yaml_shape_fails_fast(tmp_path: Path, document: str):
    path = tmp_path / "tasks.yaml"
    path.write_text(document)

    with pytest.raises((QuantSystemError, ValidationError)):
        load_task_profiles(path)


def test_yaml_cannot_construct_python_objects(tmp_path: Path):
    path = tmp_path / "tasks.yaml"
    path.write_text("!!python/object/apply:builtins.str [untrusted]")

    with pytest.raises(yaml.constructor.ConstructorError):
        load_task_profiles(path)


def test_empty_profiles_and_no_credentials_preserve_task_contract(config: AgentLLMConfig):
    original = _task()

    updated = apply_task_profiles([original], {}, config)[0]

    assert updated is not original
    assert updated.descriptor == original.descriptor
    assert updated.descriptor.ready
    assert updated.request_type is original.request_type
    assert updated.execute is original.execute
    assert updated.output_fields is original.output_fields


def test_unknown_task_profile_is_not_discovered_or_registered(config: AgentLLMConfig):
    with pytest.raises(QuantSystemError, match="unregistered tasks: untrusted.module"):
        apply_task_profiles([_task()], {"untrusted.module": TaskProfile()}, config)


def test_disabled_task_remains_discoverable_before_registration(config: AgentLLMConfig):
    tasks = apply_task_profiles([_task()], {"example.research": TaskProfile(enabled=False)}, config)
    registry = ModuleRegistry()
    for task in tasks:
        registry.register(task)

    descriptor = registry.describe()[0]
    assert descriptor.task_type == "example.research"
    assert descriptor.ready is False
    assert descriptor.unavailable_reason is not None
    assert descriptor.unavailable_reason.startswith("disabled")


def test_roles_can_rebind_routes_without_changing_logical_team(config: AgentLLMConfig):
    roles = {"writer": "team.reviewer", "reviewer": "team.writer"}

    updated = apply_task_profiles([_task()], {"example.research": TaskProfile(roles=roles)}, config)[0]

    assert updated.descriptor.roles == roles
    assert updated.descriptor.ready


@pytest.mark.parametrize(
    "roles",
    [
        {},
        {"writer": "team.writer"},
        {"writer": "team.writer", "reviewer": "team.reviewer", "new": "team.writer"},
    ],
)
def test_role_overrides_cannot_remove_or_add_logical_roles(config: AgentLLMConfig, roles: dict[str, str]):
    with pytest.raises(QuantSystemError, match="exact logical role keys"):
        apply_task_profiles([_task()], {"example.research": TaskProfile(roles=roles)}, config)


def test_missing_route_only_marks_affected_task_unavailable(config: AgentLLMConfig):
    roles = {"writer": "team.missing", "reviewer": "team.reviewer"}

    affected, collection = apply_task_profiles(
        [_task(), _task("example.collect", roles={})],
        {"example.research": TaskProfile(roles=roles)},
        config,
    )

    assert affected.descriptor.ready is False
    assert "team.missing" in (affected.descriptor.unavailable_reason or "")
    assert collection.descriptor.ready is True
    assert collection.descriptor.unavailable_reason is None


def test_missing_default_route_is_unavailable_even_without_profile(config: AgentLLMConfig):
    task = _task(roles={"writer": "unconfigured.writer"})

    updated = apply_task_profiles([task], {}, config)[0]

    assert updated.descriptor.ready is False
    assert "unconfigured.writer" in (updated.descriptor.unavailable_reason or "")


@pytest.mark.parametrize("provider", ["writer", "reviewer"])
def test_independence_requires_different_model_names_even_across_providers(
    config: AgentLLMConfig,
    provider: str,
):
    payload = config.model_dump()
    payload["roles"]["team.reviewer"].update(provider=provider, model="model-writer")
    same_model_config = AgentLLMConfig.model_validate(payload)
    profile = TaskProfile(independent_roles=[("writer", "reviewer")])

    updated = apply_task_profiles([_task()], {"example.research": profile}, same_model_config)[0]

    assert updated.descriptor.ready is False
    assert "different model names" in (updated.descriptor.unavailable_reason or "")


def test_independent_roles_can_use_different_models_at_one_provider(config: AgentLLMConfig):
    payload = config.model_dump()
    payload["roles"]["team.reviewer"]["provider"] = "writer"
    profile = TaskProfile(independent_roles=[("writer", "reviewer")])

    updated = apply_task_profiles(
        [_task()],
        {"example.research": profile},
        AgentLLMConfig.model_validate(payload),
    )[0]

    assert updated.descriptor.ready is True


@pytest.mark.parametrize("pair", [("writer", "writer"), ("writer", "unknown")])
def test_independence_policy_must_reference_distinct_declared_roles(config: AgentLLMConfig, pair):
    with pytest.raises(QuantSystemError, match="distinct declared independent roles"):
        apply_task_profiles([_task()], {"example.research": TaskProfile(independent_roles=[pair])}, config)


def test_missing_independent_role_route_is_not_a_startup_error(config: AgentLLMConfig):
    profile = TaskProfile(
        roles={"writer": "team.writer", "reviewer": "missing.audit"},
        independent_roles=[("writer", "reviewer")],
    )

    updated = apply_task_profiles([_task()], {"example.research": profile}, config)[0]

    assert updated.descriptor.ready is False
    assert "missing.audit" in (updated.descriptor.unavailable_reason or "")


def test_policy_can_only_narrow_tools_capabilities_and_budget_without_mutation(config: AgentLLMConfig):
    original = _task()
    before = original.descriptor.model_dump()
    profile = TaskProfile(
        tools={"system": ["example.read"]},
        capabilities=["example.read"],
        budget=BudgetLimits(max_calls=2, max_tokens=4000, timeout_seconds=60, max_parallel_calls=1),
    )
    before_profile = profile.model_dump()

    updated = apply_task_profiles([original], {"example.research": profile}, config)[0]

    assert updated.descriptor.tools == {"system": ["example.read"]}
    assert updated.descriptor.capabilities == ["example.read"]
    assert updated.descriptor.default_budget == profile.budget
    assert original.descriptor.model_dump() == before
    assert profile.model_dump() == before_profile
    assert updated.descriptor.version == original.descriptor.version
    assert updated.descriptor.result_schema_version == original.descriptor.result_schema_version
    assert updated.descriptor.request_schema == original.descriptor.request_schema
    assert updated.descriptor.result_schema == original.descriptor.result_schema


def test_returned_metadata_does_not_alias_original_task_or_profile(config: AgentLLMConfig):
    original = _task()
    profile = TaskProfile(
        roles=dict(original.descriptor.roles),
        tools={"system": ["example.read"]},
        capabilities=["example.read"],
    )
    before = original.descriptor.model_dump()
    before_profile = profile.model_dump()

    updated = apply_task_profiles([original], {"example.research": profile}, config)[0]
    updated.descriptor.roles["writer"] = "mutated.writer"
    updated.descriptor.tools["system"].append("mutated.tool")
    updated.descriptor.capabilities.append("mutated.capability")

    assert original.descriptor.model_dump() == before
    assert profile.model_dump() == before_profile


@pytest.mark.parametrize(
    "override",
    [
        {"tools": {"writer": ["example.calculate"]}},
        {"tools": {"unknown": ["example.read"]}},
        {"tools": {"system": ["shell.execute"]}},
        {"capabilities": ["example.read", "network.write"]},
    ],
)
def test_policy_rejects_permission_escalation(config: AgentLLMConfig, override):
    with pytest.raises(QuantSystemError, match="cannot grant unadvertised"):
        apply_task_profiles([_task()], {"example.research": TaskProfile.model_validate(override)}, config)


def test_explicit_empty_permissions_remove_permissions(config: AgentLLMConfig):
    updated = apply_task_profiles(
        [_task()],
        {"example.research": TaskProfile(tools={}, capabilities=[])},
        config,
    )[0]

    assert updated.descriptor.tools == {}
    assert updated.descriptor.capabilities == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_calls", 7), ("max_tokens", 12001), ("timeout_seconds", 121), ("max_parallel_calls", 3)],
)
def test_budget_cannot_increase_any_limit(config: AgentLLMConfig, field: str, value: int):
    profile = TaskProfile(budget=BudgetLimits.model_validate({field: value}))

    with pytest.raises(QuantSystemError, match=f"cannot increase budget.{field}"):
        apply_task_profiles([_task()], {"example.research": profile}, config)


def test_partial_budget_keeps_omitted_module_limits(config: AgentLLMConfig):
    original = _task()
    profile = TaskProfile(budget=BudgetLimits(max_calls=2))

    updated = apply_task_profiles([original], {"example.research": profile}, config)[0]

    assert updated.descriptor.default_budget.model_dump() == {
        **original.descriptor.default_budget.model_dump(),
        "max_calls": 2,
    }


def test_zero_llm_budget_and_no_roles_require_no_model_routes():
    config = AgentLLMConfig(providers={}, roles={})
    profile = TaskProfile(roles={}, budget=BudgetLimits(max_calls=0, max_tokens=0))

    updated = apply_task_profiles(
        [_task("example.collect", roles={})],
        {"example.collect": profile},
        config,
    )[0]

    assert updated.descriptor.ready is True
    assert updated.descriptor.default_budget.max_calls == 0
    assert updated.descriptor.default_budget.max_tokens == 0


def test_profile_does_not_clear_module_unavailability(config: AgentLLMConfig):
    task = _task()
    task = ResearchTask(
        task.descriptor.model_copy(update={"ready": False, "unavailable_reason": "source unavailable"}),
        task.request_type,
        task.execute,
    )

    updated = apply_task_profiles([task], {"example.research": TaskProfile(enabled=True)}, config)[0]

    assert updated.descriptor.ready is False
    assert updated.descriptor.unavailable_reason == "source unavailable"


def test_default_yaml_defines_only_explicit_tasks_and_expected_policy():
    path = Path(__file__).resolve().parents[2] / "configs" / "research" / "tasks.yaml"

    profiles = load_task_profiles(path)

    assert set(profiles) == {
        "gold.collect",
        "gold.research",
        "factor.research",
        "macro.collect",
        "macro.analyze",
        "macro.research",
    }
    for name in ("gold.collect", "macro.collect", "macro.analyze"):
        assert profiles[name].roles == {}
        budget = profiles[name].budget
        assert budget is not None
        assert budget.max_calls == budget.max_tokens == 0
    for name, expected in (
        ("gold.research", (12, 64000)),
        ("factor.research", (30, 200000)),
        ("macro.research", (4, 64000)),
    ):
        budget = profiles[name].budget
        assert budget is not None
        assert (budget.max_calls, budget.max_tokens) == expected
    assert profiles["macro.research"].roles == {"macro.rates": "statistician", "macro.risks": "critic"}
    assert profiles["gold.research"].independent_roles == [("final_auditor", "orchestrator")]
    assert len(profiles["factor.research"].roles or {}) == 4


@pytest.mark.parametrize("missing_routes", [False, True])
def test_default_yaml_applies_to_actual_module_descriptors_without_running_tasks(
    store,
    registry,
    missing_routes: bool,
):
    root = Path(__file__).resolve().parents[2]
    config = (
        AgentLLMConfig(providers={}, roles={})
        if missing_routes
        else load_agent_llm_config(root / "configs" / "agents" / "default.yaml")
    )

    def no_factor_service(request: FactorTaskRequest) -> AgentResearchOrchestrator:
        raise AssertionError("profile application must not create a factor service")

    tasks = [
        *gold_tasks(GoldService(store, registry), config),
        *factor_tasks(no_factor_service),
        *macro_tasks(MacroService(store, registry), config, store=store, registry=registry),
        _task("research.plan", roles={}),
    ]
    budgets = {task.descriptor.task_type: task.descriptor.default_budget for task in tasks}
    profiles = load_task_profiles(root / "configs" / "research" / "tasks.yaml")

    updated = apply_task_profiles(tasks, profiles, config)
    modules = ModuleRegistry()
    for task in updated:
        assert task.descriptor.default_budget == budgets[task.descriptor.task_type]
        assert task.descriptor.ready is (not missing_routes or not task.descriptor.roles)
        modules.register(task)

    assert len(modules.describe()) == 7
    assert modules.get("macro.research").descriptor.roles == {
        "macro.rates": "statistician",
        "macro.risks": "critic",
    }
