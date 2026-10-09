# SPDX-License-Identifier: BUSL-1.1
"""Tests for the safe boolean condition evaluator.

The evaluator parses a restricted Python expression grammar (comparisons,
boolean and arithmetic operators, names and literals) and rejects everything
else (calls, attribute access, subscripts, ...). It never uses eval().
"""

from __future__ import annotations

import pytest

from procworks import ConditionError, evaluate_condition, referenced_names


def test_comparisons() -> None:
    assert evaluate_condition("x > 100", {"x": 250}) is True
    assert evaluate_condition("x > 100", {"x": 10}) is False
    assert evaluate_condition("x == y", {"x": 1, "y": 1}) is True
    assert evaluate_condition("x != y", {"x": 1, "y": 2}) is True


def test_chained_comparison() -> None:
    assert evaluate_condition("0 < x < 10", {"x": 5}) is True
    assert evaluate_condition("0 < x < 10", {"x": 20}) is False


def test_boolean_operators() -> None:
    assert evaluate_condition("a and b", {"a": True, "b": True}) is True
    assert evaluate_condition("a and b", {"a": True, "b": False}) is False
    assert evaluate_condition("a or b", {"a": False, "b": True}) is True
    assert evaluate_condition("not a", {"a": False}) is True


def test_arithmetic() -> None:
    assert evaluate_condition("a + b > 10", {"a": 6, "b": 5}) is True
    assert evaluate_condition("a * b == 12", {"a": 3, "b": 4}) is True
    assert evaluate_condition("a - b < 0", {"a": 1, "b": 3}) is True


def test_unknown_variable_raises() -> None:
    with pytest.raises(ConditionError):
        evaluate_condition("x > 0", {})


def test_rejects_function_call() -> None:
    with pytest.raises(ConditionError):
        evaluate_condition("len(x) > 0", {"x": [1]})


def test_rejects_attribute_access() -> None:
    with pytest.raises(ConditionError):
        evaluate_condition("x.value > 0", {"x": object()})


def test_rejects_subscript() -> None:
    with pytest.raises(ConditionError):
        evaluate_condition("x[0] > 0", {"x": [1]})


def test_rejects_syntax_error() -> None:
    with pytest.raises(ConditionError):
        evaluate_condition("x >", {"x": 1})


def test_referenced_names() -> None:
    assert referenced_names("a + b > c") == {"a", "b", "c"}
    assert referenced_names("0 < x") == {"x"}
