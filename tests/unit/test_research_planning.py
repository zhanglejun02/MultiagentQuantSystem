from __future__ import annotations

import ast
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import LLMMessage, LLMResponse
from quant_system.domain.exceptions import QuantSystemError
from quant_system.gold.research import GoldResearchRequest, GoldTaskRequest
from quant_system.research.modules.recipes import register_research_recipes
from quant_system.research.runtime.calls import BudgetLimits, CallReceipt
from quant_system.research.runtime.models import (
    InputRef,
    PlanTask,
    ResearchPlan,
    RunRequest,
    TaskDescriptor,
)
from quant_system.research.runtime.planning import ResearchPlanner, single_task_plan, validate_plan
from quant_system.research.runtime.ports import ModuleRegistry, ResearchRecipe, ResearchTask


class CollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date = date(2023, 1, 1)
    series_ids: list[str] = Field(default_factory=lambda: ["DGS10"])


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1)


class GoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = "Au99.99"
    focus: str = "Gold trends"
    max_weight: float = Field(default=0.10, ge=0, le=0.30)
    macro_snapshot_id: str | None = None


class FactorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def ordered_dates(self) -> FactorRequest:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not exceed end_date")
        return self


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class DocumentAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str


def _unexecuted(*_args):
    raise AssertionError("planning must never execute a task")


def _registry(*, actual_gold: bool = False) -> ModuleRegistry:
    registry = ModuleRegistry()
    for name, request_type, outputs, acquisition in (
        ("macro.collect", CollectRequest, {"snapshot_id": str, "count": int}, True),
        ("macro.analyze", AnalyzeRequest, {}, False),
        ("gold.research", GoldTaskRequest if actual_gold else GoldRequest, {}, False),
        ("factor.research", FactorRequest, {}, False),
    ):
        registry.register(
            ResearchTask(
                TaskDescriptor(
                    task_type=name,
                    module_id=name.split(".")[0],
                    title=name,
                    acquisition=acquisition,
                ),
                request_type,
                _unexecuted,
                output_fields=outputs,
            )
        )
    if actual_gold:
        registry.register(
            ResearchTask(
                TaskDescriptor(
                    task_type="gold.collect",
                    module_id="gold",
                    title="Gold collection",
                    acquisition=True,
                ),
                GoldResearchRequest,
                _unexecuted,
                output_fields={"snapshot_id": str},
            )
        )
    register_research_recipes(registry)
    return registry


def _config(**updates) -> AgentLLMConfig:
    return AgentLLMConfig.model_validate(
        {
            "providers": {
                "fake": {
                    "kind": "openai_compatible",
                    "api_key_env": "UNUSED_PLANNING_TEST_KEY",
                    "base_url": "https://example.invalid",
                }
            },
            "roles": {"orchestrator": {"provider": "fake", "model": "planner-test"}},
            **updates,
        }
    )


def _documents_registry() -> ModuleRegistry:
    registry = _registry()
    for name, request_type, outputs, acquisition in (
        ("documents.index", DocumentRequest, {"document_id": str}, True),
        ("documents.inspect", DocumentAnalysisRequest, {}, False),
        ("documents.gold", DocumentRequest, {}, False),
    ):
        registry.register(
            ResearchTask(
                TaskDescriptor(task_type=name, module_id="documents", title=name, acquisition=acquisition),
                request_type,
                _unexecuted,
                output_fields=outputs,
            )
        )

    def build(question: str, payload: dict, budget: BudgetLimits) -> ResearchPlan:
        return ResearchPlan(
            question=question,
            tasks=[
                PlanTask(task_id="index", task_type="documents.index", payload=payload, budget=budget),
                PlanTask(
                    task_id="inspect",
                    task_type="documents.inspect",
                    depends_on=["index"],
                    inputs={"document_id": InputRef(task_id="index", output_key="document_id")},
                    budget=budget,
                ),
            ],
            budget=budget,
        )

    registry.register_recipe(
        ResearchRecipe(
            recipe_id="documents-review",
            matches=lambda question: "documents" in question,
            build=build,
            task_types=("documents.inspect",),
        )
    )
    return registry


def test_recipe_selection_separates_explicit_routes_from_keyword_matches() -> None:
    registry = ModuleRegistry()
    registry.register(
        ResearchTask(
            TaskDescriptor(task_type="documents.inspect", module_id="documents", title="Inspect"),
            AnalyzeRequest,
            _unexecuted,
        )
    )
    recipe = ResearchRecipe(
        recipe_id="documents",
        matches=lambda question: "documents" in question,
        build=lambda _question, payload, budget: ResearchPlan(
            tasks=[
                PlanTask(task_id="inspect", task_type="documents.inspect", payload=payload, budget=budget)
            ],
            budget=budget,
        ),
        task_types=("documents.inspect",),
    )
    registry.register_recipe(recipe)

    assert registry.recipe("inspect documents") is recipe
    assert registry.recipe("unrelated", explicit_task="documents.inspect") is recipe
    assert registry.recipe("documents", explicit_task="gold.research") is None
    assert registry.recipe("unrelated") is None


def test_recipe_registration_rejects_duplicate_ids_routes_and_missing_entry_tasks() -> None:
    registry = ModuleRegistry()
    registry.register(_registry().get("gold.research"))

    def build(_question: str, _payload: dict, _budget: BudgetLimits) -> ResearchPlan:
        return ResearchPlan(tasks=[PlanTask(task_id="task", task_type="gold.research")])

    registry.register_recipe(ResearchRecipe("first", lambda _question: True, build, ("gold.research",)))
    with pytest.raises(ValueError, match="already registered"):
        registry.register_recipe(ResearchRecipe("first", lambda _question: False, build))
    with pytest.raises(ValueError, match="routes already registered"):
        registry.register_recipe(ResearchRecipe("second", lambda _question: False, build, ("gold.research",)))
    with pytest.raises(QuantSystemError, match="not registered"):
        registry.register_recipe(ResearchRecipe("missing", lambda _question: False, build, ("missing.run",)))
    registry.register_recipe(ResearchRecipe("second", lambda _question: True, build))
    with pytest.raises(QuantSystemError, match="choose.*task_type"):
        registry.recipe("ambiguous")


@pytest.mark.parametrize("entry", ["review documents", "documents.inspect", None])
def test_fourth_nonfinancial_module_supplies_its_own_multitask_recipe(entry: str | None) -> None:
    registry = _documents_registry()
    payload = {"text": "caller document"}
    budget = BudgetLimits(max_calls=1, max_tokens=100, timeout_seconds=20, max_parallel_calls=1)
    if entry is None:
        plan = single_task_plan(
            RunRequest(task_type="documents.inspect", payload=payload, budget=budget), registry
        )
    else:
        planner = ResearchPlanner(registry, _config(), None)
        plan = planner.propose(entry, payload=payload, budget=budget)
        assert planner.receipts == [] and planner.ledger is None

    assert {task.module_id for task in registry.describe()} == {"gold", "macro", "factor", "documents"}
    assert plan.source == ("EXPLICIT" if entry is None else "RULE")
    assert [task.task_type for task in plan.tasks] == ["documents.index", "documents.inspect"]
    assert plan.tasks[0].payload == payload == {"text": "caller document"}
    assert plan.tasks[1].inputs == {"document_id": InputRef(task_id="index", output_key="document_id")}
    assert plan.tasks[1].depends_on == ["index"]
    assert plan.budget == budget and all(task.budget == budget for task in plan.tasks)


def test_custom_recipe_keeps_core_schema_budget_readiness_and_historical_checks() -> None:
    registry = _documents_registry()
    planner = ResearchPlanner(registry, _config(), None)
    with pytest.raises(QuantSystemError, match="unexpected"):
        planner.propose("review documents", payload={"text": "caller", "shell": "pwd"})
    with pytest.raises(QuantSystemError, match="historical.*acquisition"):
        single_task_plan(
            RunRequest(
                task_type="documents.inspect",
                payload={"text": "caller"},
                mode="HISTORICAL",
                knowledge_cutoff=datetime(2024, 6, 30, tzinfo=UTC),
            ),
            registry,
        )

    def build(_question: str, payload: dict, _budget: BudgetLimits) -> ResearchPlan:
        return ResearchPlan(tasks=[PlanTask(task_id="task", task_type="documents.gold", payload=payload)])

    registry.register_recipe(ResearchRecipe("oversized", lambda question: question == "oversized", build))
    with pytest.raises(QuantSystemError, match="budget exceeds"):
        planner.propose("oversized", payload={"text": "caller"}, budget=BudgetLimits(max_calls=1))

    registry.register(
        ResearchTask(
            TaskDescriptor(
                task_type="documents.unavailable",
                module_id="documents",
                title="Unavailable",
                ready=False,
                unavailable_reason="document source unavailable",
            ),
            DocumentRequest,
            _unexecuted,
        )
    )
    registry.register_recipe(
        ResearchRecipe(
            "unavailable",
            lambda question: question == "unavailable",
            lambda _question, payload, budget: ResearchPlan(
                tasks=[
                    PlanTask(
                        task_id="task", task_type="documents.unavailable", payload=payload, budget=budget
                    )
                ],
                budget=budget,
            ),
        )
    )
    with pytest.raises(QuantSystemError, match="document source unavailable"):
        planner.propose("unavailable", payload={"text": "caller"})


@pytest.mark.parametrize("nested", [False, True])
def test_unbound_task_falls_back_to_its_schema_without_running_keyword_recipes(nested: bool) -> None:
    registry = _documents_registry()
    registry.register_recipe(ResearchRecipe("never-matched", _unexecuted, _unexecuted))
    text = {"text": "unmodified caller document"}
    payload = {"documents.gold": text} if nested else text
    plans = (
        ResearchPlanner(registry, _config(), None).propose("documents.gold", payload=payload),
        single_task_plan(RunRequest(task_type="documents.gold", payload=payload), registry),
    )
    for plan in plans:
        assert len(plan.tasks) == 1
        assert plan.tasks[0].task_type == "documents.gold"
        assert plan.tasks[0].payload == text
        assert plan.tasks[0].inputs == {} and plan.tasks[0].depends_on == []
    with pytest.raises(QuantSystemError, match="invalid request"):
        single_task_plan(RunRequest(task_type="documents.gold"), registry)
    with pytest.raises(QuantSystemError, match="unexpected"):
        ResearchPlanner(registry, _config(), None).propose(
            "documents.gold", payload={"text": "caller", "route": "untrusted"}
        )


@pytest.mark.parametrize("question", ["gold.research", "factor.research", "macro.collect", "documents.gold"])
def test_financial_keyword_matchers_do_not_capture_task_names(question: str) -> None:
    assert _registry().recipe(question) is None


def test_explicit_task_does_not_need_any_registered_recipe() -> None:
    registry = ModuleRegistry()
    registry.register(_registry(actual_gold=True).get("gold.research"))
    payload = {"snapshot_id": "frozen"}
    assert registry.recipe("gold", explicit_task="gold.research") is None
    for plan in (
        ResearchPlanner(registry, _config(), None).propose("gold.research", payload=payload),
        single_task_plan(RunRequest(task_type="gold.research", payload=payload), registry),
    ):
        assert len(plan.tasks) == 1 and plan.tasks[0].payload == payload
    with pytest.raises(QuantSystemError, match="choose.*task_type"):
        ResearchPlanner(registry, _config(), None).propose("gold", payload=payload)
    with pytest.raises(QuantSystemError, match="snapshot_id"):
        single_task_plan(RunRequest(task_type="gold.research"), registry)


def test_financial_recipe_registration_is_scoped_to_available_tasks_and_dependencies() -> None:
    registry = ModuleRegistry()
    register_research_recipes(registry)
    assert registry.recipe("gold") is None and registry.recipe("macro") is None
    registry.register(_registry(actual_gold=True).get("gold.research"))
    register_research_recipes(registry)
    assert registry.recipe("gold") is not None
    assert registry.recipe("macro") is None and registry.recipe("factor") is None
    assert registry.recipe("macro gold") is None
    with pytest.raises(QuantSystemError, match=r"not registered: gold\.collect"):
        single_task_plan(RunRequest(task_type="gold.research"), registry)


@pytest.mark.parametrize("module_name", ["planning", "ports"])
def test_core_planning_has_no_financial_rules_or_module_imports(module_name: str) -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "quant_system"
        / "research"
        / "runtime"
        / f"{module_name}.py"
    ).read_text(encoding="utf-8")
    forbidden_imports = (
        "quant_system.gold",
        "quant_system.macro",
        "quant_system.factors",
        "quant_system.research.modules",
    )
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert not any((node.module or "").startswith(prefix) for prefix in forbidden_imports)
        elif isinstance(node, ast.Import):
            assert not any(
                alias.name.startswith(prefix) for alias in node.names for prefix in forbidden_imports
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not any(
                word in node.value
                for word in (
                    "gold.",
                    "macro.",
                    "factor.",
                    "黄金",
                    "宏观",
                    "因子",
                    "利率",
                    "汇率",
                    "美元",
                    "储备",
                )
            )
    assert "_template_tasks" not in source


def test_gold_macro_rule_uses_one_snapshot_and_independent_analysis() -> None:
    registry = _registry()
    planner = ResearchPlanner(registry, _config(), None)
    plan = planner.propose("研究宏观利率对黄金的影响")

    assert plan.source == "RULE"
    assert [task.task_type for task in plan.tasks] == [
        "macro.collect",
        "macro.analyze",
        "gold.research",
    ]
    collect, analyze, gold = plan.tasks
    assert analyze.depends_on == gold.depends_on == [collect.task_id]
    assert analyze.inputs == {"snapshot_id": InputRef(task_id=collect.task_id, output_key="snapshot_id")}
    assert gold.inputs == {"macro_snapshot_id": InputRef(task_id=collect.task_id, output_key="snapshot_id")}
    assert validate_plan(plan, registry) == plan


def test_gold_macro_rule_collects_both_actual_required_snapshots_before_analysis() -> None:
    registry = _registry(actual_gold=True)
    planner = ResearchPlanner(registry, _config(), None)
    gold_payload = {"symbol": "518880", "max_weight": 0.05, "focus": "caller gold focus"}
    macro_payload = {"start_date": "2024-01-01", "series_ids": ["DFII10"]}
    plan = planner.propose(
        "macro gold", payload={"gold.research": gold_payload, "macro.collect": macro_payload}
    )

    assert len(plan.tasks) == 4
    assert [task.task_type for task in plan.tasks] == [
        "macro.collect",
        "gold.collect",
        "macro.analyze",
        "gold.research",
    ]
    macro_collect, gold_collect, macro_analyze, gold_research = plan.tasks
    assert [registry.get(task.task_type).descriptor.acquisition for task in plan.tasks] == [
        True,
        True,
        False,
        False,
    ]
    assert macro_collect.payload == macro_payload
    assert gold_collect.payload == {}
    assert gold_research.payload == gold_payload
    assert macro_analyze.depends_on == [macro_collect.task_id]
    assert macro_analyze.inputs == {
        "snapshot_id": InputRef(task_id=macro_collect.task_id, output_key="snapshot_id")
    }
    assert gold_research.depends_on == [gold_collect.task_id, macro_collect.task_id]
    assert gold_research.inputs == {
        "snapshot_id": InputRef(task_id=gold_collect.task_id, output_key="snapshot_id"),
        "macro_snapshot_id": InputRef(task_id=macro_collect.task_id, output_key="snapshot_id"),
    }
    assert validate_plan(plan, registry) == plan


@pytest.mark.parametrize("question", ["gold risk", "gold.research"])
@pytest.mark.parametrize("nested", [False, True])
def test_actual_gold_rule_and_explicit_route_collect_missing_snapshot(question: str, nested: bool) -> None:
    registry = _registry(actual_gold=True)
    planner = ResearchPlanner(registry, _config(), None)
    gold_payload = {"symbol": "518880", "max_weight": 0.05, "focus": "caller focus"}
    plan = planner.propose(question, payload={"gold.research": gold_payload} if nested else gold_payload)

    assert [task.task_type for task in plan.tasks] == ["gold.collect", "gold.research"]
    collect, research = plan.tasks
    assert collect.payload == ({} if nested else gold_payload)
    assert research.payload == gold_payload
    assert research.depends_on == [collect.task_id]
    assert research.inputs == {"snapshot_id": InputRef(task_id=collect.task_id, output_key="snapshot_id")}
    assert planner.receipts == [] and planner.ledger is None


@pytest.mark.parametrize("question", ["gold risk", "gold.research"])
@pytest.mark.parametrize("nested", [False, True])
def test_actual_gold_rule_preserves_frozen_snapshot_without_collection(question: str, nested: bool) -> None:
    gold_payload = {"snapshot_id": "caller-frozen-gold", "focus": "caller focus"}
    plan = ResearchPlanner(_registry(actual_gold=True), _config(), None).propose(
        question, payload={"gold.research": gold_payload} if nested else gold_payload
    )

    assert len(plan.tasks) == 1
    assert plan.tasks[0].task_type == "gold.research"
    assert plan.tasks[0].payload == gold_payload
    assert plan.tasks[0].inputs == {} and plan.tasks[0].depends_on == []


@pytest.mark.parametrize(
    "macro_payload", [{}, {"macro_snapshot_id": None}, {"macro_snapshot_id": "caller-frozen-macro"}]
)
def test_combined_actual_gold_rule_preserves_caller_snapshots(macro_payload: dict) -> None:
    gold_payload = {"snapshot_id": "caller-frozen-gold", "focus": "caller focus", **macro_payload}
    plan = ResearchPlanner(_registry(actual_gold=True), _config(), None).propose(
        "macro gold", payload={"gold.research": gold_payload}
    )

    assert [task.task_type for task in plan.tasks] == [
        "macro.collect",
        "macro.analyze",
        "gold.research",
    ]
    macro_collect, macro_analyze, research = plan.tasks
    reference = InputRef(task_id=macro_collect.task_id, output_key="snapshot_id")
    assert macro_analyze.inputs == {"snapshot_id": reference}
    assert research.payload == gold_payload
    assert research.inputs == ({} if macro_payload else {"macro_snapshot_id": reference})
    assert research.depends_on == ([] if macro_payload else [macro_collect.task_id])


def test_actual_frozen_gold_snapshot_does_not_require_a_registered_collector() -> None:
    registry = ModuleRegistry()
    registry.register(_registry(actual_gold=True).get("gold.research"))
    register_research_recipes(registry)
    payload = {"snapshot_id": "caller-frozen-gold"}

    for plan in (
        ResearchPlanner(registry, _config(), None).propose("gold", payload=payload),
        single_task_plan(RunRequest(task_type="gold.research", payload=payload), registry),
    ):
        assert len(plan.tasks) == 1
        assert plan.tasks[0].task_type == "gold.research"
        assert plan.tasks[0].payload["snapshot_id"] == "caller-frozen-gold"
        assert plan.tasks[0].inputs == {}


def test_actual_gold_collection_requires_caller_dates_without_inventing_request_fields() -> None:
    class DatedGoldCollection(GoldResearchRequest):
        start_date: date
        end_date: date

    registry = ModuleRegistry()
    registry.register(_registry(actual_gold=True).get("gold.research"))
    registry.register(
        ResearchTask(
            TaskDescriptor(
                task_type="gold.collect", module_id="gold", title="Dated gold collection", acquisition=True
            ),
            DatedGoldCollection,
            _unexecuted,
            output_fields={"snapshot_id": str},
        )
    )
    register_research_recipes(registry)
    planner = ResearchPlanner(registry, _config(), None)
    with pytest.raises(QuantSystemError, match="start_date"):
        planner.propose("gold")

    dates = {"start_date": "2024-01-01", "end_date": "2024-06-30"}
    plan = planner.propose("gold", payload={"gold.collect": dates})
    collect, research = plan.tasks
    assert collect.payload == dates
    assert research.payload == {"focus": "gold"}
    assert research.inputs == {"snapshot_id": InputRef(task_id=collect.task_id, output_key="snapshot_id")}


@pytest.mark.parametrize("nested", [False, True])
def test_direct_actual_gold_request_expands_missing_snapshot_with_root_limits(nested: bool) -> None:
    registry = _registry(actual_gold=True)
    budget = BudgetLimits(max_calls=1, max_tokens=100, timeout_seconds=20, max_parallel_calls=1)
    gold_payload = {"symbol": "518880", "max_weight": 0.05, "focus": "caller focus"}
    plan = single_task_plan(
        RunRequest(
            task_type="gold.research",
            payload={"gold.research": gold_payload} if nested else gold_payload,
            budget=budget,
        ),
        registry,
    )

    assert plan.source == "EXPLICIT"
    assert plan.budget == budget
    assert [task.task_type for task in plan.tasks] == ["gold.collect", "gold.research"]
    collect, research = plan.tasks
    assert collect.payload == ({} if nested else gold_payload)
    assert research.payload == gold_payload
    assert research.inputs == {"snapshot_id": InputRef(task_id=collect.task_id, output_key="snapshot_id")}
    assert research.depends_on == [collect.task_id]
    assert all(task.budget == budget for task in plan.tasks)


@pytest.mark.parametrize("nested", [False, True])
def test_direct_actual_gold_request_uses_frozen_snapshot_in_historical_mode(nested: bool) -> None:
    cutoff = datetime(2024, 6, 30, tzinfo=UTC)
    gold_payload = {"snapshot_id": "caller-frozen-gold", "focus": "caller focus"}
    plan = single_task_plan(
        RunRequest(
            task_type="gold.research",
            payload={"gold.research": gold_payload} if nested else gold_payload,
            mode="HISTORICAL",
            knowledge_cutoff=cutoff,
        ),
        _registry(actual_gold=True),
    )

    assert plan.mode == "HISTORICAL" and plan.knowledge_cutoff == cutoff
    assert len(plan.tasks) == 1
    assert plan.tasks[0].payload == gold_payload
    assert plan.tasks[0].inputs == {} and plan.tasks[0].depends_on == []


def test_direct_actual_gold_request_cannot_collect_in_historical_mode() -> None:
    with pytest.raises(QuantSystemError, match="historical.*acquisition"):
        single_task_plan(
            RunRequest(
                task_type="gold.research",
                mode="HISTORICAL",
                knowledge_cutoff=datetime(2024, 6, 30, tzinfo=UTC),
            ),
            _registry(actual_gold=True),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "518880", "gold.research": {"symbol": "518800"}},
        {"gold.research": {"symbol": "518880"}, "symbol": "518800"},
        {"snapshot_id": "first", "gold.research": {"snapshot_id": "second"}},
        {"gold.research": {"snapshot_id": "first"}, "snapshot_id": "second"},
        {"focus": "first", "gold.collect": {"focus": "second"}},
        {"gold.collect": {"focus": "first"}, "focus": "second"},
        {"series_ids": ["DGS10"], "macro.collect": {"series_ids": ["DFII10"]}},
        {"macro.collect": {"series_ids": ["DGS10"]}, "series_ids": ["DFII10"]},
    ],
)
def test_actual_gold_expansion_rejects_duplicate_fields_per_task(payload: dict) -> None:
    with pytest.raises(QuantSystemError, match="duplicate"):
        ResearchPlanner(_registry(actual_gold=True), _config(), None).propose("macro gold", payload=payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": "value"},
        {"gold.research": {"start_date": "2024-01-01"}},
        {"gold.collect": {"snapshot_id": "not-a-collection-field"}},
        {"macro.collect": {"unknown": "value"}},
        {"gold.research": "not-an-object"},
    ],
)
def test_actual_gold_expansion_rejects_unknown_and_non_object_payloads(payload: dict) -> None:
    with pytest.raises(QuantSystemError, match="unexpected|object"):
        ResearchPlanner(_registry(actual_gold=True), _config(), None).propose("macro gold", payload=payload)


@pytest.mark.parametrize(
    ("question", "expected"),
    [("gold risk", ["gold.research"]), ("macro outlook", ["macro.collect", "macro.analyze"])],
)
def test_single_domain_rules(question: str, expected: list[str]) -> None:
    plan = ResearchPlanner(_registry(), _config(), None).propose(question)
    assert [task.task_type for task in plan.tasks] == expected


def test_planner_uses_injected_parent_caller_without_a_second_ledger() -> None:
    from quant_system.research.runtime.calls import BudgetLedger, StructuredCaller

    published: list[CallReceipt] = []
    ledger = BudgetLedger(BudgetLimits(max_calls=1))
    caller = StructuredCaller(_config(), FakeGateway([_candidate()]), ledger, sink=published.append)
    planner = ResearchPlanner(_registry(), _config(), None)
    plan = planner.propose("gold", use_model=True, caller=caller)
    assert plan.source == "MODEL"
    assert ledger.usage()["calls_used"] == 1
    assert planner.ledger is None
    assert len(published) == 2
    assert not ledger.can_afford(1)


@pytest.mark.parametrize("declared", ["str", "string", {"type": "string"}])
def test_output_declarations_accept_string_and_schema_forms(declared) -> None:
    registry = _registry()
    registry.register(
        ResearchTask(
            TaskDescriptor(task_type="documents.collect", module_id="documents", title="Collect"),
            CollectRequest,
            _unexecuted,
            output_fields={"snapshot_id": declared},
        )
    )
    plan = ResearchPlan(
        tasks=[
            PlanTask(task_id="collect", task_type="documents.collect"),
            PlanTask(
                task_id="analyze",
                task_type="macro.analyze",
                depends_on=["collect"],
                inputs={"snapshot_id": InputRef(task_id="collect", output_key="snapshot_id")},
            ),
        ]
    )
    assert validate_plan(plan, registry) == plan


@pytest.mark.parametrize("question", ["factor momentum", "factor.research"])
def test_factor_requires_real_caller_dates(question: str) -> None:
    planner = ResearchPlanner(_registry(), _config(), None)
    with pytest.raises(QuantSystemError, match="start_date"):
        planner.propose(question)
    plan = planner.propose(question, payload={"start_date": "2024-01-01", "end_date": "2024-06-30"})
    assert plan.tasks[0].task_type == "factor.research"
    assert plan.tasks[0].payload["start_date"] == "2024-01-01"
    assert plan.tasks[0].payload["topic"] == question


def test_direct_factor_recipe_preserves_caller_topic_and_dates() -> None:
    payload = {"topic": "caller topic", "start_date": "2024-01-01", "end_date": "2024-06-30"}
    plan = single_task_plan(RunRequest(task_type="factor.research", payload=payload), _registry())
    assert len(plan.tasks) == 1 and plan.tasks[0].payload == payload
    with pytest.raises(QuantSystemError, match="topic|start_date"):
        single_task_plan(RunRequest(task_type="factor.research"), _registry())


def test_ambiguous_rule_requires_explicit_choice() -> None:
    with pytest.raises(QuantSystemError, match="choose|choice|task_type"):
        ResearchPlanner(_registry(), _config(), None).propose("research something")


def test_direct_request_clamps_child_budget_without_changing_root() -> None:
    budget = BudgetLimits(max_calls=1, max_tokens=100, timeout_seconds=20, max_parallel_calls=1)
    request = RunRequest(task_type="gold.research", budget=budget)
    plan = single_task_plan(request, _registry())
    assert plan.source == "EXPLICIT"
    assert plan.budget == plan.tasks[0].budget == budget


@pytest.mark.parametrize("payload", [{"shell": "pwd"}, {"max_weight": "wrong"}, {"max_weight": 0.9}])
def test_invalid_request_arguments_fail_before_submission(payload: dict) -> None:
    with pytest.raises(QuantSystemError):
        single_task_plan(RunRequest(task_type="gold.research", payload=payload), _registry())


@pytest.mark.parametrize("value", ["0.10", True, [], {}])
def test_json_primitive_type_errors_are_not_coerced_to_request_numbers(value: object) -> None:
    with pytest.raises(QuantSystemError):
        single_task_plan(RunRequest(task_type="gold.research", payload={"max_weight": value}), _registry())


@pytest.mark.parametrize(
    "tasks",
    [
        [PlanTask(task_id="same", task_type="gold.research")] * 2,
        [PlanTask(task_id="one", task_type="unknown.execute")],
        [PlanTask(task_id="one", task_type="gold.research", depends_on=["missing"])],
        [
            PlanTask(task_id="one", task_type="gold.research", depends_on=["two"]),
            PlanTask(task_id="two", task_type="gold.research", depends_on=["one"]),
        ],
    ],
)
def test_invalid_graphs_are_rejected(tasks: list[PlanTask]) -> None:
    with pytest.raises(QuantSystemError):
        validate_plan(ResearchPlan(tasks=tasks), _registry())


@pytest.mark.parametrize(
    ("output_key", "depends_on"),
    [("missing", ["collect"]), ("count", ["collect"]), ("snapshot_id", [])],
)
def test_refs_require_registered_compatible_output_and_explicit_dependency(
    output_key: str, depends_on: list[str]
) -> None:
    plan = ResearchPlan(
        tasks=[
            PlanTask(task_id="collect", task_type="macro.collect"),
            PlanTask(
                task_id="analyze",
                task_type="macro.analyze",
                depends_on=depends_on,
                inputs={"snapshot_id": InputRef(task_id="collect", output_key=output_key)},
            ),
        ]
    )
    with pytest.raises(QuantSystemError):
        validate_plan(plan, _registry())


def test_explicit_registered_nonfinancial_task_uses_its_own_schema() -> None:
    class DocumentRequest(BaseModel):
        text: str

    registry = ModuleRegistry()
    registry.register(
        ResearchTask(
            TaskDescriptor(task_type="documents.inspect", module_id="documents", title="Inspect"),
            DocumentRequest,
            _unexecuted,
        )
    )
    planner = ResearchPlanner(registry, _config(), None)
    plan = planner.propose("documents.inspect", payload={"text": "plain document"})
    assert plan.tasks[0].payload == {"text": "plain document"}
    with pytest.raises(QuantSystemError, match="unexpected"):
        planner.propose("documents.inspect", payload={"text": "document", "url": "https://example.invalid"})


def test_rule_payloads_are_partitioned_without_dropping_unknown_arguments() -> None:
    planner = ResearchPlanner(_registry(), _config(), None)
    plan = planner.propose(
        "macro gold", payload={"macro.collect": {"series_ids": ["DFII10"]}, "symbol": "518880"}
    )
    assert plan.tasks[0].payload == {"series_ids": ["DFII10"]}
    assert plan.tasks[2].payload["symbol"] == "518880"
    with pytest.raises(QuantSystemError, match="unexpected"):
        planner.propose("macro gold", payload={"shell": "pwd"})


@pytest.mark.parametrize(
    "payload",
    [
        {"focus": "first", "gold.research": {"focus": "second"}},
        {"gold.research": {"focus": "first"}, "focus": "second"},
    ],
)
def test_rule_payload_cannot_silently_overwrite_duplicate_arguments(payload: dict) -> None:
    with pytest.raises(QuantSystemError, match="duplicate"):
        ResearchPlanner(_registry(), _config(), None).propose("gold", payload=payload)


def test_schema_allows_literal_shell_code_and_url_text_in_a_research_question() -> None:
    focus = "Inspect shell code and https://example.invalid, without executing or fetching it"
    plan = single_task_plan(RunRequest(task_type="gold.research", payload={"focus": focus}), _registry())
    assert plan.tasks[0].payload["focus"] == focus


@pytest.mark.parametrize("field", ["shell", "code", "url", "route", "tools", "roles"])
def test_unregistered_execution_controls_are_rejected_by_request_fields(field: str) -> None:
    with pytest.raises(QuantSystemError, match="unexpected"):
        single_task_plan(RunRequest(task_type="gold.research", payload={field: "anything"}), _registry())


@pytest.mark.parametrize(
    "payload",
    [
        {"plan": {"tasks": []}},
        {"focus": {"nested": [{"schema_version": "research-plan-v1", "tasks": []}]}},
        {"focus": ResearchPlan(tasks=[PlanTask(task_id="nested", task_type="gold.research")])},
    ],
)
def test_plans_cannot_be_nested_in_task_payloads(payload: dict) -> None:
    with pytest.raises(QuantSystemError, match="nested"):
        single_task_plan(RunRequest(task_type="gold.research", payload=payload), _registry())


def test_historical_analysis_is_allowed_but_acquisition_is_not() -> None:
    cutoff = datetime(2024, 6, 30, tzinfo=UTC)
    plan = single_task_plan(
        RunRequest(
            task_type="macro.analyze",
            payload={"snapshot_id": "frozen"},
            mode="HISTORICAL",
            knowledge_cutoff=cutoff,
        ),
        _registry(),
    )
    assert plan.mode == "HISTORICAL"
    assert plan.knowledge_cutoff == cutoff
    with pytest.raises(QuantSystemError, match="historical.*acquisition"):
        single_task_plan(
            RunRequest(task_type="macro.collect", mode="HISTORICAL", knowledge_cutoff=cutoff), _registry()
        )


def test_acquisition_cannot_depend_on_analysis_but_independent_roots_remain_valid() -> None:
    analysis = PlanTask(task_id="gold", task_type="gold.research")
    collect = PlanTask(task_id="collect", task_type="macro.collect")
    plan = ResearchPlan(tasks=[analysis, collect])
    assert validate_plan(plan, _registry()) == plan
    with pytest.raises(QuantSystemError, match="acquisition.*precede"):
        validate_plan(
            plan.model_copy(
                update={"tasks": [analysis, collect.model_copy(update={"depends_on": ["gold"]})]}
            ),
            _registry(),
        )


def test_unavailable_tasks_are_rejected_by_default_and_can_be_described_without_ready_check() -> None:
    registry = ModuleRegistry()
    registry.register(
        ResearchTask(
            TaskDescriptor(
                task_type="gold.research",
                module_id="gold",
                title="Gold",
                ready=False,
                unavailable_reason="missing module credential",
            ),
            GoldRequest,
            _unexecuted,
        )
    )
    plan = ResearchPlan(tasks=[PlanTask(task_id="gold", task_type="gold.research")])
    with pytest.raises(QuantSystemError, match="missing module credential"):
        validate_plan(plan, registry)
    assert validate_plan(plan, registry, skip_ready=True) == plan


@pytest.mark.parametrize("field", ["max_calls", "max_tokens", "timeout_seconds", "max_parallel_calls"])
def test_each_child_budget_is_bounded_by_the_root(field: str) -> None:
    budget = BudgetLimits(max_calls=2, max_tokens=1000, timeout_seconds=10, max_parallel_calls=1)
    child = budget.model_copy(update={field: getattr(budget, field) + 1})
    with pytest.raises(QuantSystemError, match="budget"):
        validate_plan(
            ResearchPlan(
                budget=budget, tasks=[PlanTask(task_id="gold", task_type="gold.research", budget=child)]
            ),
            _registry(),
        )


def test_child_caps_are_not_summed_as_independent_allocations() -> None:
    tasks = [PlanTask(task_id=f"gold_{index}", task_type="gold.research") for index in range(6)]
    assert validate_plan(ResearchPlan(tasks=tasks), _registry()).tasks == tasks


def test_submission_revalidates_model_copy_and_frozen_schema_version() -> None:
    plan = ResearchPlan(tasks=[PlanTask(task_id="gold", task_type="gold.research")])
    with pytest.raises(QuantSystemError, match="invalid research plan"):
        validate_plan(plan.model_copy(update={"schema_version": "research-plan-v2"}), _registry())
    with pytest.raises(QuantSystemError):
        validate_plan(plan.model_copy(update={"tasks": plan.tasks * 7}), _registry())


def test_submitting_a_plan_does_not_ignore_outer_limits_mode_or_payload() -> None:
    plan = ResearchPlan(tasks=[PlanTask(task_id="gold", task_type="gold.research")])
    assert single_task_plan(RunRequest(plan=plan), _registry()) == plan
    with pytest.raises(QuantSystemError, match="budget"):
        single_task_plan(RunRequest(plan=plan, budget=BudgetLimits(max_calls=1)), _registry())
    with pytest.raises(QuantSystemError, match="historical"):
        single_task_plan(RunRequest(plan=plan, mode="HISTORICAL"), _registry())
    with pytest.raises(QuantSystemError, match="payload"):
        single_task_plan(RunRequest(plan=plan, payload={"shell": "pwd"}), _registry())


def test_refs_cannot_target_dotted_paths_unknown_fields_or_overwrite_payload() -> None:
    collect = PlanTask(task_id="collect", task_type="macro.collect")
    for field, payload in (("unknown", {}), ("snapshot_id", {"snapshot_id": "another"})):
        analyze = PlanTask(
            task_id="analyze",
            task_type="macro.analyze",
            payload=payload,
            depends_on=["collect"],
            inputs={field: InputRef(task_id="collect", output_key="snapshot_id")},
        )
        with pytest.raises(QuantSystemError):
            validate_plan(ResearchPlan(tasks=[collect, analyze]), _registry())
    with pytest.raises(ValueError):
        InputRef(task_id="collect", output_key="payload.snapshot_id")


def test_incompatible_container_reference_is_not_accepted_as_a_string() -> None:
    registry = _registry()
    registry.register(
        ResearchTask(
            TaskDescriptor(task_type="documents.collect", module_id="documents", title="Documents"),
            CollectRequest,
            _unexecuted,
            output_fields={"snapshot_ids": list[str]},
        )
    )
    with pytest.raises(QuantSystemError, match="incompatible"):
        validate_plan(
            ResearchPlan(
                tasks=[
                    PlanTask(task_id="documents", task_type="documents.collect"),
                    PlanTask(
                        task_id="analyze",
                        task_type="macro.analyze",
                        depends_on=["documents"],
                        inputs={"snapshot_id": InputRef(task_id="documents", output_key="snapshot_ids")},
                    ),
                ]
            ),
            registry,
        )


@pytest.mark.parametrize(
    ("source_type", "field_name", "accepted"),
    [
        (str | None, "snapshot_id", False),
        (str | None, "macro_snapshot_id", True),
        (bool, "snapshot_id", False),
    ],
)
def test_ref_type_compatibility_checks_every_possible_output_value(
    source_type: Any, field_name: str, accepted: bool
) -> None:
    registry = _registry()
    registry.register(
        ResearchTask(
            TaskDescriptor(task_type="documents.collect", module_id="documents", title="Documents"),
            CollectRequest,
            _unexecuted,
            output_fields={"snapshot_id": source_type},
        )
    )
    consumer = "macro.analyze" if field_name == "snapshot_id" else "gold.research"
    plan = ResearchPlan(
        tasks=[
            PlanTask(task_id="collect", task_type="documents.collect"),
            PlanTask(
                task_id="consumer",
                task_type=consumer,
                depends_on=["collect"],
                inputs={field_name: InputRef(task_id="collect", output_key="snapshot_id")},
            ),
        ]
    )
    if accepted:
        assert validate_plan(plan, registry) == plan
    else:
        with pytest.raises(QuantSystemError, match="incompatible"):
            validate_plan(plan, registry)


class FakeGateway:
    def __init__(self, responses: list[dict | str], before_send: Callable[[], None] | None = None) -> None:
        self.responses = responses
        self.before_send = before_send
        self.calls: list[tuple[str, list[LLMMessage]]] = []

    def complete(self, role: str, messages: list[LLMMessage]) -> LLMResponse:
        if self.before_send is not None:
            self.before_send()
        response = self.responses[len(self.calls)]
        self.calls.append((role, messages))
        return LLMResponse(
            content=response if isinstance(response, str) else json.dumps(response),
            provider="fake",
            model="planner-test",
            input_tokens=10,
            output_tokens=5,
        )


def _candidate(**updates) -> dict:
    return (
        ResearchPlan(
            question="model supplied question",
            tasks=[PlanTask(task_id="gold", task_type="gold.research")],
        ).model_dump(mode="json")
        | updates
    )


def test_model_plan_is_an_actual_validated_plan_with_pre_send_audit_and_shared_root() -> None:
    published: list[CallReceipt] = []
    candidate = _candidate(
        plan_id="untrusted", source="EXPLICIT", budget=BudgetLimits(max_calls=999).model_dump()
    )

    def before_send() -> None:
        assert published[-1].status == "STARTED"
        assert published[-1].response is None

    gateway = FakeGateway([candidate], before_send)
    budget = BudgetLimits(max_calls=2)
    planner = ResearchPlanner(_registry(), _config(), gateway, sink=published.append)
    plan = planner.propose("gold", use_model=True, budget=budget)

    assert isinstance(plan, ResearchPlan)
    assert plan.source == "MODEL" and plan.plan_id != "untrusted"
    assert plan.question == "gold"
    assert plan.budget == plan.tasks[0].budget == budget
    assert [receipt.status for receipt in published] == ["STARTED", "COMPLETED"]
    assert gateway.calls[0][0] == "research.planner"
    assert planner.receipts[0].route == {"provider": "fake", "model": "planner-test"}
    assert planner.ledger is not None
    assert planner.ledger.usage()["calls_used"] == 1
    child = planner.ledger.child(plan.tasks[0].budget)
    child.reserve(0)
    assert planner.ledger.usage()["calls_used"] == 2
    assert not child.can_afford(1)


@pytest.mark.parametrize(
    "first", ["not JSON", _candidate(tasks=[{"task_id": "bad", "task_type": "unknown.run"}])]
)
def test_model_has_one_schema_or_semantic_repair_charged_to_the_same_root(first: dict | str) -> None:
    published: list[CallReceipt] = []
    gateway = FakeGateway([first, _candidate()])
    planner = ResearchPlanner(_registry(), _config(), gateway, sink=published.append)
    plan = planner.propose("gold", use_model=True, budget=BudgetLimits(max_calls=2))
    assert len(gateway.calls) == 2
    assert [receipt.status for receipt in published] == ["STARTED", "INVALID", "STARTED", "COMPLETED"]
    first_receipt, repaired = planner.receipts
    assert first_receipt.sequence == 1 and repaired.sequence == 2
    assert repaired.repair_of == first_receipt.call_id
    assert planner.ledger is not None and not planner.ledger.can_afford(1)
    assert plan.budget.max_calls == 2


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": "research-plan-v2"},
        {"tasks": [{"task_id": "one", "task_type": "gold.research", "route": "untrusted"}]},
        {"tasks": [{"task_id": "one", "task_type": "gold.research", "version": "2.0"}]},
        {"tasks": [{"task_id": "one", "task_type": "gold.research", "result_schema_version": "2.0"}]},
        {"tasks": [{"task_id": "one", "task_type": "gold.research", "payload": {"code": "print(1)"}}]},
        {"tasks": [{"task_id": "one", "task_type": "unknown.execute"}]},
        {"mode": "HISTORICAL", "knowledge_cutoff": "2024-06-30T00:00:00Z"},
    ],
)
def test_model_cannot_change_contracts_routes_arguments_or_policy(updates: dict) -> None:
    gateway = FakeGateway([_candidate(**updates)] * 2)
    planner = ResearchPlanner(_registry(), _config(), gateway, sink=lambda _receipt: None)
    with pytest.raises(QuantSystemError):
        planner.propose("gold", use_model=True)
    assert len(gateway.calls) == 2
    assert [receipt.status for receipt in planner.receipts] == ["INVALID", "INVALID"]


def test_model_cannot_invent_factor_dates_missing_from_the_caller_payload() -> None:
    candidate = _candidate(
        tasks=[
            {
                "task_id": "factor",
                "task_type": "factor.research",
                "payload": {"topic": "factor", "start_date": "2024-01-01", "end_date": "2024-06-30"},
            }
        ]
    )
    planner = ResearchPlanner(
        _registry(), _config(), FakeGateway([candidate] * 2), sink=lambda _receipt: None
    )
    with pytest.raises(QuantSystemError, match="date|payload"):
        planner.propose("factor", use_model=True)


@pytest.mark.parametrize("payload", [{"shell": "pwd"}, {"max_weight": 0.05}, {"symbol": "518880"}])
def test_model_cannot_drop_unknown_or_change_caller_supplied_request_arguments(payload: dict) -> None:
    gateway = FakeGateway([_candidate()] * 2)
    planner = ResearchPlanner(_registry(), _config(), gateway, sink=lambda _receipt: None)
    with pytest.raises(QuantSystemError):
        planner.propose("gold", payload=payload, use_model=True)


def test_model_can_preserve_caller_arguments_using_registered_request_defaults() -> None:
    planner = ResearchPlanner(_registry(), _config(), FakeGateway([_candidate()]), sink=lambda _receipt: None)
    plan = planner.propose("gold", payload={"symbol": "Au99.99"}, use_model=True)
    assert plan.tasks[0].task_type == "gold.research"


def test_model_cannot_override_caller_macro_snapshot_with_an_input_reference() -> None:
    candidate = _candidate(
        tasks=[
            {"task_id": "collect", "task_type": "macro.collect"},
            {
                "task_id": "gold",
                "task_type": "gold.research",
                "depends_on": ["collect"],
                "inputs": {"macro_snapshot_id": {"task_id": "collect", "output_key": "snapshot_id"}},
            },
        ]
    )
    planner = ResearchPlanner(
        _registry(), _config(), FakeGateway([candidate] * 2), sink=lambda _receipt: None
    )
    with pytest.raises(QuantSystemError):
        planner.propose("macro gold", payload={"macro_snapshot_id": "caller-frozen"}, use_model=True)


def test_model_accepts_caller_dates_as_python_dates_without_changing_them() -> None:
    dates = {"start_date": date(2024, 1, 1), "end_date": date(2024, 6, 30)}
    candidate = _candidate(
        tasks=[
            {
                "task_id": "factor",
                "task_type": "factor.research",
                "payload": {"topic": "factor", **{key: value.isoformat() for key, value in dates.items()}},
            }
        ]
    )
    planner = ResearchPlanner(_registry(), _config(), FakeGateway([candidate]), sink=lambda _receipt: None)
    plan = planner.propose("factor", payload=dates, use_model=True)
    assert plan.tasks[0].payload["start_date"] == "2024-01-01"


def test_config_caps_planning_and_repairs_even_when_caller_requests_more() -> None:
    gateway = FakeGateway(["invalid", _candidate()])
    planner = ResearchPlanner(
        _registry(),
        _config(research_budget={"max_llm_calls_per_run": 1, "max_total_tokens_per_run": 60_000}),
        gateway,
        sink=lambda _receipt: None,
    )
    with pytest.raises(QuantSystemError, match="budget"):
        planner.propose("gold", use_model=True, budget=BudgetLimits(max_calls=20))
    assert len(gateway.calls) == 1
    assert planner.ledger is not None
    assert planner.ledger.limits.max_calls == 1
    assert planner.ledger.limits.max_tokens == 60_000


def test_model_route_uses_namespaced_binding_when_configured_and_only_catalog_data() -> None:
    config = _config(
        roles={
            "orchestrator": {"provider": "fake", "model": "fallback"},
            "research.planner": {"provider": "fake", "model": "dedicated"},
        }
    )
    gateway = FakeGateway([_candidate()])
    planner = ResearchPlanner(_registry(), config, gateway, sink=lambda _receipt: None)
    planner.propose("gold", use_model=True)
    assert planner.receipts[0].model == "dedicated"
    data = json.loads(gateway.calls[0][1][-1].content)
    collect = next(task for task in data["catalog"] if task["task_type"] == "macro.collect")
    assert collect["output_fields"]["snapshot_id"] == {"type": "string"}
    assert "properties" in collect["request_schema"]
    assert "roles" not in collect and "tools" not in collect
    assert "providers" not in data and "api_key_env" not in data


def test_model_catalog_has_only_registered_tasks_and_never_exposes_recipe_callbacks() -> None:
    registry = _documents_registry()
    candidate = ResearchPlan(
        tasks=[PlanTask(task_id="task", task_type="documents.gold", payload={"text": "caller"})]
    ).model_dump(mode="json")
    gateway = FakeGateway([candidate])
    planner = ResearchPlanner(registry, _config(), gateway, sink=lambda _receipt: None)
    plan = planner.propose("review documents", payload={"text": "caller"}, use_model=True)

    assert plan.source == "MODEL" and plan.tasks[0].task_type == "documents.gold"
    data = json.loads(gateway.calls[0][1][-1].content)
    assert {task["task_type"] for task in data["catalog"]} == {task.task_type for task in registry.describe()}
    assert "documents-review" not in json.dumps(data)
    for task in data["catalog"]:
        assert not {"recipe_id", "matches", "build", "import", "module", "execute"} & task.keys()


def test_model_cannot_select_a_recipe_id_or_import_path_in_place_of_a_registered_task() -> None:
    for task_type in ("documents-review", "quant_system.research.modules.recipes.register_research_recipes"):
        candidate = ResearchPlan(tasks=[PlanTask(task_id="task", task_type=task_type)]).model_dump(
            mode="json"
        )
        gateway = FakeGateway([candidate, candidate])
        planner = ResearchPlanner(_documents_registry(), _config(), gateway, sink=lambda _receipt: None)
        with pytest.raises(QuantSystemError, match="not registered"):
            planner.propose("review documents", use_model=True)
        assert len(gateway.calls) == 2


def test_model_planning_refuses_network_without_a_persistent_audit_sink() -> None:
    gateway = FakeGateway([])
    planner = ResearchPlanner(_registry(), _config(), gateway)
    with pytest.raises(QuantSystemError, match="audit sink"):
        planner.propose("gold", use_model=True)
    assert gateway.calls == [] and planner.receipts == []


def test_audit_sink_failure_prevents_network_and_keeps_failure_receipt() -> None:
    def broken_sink(_receipt: CallReceipt) -> None:
        raise OSError("audit storage unavailable")

    gateway = FakeGateway([])
    planner = ResearchPlanner(_registry(), _config(), gateway, sink=broken_sink)
    with pytest.raises(QuantSystemError, match="audit sink failed"):
        planner.propose("gold", use_model=True)
    assert gateway.calls == []
    assert planner.receipts[0].status == "FAILED"


def test_repository_duck_protocol_receives_a_stable_plan_audit_id_before_network() -> None:
    class Repository:
        def __init__(self) -> None:
            self.records: list[tuple[str, CallReceipt]] = []

        def record_call(self, run_id: str, receipt: CallReceipt) -> None:
            self.records.append((run_id, receipt))

    repository = Repository()

    def before_send() -> None:
        assert repository.records[-1][1].status == "STARTED"

    planner = ResearchPlanner(
        _registry(), _config(), FakeGateway([_candidate()], before_send), repository=repository
    )
    plan = planner.propose("gold", use_model=True)
    assert {run_id for run_id, _receipt in repository.records} == {plan.plan_id}
    assert [receipt.status for _run_id, receipt in repository.records] == ["STARTED", "COMPLETED"]


def test_real_artifact_repository_records_model_plan_intent_before_fake_send(store, registry) -> None:
    from quant_system.research.runtime.storage import ArtifactRunRepository

    repository = ArtifactRunRepository(store, registry)

    def before_send():
        audit = repository.load()[0]
        assert audit.status == "RUNNING"
        assert repository.calls(audit.run_id)[0].status == "STARTED"

    planner = ResearchPlanner(
        _registry(), _config(), FakeGateway([_candidate()], before_send), repository=repository
    )
    plan = planner.propose("gold", use_model=True)
    assert repository.get_plan(plan.plan_id) == plan
    assert repository.get(plan.plan_id).status == "COMPLETED"
    assert repository.get(plan.plan_id).usage["calls_used"] == 1
    assert repository.calls(plan.plan_id)[0].status == "COMPLETED"
    reopened = ArtifactRunRepository(store, registry)
    assert reopened.get_plan(plan.plan_id) == plan


def test_real_repository_keeps_failed_planner_receipts_without_replay(store, registry) -> None:
    from quant_system.research.runtime.storage import ArtifactRunRepository

    repository = ArtifactRunRepository(store, registry)
    planner = ResearchPlanner(
        _registry(), _config(), FakeGateway(["invalid", "invalid"]), repository=repository
    )
    with pytest.raises(QuantSystemError):
        planner.propose("gold", use_model=True)
    audit = repository.load()[0]
    assert audit.status == "FAILED"
    assert audit.usage["calls_used"] == 2
    assert len(repository.calls(audit.run_id)) == 2
    assert all(receipt.status == "INVALID" for receipt in repository.calls(audit.run_id))


def test_insufficient_token_budget_prevents_the_first_network_call() -> None:
    gateway = FakeGateway([])
    planner = ResearchPlanner(_registry(), _config(), gateway, sink=lambda _receipt: None)
    with pytest.raises(QuantSystemError, match="token budget"):
        planner.propose("gold", use_model=True, budget=BudgetLimits(max_tokens=1))
    assert gateway.calls == []
