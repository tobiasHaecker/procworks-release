# SPDX-License-Identifier: BUSL-1.1
"""Safe boolean condition evaluator (roadmap step 11+, CONDITIONAL follow-ups).

Conditions are short expressions over the process data values, e.g.
``"betrag > 1000"`` or ``"status == 'open' and amount >= 0"``. They are
evaluated against a variable mapping (an instance's ``data_values``).

Security: expressions are parsed with the ``ast`` module and only a small,
explicit whitelist of node types is allowed -- comparisons, boolean operators,
basic arithmetic, names and literals. Anything else (function calls, attribute
access, subscripts, comprehensions, lambdas) is rejected. ``eval`` is never
used on the raw string, so a condition cannot execute arbitrary code.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Mapping
from typing import Any


class ConditionError(ValueError):
    """A condition expression is malformed or cannot be evaluated."""


_COMPARE_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}


def _validate(node: ast.AST) -> None:
    """Reject any node type outside the whitelist."""

    if isinstance(node, ast.Expression):
        _validate(node.body)
    elif isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ConditionError("unsupported boolean operator")
        for value in node.values:
            _validate(value)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise ConditionError("unsupported unary operator")
        _validate(node.operand)
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise ConditionError("unsupported arithmetic operator")
        _validate(node.left)
        _validate(node.right)
    elif isinstance(node, ast.Compare):
        _validate(node.left)
        for op in node.ops:
            if type(op) not in _COMPARE_OPS:
                raise ConditionError("unsupported comparison operator")
        for comparator in node.comparators:
            _validate(comparator)
    elif isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            raise ConditionError("names may only be read in a condition")
    elif isinstance(node, ast.Constant):
        return
    else:
        raise ConditionError(
            f"unsupported expression element: {type(node).__name__}"
        )


def parse_condition(expression: str) -> ast.Expression:
    """Parse and whitelist-validate a condition; raise ``ConditionError``."""

    if not expression or not expression.strip():
        raise ConditionError("empty condition")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ConditionError(
            f"cannot parse condition '{expression}': {exc.msg}"
        ) from exc
    _validate(tree)
    return tree


def referenced_names(expression: str) -> set[str]:
    """Return the set of variable names a (valid) condition reads."""

    tree = parse_condition(expression)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _evaluate(node: ast.AST, variables: Mapping[str, object]) -> Any:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, variables)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_evaluate(v, variables) for v in node.values)
        return any(_evaluate(v, variables) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_evaluate(node.operand, variables))
    if isinstance(node, ast.BinOp):
        return _BIN_OPS[type(node.op)](
            _evaluate(node.left, variables), _evaluate(node.right, variables)
        )
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, variables)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate(comparator, variables)
            if not _COMPARE_OPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ConditionError(f"unknown variable '{node.id}' in condition")
        return variables[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    raise ConditionError(f"unsupported expression element: {type(node).__name__}")


def evaluate_condition(expression: str, variables: Mapping[str, object]) -> bool:
    """Evaluate ``expression`` against ``variables`` and return a boolean.

    Raises ``ConditionError`` if the expression is malformed or references a
    variable that is not present in ``variables``.
    """

    tree = parse_condition(expression)
    return bool(_evaluate(tree, variables))
