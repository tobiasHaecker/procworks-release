# SPDX-License-Identifier: BUSL-1.1
"""Tests for the static temporal perspective T1/T2 (roadmap E5).

The temporal rules extend Correctness by Construction to time: a schema can
only carry temporal annotations that are well-formed (T1) and whose critical
path fits the deadline (T2). The rules are silent for models without any time
data, so they are fully additive.
"""

from __future__ import annotations

import pytest

from procworks import (
    create_empty_schema,
    serial_insert,
    set_deadline,
    set_time_constraint,
    validate,
)
from procworks.model import TimeConstraint
from procworks.validator import CorrectnessError


def _activity_id(schema, label):
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _chain(schema_id: str):
    schema = create_empty_schema("Zeit", schema_id=schema_id)
    schema = serial_insert(schema, "A", after_node_id="start")
    a = _activity_id(schema, "A")
    schema = serial_insert(schema, "B", after_node_id=a)
    b = _activity_id(schema, "B")
    return schema, a, b


def test_no_temporal_data_means_no_findings() -> None:
    schema, _, _ = _chain("t0")
    findings = [f for f in validate(schema) if f.rule in ("T1", "T2")]
    assert findings == []


def test_well_formed_durations_within_deadline() -> None:
    schema, a, b = _chain("t1")
    schema = set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=30))
    schema = set_time_constraint(schema, b, TimeConstraint(max_duration_seconds=40))
    schema = set_deadline(schema, 100)
    assert [f for f in validate(schema) if f.rule.startswith("T")] == []


def test_negative_duration_is_rejected() -> None:
    schema, a, _ = _chain("t2")
    with pytest.raises(CorrectnessError) as exc:
        set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=-1))
    assert any(f.rule == "T1" for f in exc.value.findings)


def test_negative_deadline_is_rejected() -> None:
    schema, _, _ = _chain("t3")
    with pytest.raises(CorrectnessError) as exc:
        set_deadline(schema, -5)
    assert any(f.rule == "T1" for f in exc.value.findings)


def test_critical_path_exceeding_deadline_is_rejected() -> None:
    schema, a, b = _chain("t4")
    schema = set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=30))
    schema = set_time_constraint(schema, b, TimeConstraint(max_duration_seconds=40))
    # Critical path is 70s; a 60s deadline must be refused (T2).
    with pytest.raises(CorrectnessError) as exc:
        set_deadline(schema, 60)
    assert any(f.rule == "T2" for f in exc.value.findings)


def test_tightening_a_constraint_past_the_deadline_is_rejected() -> None:
    schema, a, b = _chain("t5")
    schema = set_deadline(schema, 50)
    schema = set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=20))
    # Adding B's 40s pushes the critical path to 60s > 50s deadline (T2).
    with pytest.raises(CorrectnessError) as exc:
        set_time_constraint(schema, b, TimeConstraint(max_duration_seconds=40))
    assert any(f.rule == "T2" for f in exc.value.findings)


def test_time_constraint_on_unknown_handling_via_clear() -> None:
    schema, a, _ = _chain("t6")
    schema = set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=10))
    schema = set_time_constraint(schema, a, None)
    assert a not in schema.time_constraints
