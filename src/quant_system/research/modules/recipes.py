"""Explicitly composed financial research routes and snapshot dependencies."""

from __future__ import annotations

import re
from functools import partial

from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.models import InputRef, PlanTask, ResearchPlan
from quant_system.research.runtime.planning import clamp_budget, partition_payload, request_fields
from quant_system.research.runtime.ports import ModuleRegistry, ResearchRecipe

__all__ = ["register_research_recipes"]


def _matches(question: str, *, domains: tuple[bool, bool, bool]) -> bool:
    text = question.strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_-]*\.[a-z][a-z0-9_.-]*", text):
        return False
    gold = "\u9ec4\u91d1" in text or re.search(r"\bgold\b", text) is not None
    macro = (
        any(
            word in text
            for word in ("\u5b8f\u89c2", "\u5229\u7387", "\u6c47\u7387", "\u7f8e\u5143", "\u50a8\u5907")
        )
        or re.search(r"\bmacro(?:economic|economics)?\b", text) is not None
    )
    factor = "\u56e0\u5b50" in text or re.search(r"\bfactors?\b", text) is not None
    return (gold, macro, factor) == domains


def _build_plan(
    task_types: tuple[str, ...],
    registry: ModuleRegistry,
    question: str,
    payload: dict,
    budget: BudgetLimits,
) -> ResearchPlan:
    registered = {name: registry.get(name) for name in task_types}
    if "gold.research" in registered:
        snapshot_field = request_fields(registered["gold.research"]).get("snapshot_id")
        gold_payload = payload.get("gold.research", {})
        if (
            snapshot_field is not None
            and snapshot_field.is_required()
            and "snapshot_id" not in payload
            and not (isinstance(gold_payload, dict) and "snapshot_id" in gold_payload)
        ):
            task_types = (*task_types, "gold.collect")
            registered["gold.collect"] = registry.get("gold.collect")
    ordered = sorted(task_types, key=lambda name: not registered[name].descriptor.acquisition)
    fields = {name: request_fields(task) for name, task in registered.items()}
    arguments = partition_payload(payload, registered)
    nodes = []
    for name in ordered:
        inputs: dict[str, InputRef] = {}
        if name == "gold.research" and "gold.collect" in registered:
            inputs["snapshot_id"] = InputRef(task_id="gold_collect", output_key="snapshot_id")
        if "macro.collect" in registered and name in ("macro.analyze", "gold.research"):
            field_name = "snapshot_id" if name == "macro.analyze" else "macro_snapshot_id"
            if field_name in fields[name] and field_name not in arguments[name]:
                inputs[field_name] = InputRef(task_id="macro_collect", output_key="snapshot_id")
        for field_name in ("focus", "topic"):
            if (
                question
                and not registered[name].descriptor.acquisition
                and field_name in fields[name]
                and field_name not in arguments[name]
                and field_name not in inputs
            ):
                arguments[name][field_name] = question
        nodes.append(
            PlanTask(
                task_id=name.replace(".", "_"),
                task_type=name,
                payload=arguments[name],
                inputs=inputs,
                depends_on=list(dict.fromkeys(reference.task_id for reference in inputs.values())),
                budget=clamp_budget(registered[name].descriptor.default_budget, budget),
            )
        )
    return ResearchPlan(question=question, tasks=nodes, budget=budget)


def register_research_recipes(modules: ModuleRegistry) -> None:
    """Register routes for the available tasks after task registration is complete."""
    available = {descriptor.task_type for descriptor in modules.describe()}
    for recipe_id, domains, task_types, entry_tasks in (
        ("gold", (True, False, False), ("gold.research",), ("gold.research",)),
        ("macro", (False, True, False), ("macro.collect", "macro.analyze"), ()),
        ("gold_macro", (True, True, False), ("macro.collect", "macro.analyze", "gold.research"), ()),
        ("factor", (False, False, True), ("factor.research",), ("factor.research",)),
    ):
        if not set(task_types) <= available:
            continue
        modules.register_recipe(
            ResearchRecipe(
                recipe_id=recipe_id,
                matches=partial(_matches, domains=domains),
                build=partial(_build_plan, task_types, modules),
                task_types=entry_tasks,
            )
        )
