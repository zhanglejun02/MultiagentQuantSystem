"""Small, non-Turing-complete DSL for cross-sectional factor expressions.

Operators live in a registry: adding a time-series/cross-sectional operator is
a single ``@register_operator`` function, the validator and evaluator read
signatures from the registry and never change.
"""

from __future__ import annotations

import ast
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from quant_system.domain.exceptions import QuantSystemError

FieldData = dict[str, dict[date, dict[str, float]]]

ALLOWED_FIELDS = frozenset({"open", "high", "low", "close", "volume", "amount", "return_1d"})
ALLOWED_BINARY_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
MAX_WINDOW = 252


class FactorExpressionError(QuantSystemError):
    pass


EvalArg = Callable[[ast.AST, int], float | None]
OperatorImpl = Callable[[EvalArg, list[ast.expr], int], float | None]


@dataclass(frozen=True)
class Operator:
    name: str
    arity: int
    window: bool  # second argument must be a bounded positive integer window
    outer_only: bool  # structural op; ranking is applied cross-sectionally by the caller
    evaluate: OperatorImpl | None


OPERATORS: dict[str, Operator] = {}


def register_operator(
    name: str, *, arity: int, window: bool = False, outer_only: bool = False
) -> Callable[[OperatorImpl], OperatorImpl]:
    def decorator(impl: OperatorImpl) -> OperatorImpl:
        if name in OPERATORS:
            raise ValueError(f"operator already registered: {name}")
        OPERATORS[name] = Operator(name, arity, window, outer_only, impl)
        return impl

    return decorator


def _window_size(node: ast.AST) -> int:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, int) or node.value <= 0:
        raise FactorExpressionError("window must be a positive integer")
    if node.value > MAX_WINDOW:
        raise FactorExpressionError(f"window cannot exceed {MAX_WINDOW} trading days")
    return node.value


def _window_values(evaluate: EvalArg, arg: ast.expr, index: int, window: int) -> list[float] | None:
    if index + 1 < window:
        return None
    values = [evaluate(arg, offset) for offset in range(index - window + 1, index + 1)]
    if any(value is None for value in values):
        return None
    return [value for value in values if value is not None]


@register_operator("abs", arity=1)
def _op_abs(evaluate: EvalArg, args: list[ast.expr], index: int) -> float | None:
    value = evaluate(args[0], index)
    return None if value is None else abs(value)


@register_operator("delay", arity=2, window=True)
def _op_delay(evaluate: EvalArg, args: list[ast.expr], index: int) -> float | None:
    return evaluate(args[0], index - _window_size(args[1]))


@register_operator("delta", arity=2, window=True)
def _op_delta(evaluate: EvalArg, args: list[ast.expr], index: int) -> float | None:
    current = evaluate(args[0], index)
    lagged = evaluate(args[0], index - _window_size(args[1]))
    return None if current is None or lagged is None else current - lagged


@register_operator("ts_mean", arity=2, window=True)
def _op_ts_mean(evaluate: EvalArg, args: list[ast.expr], index: int) -> float | None:
    values = _window_values(evaluate, args[0], index, _window_size(args[1]))
    return None if values is None else statistics.fmean(values)


@register_operator("ts_std", arity=2, window=True)
def _op_ts_std(evaluate: EvalArg, args: list[ast.expr], index: int) -> float | None:
    values = _window_values(evaluate, args[0], index, _window_size(args[1]))
    return None if values is None else statistics.pstdev(values)


# rank is structural: scores() returns raw values and the caller ranks them cross-sectionally.
OPERATORS["rank"] = Operator("rank", arity=1, window=False, outer_only=True, evaluate=None)

ALLOWED_FUNCTIONS = frozenset(OPERATORS)


class FactorExpression:
    def __init__(self, expression: str) -> None:
        self.expression = expression.strip()
        try:
            parsed = ast.parse(self.expression, mode="eval")
        except SyntaxError as exc:
            raise FactorExpressionError(f"invalid expression: {exc.msg}") from exc
        root = parsed.body
        if not (
            isinstance(root, ast.Call)
            and isinstance(root.func, ast.Name)
            and root.func.id == "rank"
            and len(root.args) == 1
            and not root.keywords
        ):
            raise FactorExpressionError("expression must have one outer rank(...) call")
        self._validate(root)
        self._score_node = root.args[0]
        self.inputs = sorted(
            {node.id for node in ast.walk(root) if isinstance(node, ast.Name)} & ALLOWED_FIELDS
        )
        self.complexity = sum(isinstance(node, (ast.Call, ast.BinOp, ast.UnaryOp)) for node in ast.walk(root))

    def _validate(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                if child.id not in ALLOWED_FIELDS and child.id not in OPERATORS:
                    raise FactorExpressionError(f"unknown name: {child.id}")
            elif isinstance(child, ast.Call):
                spec = OPERATORS.get(child.func.id) if isinstance(child.func, ast.Name) else None
                if spec is None:
                    raise FactorExpressionError("only approved factor functions are allowed")
                if spec.outer_only and child is not node:
                    raise FactorExpressionError(f"{spec.name} is only allowed as the outermost function")
                if child.keywords:
                    raise FactorExpressionError("keyword arguments are not supported")
                if len(child.args) != spec.arity:
                    raise FactorExpressionError(f"{spec.name} requires exactly {spec.arity} argument(s)")
                if spec.window:
                    _window_size(child.args[1])
            elif isinstance(child, ast.BinOp) and not isinstance(child.op, ALLOWED_BINARY_OPS):
                raise FactorExpressionError("unsupported binary operator")
            elif isinstance(child, ast.UnaryOp) and not isinstance(child.op, (ast.USub, ast.UAdd)):
                raise FactorExpressionError("unsupported unary operator")
            elif isinstance(child, ast.Constant) and not isinstance(child.value, (int, float)):
                raise FactorExpressionError("only numeric constants are allowed")
            elif isinstance(
                child,
                (
                    ast.Attribute,
                    ast.Subscript,
                    ast.Lambda,
                    ast.Dict,
                    ast.List,
                    ast.Tuple,
                    ast.Compare,
                    ast.BoolOp,
                    ast.IfExp,
                ),
            ):
                raise FactorExpressionError(f"unsupported syntax: {type(child).__name__}")

    def _evaluate_node(
        self,
        node: ast.AST,
        *,
        symbol: str,
        index: int,
        days: list[date],
        fields: FieldData,
    ) -> float | None:
        if index < 0:
            return None
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return float(node.value)
            raise FactorExpressionError("only numeric constants are allowed")
        if isinstance(node, ast.Name):
            return fields.get(node.id, {}).get(days[index], {}).get(symbol)
        if isinstance(node, ast.UnaryOp):
            value = self._evaluate_node(node.operand, symbol=symbol, index=index, days=days, fields=fields)
            if value is None:
                return None
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left = self._evaluate_node(node.left, symbol=symbol, index=index, days=days, fields=fields)
            right = self._evaluate_node(node.right, symbol=symbol, index=index, days=days, fields=fields)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return None if right == 0 else left / right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            spec = OPERATORS.get(node.func.id)
            if spec is None or spec.evaluate is None or len(node.args) != spec.arity:
                raise FactorExpressionError(f"invalid arguments for {node.func.id}")

            def evaluate_arg(sub: ast.AST, at: int) -> float | None:
                return self._evaluate_node(sub, symbol=symbol, index=at, days=days, fields=fields)

            return spec.evaluate(evaluate_arg, list(node.args), index)
        raise FactorExpressionError(f"unsupported expression node: {type(node).__name__}")

    def scores(
        self,
        *,
        symbols: list[str],
        index: int,
        days: list[date],
        fields: FieldData,
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        for symbol in symbols:
            value = self._evaluate_node(
                self._score_node, symbol=symbol, index=index, days=days, fields=fields
            )
            if value is not None and math.isfinite(value):
                scores[symbol] = value
        return scores
