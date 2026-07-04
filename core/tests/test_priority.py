# SPDX-License-Identifier: BUSL-1.1
"""Tests for the work-item priority and value-class annotations (E8/E3).

These cover the derived priority matrix (Priorit\u00e4t = Auswirkung + Dringlichkeit),
the change operations (which stay additive and never affect K/D/Z correctness)
and the worklist ordering by derived priority.
"""

from __future__ import annotations

import pytest

from procworks import (
    add_agent,
    add_role,
    assign_staff_rule,
    create_empty_schema,
    instantiate,
    open_tasks,
    parallel_insert,
    release,
    serial_insert,
    set_node_priority,
    set_value_class,
)
from procworks.model import (
    ImpactUrgency,
    PriorityLevel,
    StaffRule,
    StaffRuleKind,
    ValueClass,
    WorkItemPriority,
)
from procworks.validator import CorrectnessError


def _activity_id(schema, label):
    return next(n.id for n in schema.nodes.values() if n.label == label)


# --- derived priority matrix ---------------------------------------------


@pytest.mark.parametrize(
    "impact,urgency,expected",
    [
        (ImpactUrgency.LOW, ImpactUrgency.LOW, PriorityLevel.LOW),
        (ImpactUrgency.LOW, ImpactUrgency.MEDIUM, PriorityLevel.MEDIUM),
        (ImpactUrgency.MEDIUM, ImpactUrgency.MEDIUM, PriorityLevel.MEDIUM),
        (ImpactUrgency.HIGH, ImpactUrgency.MEDIUM, PriorityLevel.HIGH),
        (ImpactUrgency.HIGH, ImpactUrgency.HIGH, PriorityLevel.CRITICAL),
    ],
)
def test_priority_is_derived_from_impact_and_urgency(impact, urgency, expected) -> None:
    assert WorkItemPriority(impact=impact, urgency=urgency).level is expected


# --- change operations ----------------------------------------------------


def test_set_node_priority_round_trip() -> None:
    schema = create_empty_schema("Prio", schema_id="p1")
    schema = serial_insert(schema, "A", after_node_id="start")
    act = _activity_id(schema, "A")
    prio = WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH)
    schema = set_node_priority(schema, act, prio)
    assert schema.node_priorities[act].level is PriorityLevel.CRITICAL
    schema = set_node_priority(schema, act, None)
    assert act not in schema.node_priorities


def test_set_priority_rejects_gateway() -> None:
    schema = create_empty_schema("PrioGw", schema_id="p2")
    schema = serial_insert(schema, "A", after_node_id="start")
    start_id = schema.start_node().id
    with pytest.raises(CorrectnessError):
        set_node_priority(schema, start_id, WorkItemPriority())


def test_set_value_class_rejects_non_activity() -> None:
    schema = create_empty_schema("Val", schema_id="p3")
    start_id = schema.start_node().id
    with pytest.raises(CorrectnessError):
        set_value_class(schema, start_id, ValueClass.VALUE_ADDING)


def test_set_value_class_round_trip() -> None:
    schema = create_empty_schema("Val2", schema_id="p4")
    schema = serial_insert(schema, "A", after_node_id="start")
    act = _activity_id(schema, "A")
    schema = set_value_class(schema, act, ValueClass.BUSINESS_NECESSARY)
    assert schema.nodes[act].value_class is ValueClass.BUSINESS_NECESSARY
    schema = set_value_class(schema, act, None)
    assert schema.nodes[act].value_class is None


# --- worklist ordering ----------------------------------------------------


def test_open_tasks_sorted_by_priority() -> None:
    schema = create_empty_schema("Liste", schema_id="p5")
    schema = parallel_insert(schema, ["Niedrig", "Hoch"], after_node_id="start")
    low = _activity_id(schema, "Niedrig")
    high = _activity_id(schema, "Hoch")
    schema = add_role(schema, "SB", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    rule = StaffRule(kind=StaffRuleKind.ROLE, ref="sb")
    schema = assign_staff_rule(schema, low, rule)
    schema = assign_staff_rule(schema, high, rule)
    schema = set_node_priority(
        schema, low, WorkItemPriority(impact=ImpactUrgency.LOW, urgency=ImpactUrgency.LOW)
    )
    schema = set_node_priority(
        schema,
        high,
        WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH),
    )
    released = release(schema)
    instance = instantiate(released)
    # Both parallel branches are open at once; the worklist must list the
    # critical task first (roadmap E8 ordering).
    tasks = open_tasks(released, instance)
    assert [t.node_id for t in tasks] == [high, low]
    assert tasks[0].priority is PriorityLevel.CRITICAL
    assert tasks[1].priority is PriorityLevel.LOW


def test_open_tasks_default_priority_is_medium() -> None:
    schema = create_empty_schema("Default", schema_id="p6")
    schema = serial_insert(schema, "A", after_node_id="start")
    act = _activity_id(schema, "A")
    schema = add_role(schema, "SB", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = assign_staff_rule(schema, act, StaffRule(kind=StaffRuleKind.ROLE, ref="sb"))
    released = release(schema)
    instance = instantiate(released)
    tasks = open_tasks(released, instance)
    assert tasks[0].priority is PriorityLevel.MEDIUM
