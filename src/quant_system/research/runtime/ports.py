from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from quant_system.domain.exceptions import QuantSystemError
from quant_system.research.runtime.calls import BudgetLimits
from quant_system.research.runtime.models import ResearchPlan, TaskDescriptor, TaskResult

if TYPE_CHECKING:
    from quant_system.research.runtime.engine import RunContext


@dataclass(frozen=True)
class ResearchTask:
    descriptor: TaskDescriptor
    request_type: type[BaseModel]
    execute: Callable[[RunContext, BaseModel], TaskResult]
    output_fields: dict[str, type] | None = None

    def describe(self) -> TaskDescriptor:
        return self.descriptor.model_copy(update={"request_schema": self.request_type.model_json_schema()})


@dataclass(frozen=True)
class ResearchRecipe:
    recipe_id: str
    matches: Callable[[str], bool]
    build: Callable[[str, dict, BudgetLimits], ResearchPlan]
    task_types: tuple[str, ...] = ()


class ModuleRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, ResearchTask] = {}
        self._recipes: dict[str, ResearchRecipe] = {}
        self._recipe_tasks: dict[str, ResearchRecipe] = {}

    def register(self, task: ResearchTask) -> None:
        name = task.descriptor.task_type
        if name in self._tasks:
            raise ValueError(f"research task already registered: {name}")
        if name.split(".", 1)[0] != task.descriptor.module_id:
            raise ValueError("task namespace must match its module")
        self._tasks[name] = task

    def register_recipe(self, recipe: ResearchRecipe) -> None:
        if recipe.recipe_id in self._recipes:
            raise ValueError(f"research recipe already registered: {recipe.recipe_id}")
        overlap = self._recipe_tasks.keys() & set(recipe.task_types)
        if overlap:
            raise ValueError(f"research recipe task routes already registered: {sorted(overlap)}")
        for task_type in recipe.task_types:
            self.get(task_type)
        self._recipes[recipe.recipe_id] = recipe
        self._recipe_tasks.update(dict.fromkeys(recipe.task_types, recipe))

    def recipe(self, question: str, explicit_task: str | None = None) -> ResearchRecipe | None:
        if explicit_task is not None:
            return self._recipe_tasks.get(explicit_task)
        matches = [recipe for recipe in self._recipes.values() if recipe.matches(question)]
        if len(matches) > 1:
            raise QuantSystemError("multiple research recipes match; choose an explicit task_type")
        return matches[0] if matches else None

    def get(self, task_type: str) -> ResearchTask:
        if task_type not in self._tasks:
            raise QuantSystemError(f"research task is not registered: {task_type}")
        return self._tasks[task_type]

    def describe(self) -> list[TaskDescriptor]:
        return [self._tasks[name].describe() for name in sorted(self._tasks)]
