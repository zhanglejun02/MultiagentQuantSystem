"""Small, non-Turing-complete DSL for cross-sectional factor expressions."""

from __future__ import annotations

import ast
import math
import statistics
from datetime import date

from quant_system.domain.exceptions import QuantSystemError

FieldData = dict[str, dict[date, dict[str, float]]]

ALLOWED_FIELDS = frozenset({"open", "high", "low", "close", "volume", "amount", "return_1d"})
ALLOWED_FUNCTIONS = frozenset({"rank", "delta", "delay", "ts_mean", "ts_std", "abs"})
ALLOWED_BINARY_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)


class FactorExpressionError(QuantSystemError):
    pass


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
        self.complexity = sum(
            isinstance(node, (ast.Call, ast.BinOp, ast.UnaryOp)) for node in ast.walk(root)
        )

    def _validate(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                if child.id not in ALLOWED_FIELDS | ALLOWED_FUNCTIONS:
                    raise FactorExpressionError(f"unknown name: {child.id}")
            elif isinstance(child, ast.Call):
                if not isinstance(child.func, ast.Name) or child.func.id not in ALLOWED_FUNCTIONS:
                    raise FactorExpressionError("only approved factor functions are allowed")
                if child.keywords:
                    raise FactorExpressionError("keyword arguments are not supported")
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

    @staticmethod
    def _window_size(node: ast.AST) -> int:
        if not isinstance(node, ast.Constant) or not isinstance(node.value, int) or node.value <= 0:
            raise FactorExpressionError("window must be a positive integer")
        if node.value > 252:
            raise FactorExpressionError("window cannot exceed 252 trading days")
        return node.value

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
            name = node.func.id
            if name == "abs" and len(node.args) == 1:
                value = self._evaluate_node(
                    node.args[0], symbol=symbol, index=index, days=days, fields=fields
                )
                return abs(value) if value is not None else None
            if name in {"delay", "delta"} and len(node.args) == 2:
                window = self._window_size(node.args[1])
                current = self._evaluate_node(
                    node.args[0], symbol=symbol, index=index, days=days, fields=fields
                )
                lagged = self._evaluate_node(
                    node.args[0], symbol=symbol, index=index - window, days=days, fields=fields
                )
                if name == "delay":
                    return lagged
                return None if current is None or lagged is None else current - lagged
            if name in {"ts_mean", "ts_std"} and len(node.args) == 2:
                window = self._window_size(node.args[1])
                if index + 1 < window:
                    return None
                values = [
                    self._evaluate_node(
                        node.args[0], symbol=symbol, index=offset, days=days, fields=fields
                    )
                    for offset in range(index - window + 1, index + 1)
                ]
                if any(value is None for value in values):
                    return None
                complete = [value for value in values if value is not None]
                return statistics.pstdev(complete) if name == "ts_std" else statistics.fmean(complete)
            raise FactorExpressionError(f"invalid arguments for {name}")
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