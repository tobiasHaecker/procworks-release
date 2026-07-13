# SPDX-License-Identifier: BUSL-1.1
"""Tests for the time-based, automatic worklist prioritisation.

Realises the "Zeitbasierte-Priorisierung-Konzept": from the target times
modelled on activities the todo list orders itself so that deadline risk rises
to the top and overdue tasks sit above everything else. The logic is pure and
clock-injected, so these tests drive it deterministically with a fake ``now``
(concept step Z0), plus a round-trip through the change operations (Z1/Z2).

The prioritisation is a read-only *view* concern: it must never relax a
correctness rule and must behave exactly as before for models without any
target time (backward compatibility / leitplanke L3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    set_deadline,
    set_node_priority,
    set_time_constraint,
    validate,
)
from procworks.model import (
    ImpactUrgency,
    StaffRule,
    StaffRuleKind,
    TimeConstraint,
    TimeCriticality,
    WorkItemPriority,
)
from procworks.validator import CorrectnessError
from procworks.worklist_priority import (
    TimeContext,
    assess,
    criticality_from_ratio,
    remaining_critical_path_seconds,
    target_seconds,
)

_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)


def _activity_id(schema, label):
    return next(n.id for n in schema.nodes.values() if n.label == label)


# --- Z0: pure band logic (fake clock, no API) ----------------------------


@pytest.mark.parametrize(
    "ratio,expected",
    [
        (0.0, TimeCriticality.ON_TRACK),
        (0.49, TimeCriticality.ON_TRACK),
        (0.5, TimeCriticality.WARNING),
        (0.79, TimeCriticality.WARNING),
        (0.8, TimeCriticality.AT_RISK),
        (0.99, TimeCriticality.AT_RISK),
        (1.0, TimeCriticality.OVERDUE),
        (2.5, TimeCriticality.OVERDUE),
    ],
)
def test_criticality_bands_from_ratio(ratio, expected) -> None:
    assert criticality_from_ratio(ratio) is expected


def test_target_seconds_fallback_rule_s() -> None:
    # target_lead wins when set...
    assert target_seconds(TimeConstraint(max_duration_seconds=100, target_lead_seconds=30)) == 30
    # ...otherwise the processing duration is the fallback reaction time.
    assert target_seconds(TimeConstraint(max_duration_seconds=100)) == 100
    # nothing set -> not part of the time ordering.
    assert target_seconds(TimeConstraint()) is None
    assert target_seconds(None) is None


def test_assess_without_target_or_clock_is_band_none() -> None:
    schema = create_empty_schema("Z", schema_id="w0")
    schema = serial_insert(schema, "A", after_node_id="start")
    act = _activity_id(schema, "A")
    ctx = TimeContext(now=_NOW, activated_at={act: _NOW})
    view = assess(schema, act, ctx)  # no time constraint -> NONE
    assert view.criticality is TimeCriticality.NONE
    assert view.due_at is None and view.remaining_seconds is None


def test_assess_derives_band_due_and_remaining() -> None:
    schema = create_empty_schema("Z", schema_id="w1")
    schema = serial_insert(schema, "A", after_node_id="start")
    act = _activity_id(schema, "A")
    schema = set_time_constraint(schema, act, TimeConstraint(max_duration_seconds=100))
    activated = _NOW - timedelta(seconds=90)  # 90/100 consumed -> AT_RISK
    ctx = TimeContext(now=_NOW, activated_at={act: activated})
    view = assess(schema, act, ctx)
    assert view.criticality is TimeCriticality.AT_RISK
    assert view.target_seconds == 100
    assert view.elapsed_seconds == pytest.approx(90)
    assert view.remaining_seconds == pytest.approx(10)
    assert view.due_at == activated + timedelta(seconds=100)


def test_remaining_critical_path_is_forward_from_node() -> None:
    schema = create_empty_schema("Z", schema_id="w2")
    schema = serial_insert(schema, "A", after_node_id="start")
    a = _activity_id(schema, "A")
    schema = serial_insert(schema, "B", after_node_id=a)
    b = _activity_id(schema, "B")
    schema = set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=30))
    schema = set_time_constraint(schema, b, TimeConstraint(max_duration_seconds=40))
    # from A: A(30) + B(40) = 70; from B: 40.
    assert remaining_critical_path_seconds(schema, a) == pytest.approx(70)
    assert remaining_critical_path_seconds(schema, b) == pytest.approx(40)


def test_process_slack_bumps_the_band() -> None:
    schema = create_empty_schema("Z", schema_id="w3")
    schema = serial_insert(schema, "A", after_node_id="start")
    a = _activity_id(schema, "A")
    schema = serial_insert(schema, "B", after_node_id=a)
    b = _activity_id(schema, "B")
    schema = set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=100))
    schema = set_time_constraint(schema, b, TimeConstraint(max_duration_seconds=100))
    schema = set_deadline(schema, 200)
    # A just activated (rho small -> ON_TRACK on its own), instance started long
    # ago: only 50s of the 200s deadline are left, but 200s of work lie ahead
    # (A+B) -> negative process slack -> band bumped up.
    started = _NOW - timedelta(seconds=150)
    ctx = TimeContext(
        now=_NOW,
        activated_at={a: _NOW - timedelta(seconds=10)},
        started_at=started,
        deadline_seconds=200,
    )
    view = assess(schema, a, ctx)
    assert view.criticality is TimeCriticality.WARNING  # ON_TRACK bumped one step


# --- Z1: ordering of a live worklist -------------------------------------


def _two_task_schema(schema_id: str):
    schema = create_empty_schema("Liste", schema_id=schema_id)
    schema = parallel_insert(schema, ["Alt", "Neu"], after_node_id="start")
    old = _activity_id(schema, "Alt")
    new = _activity_id(schema, "Neu")
    schema = add_role(schema, "SB", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    rule = StaffRule(kind=StaffRuleKind.ROLE, ref="sb")
    schema = assign_staff_rule(schema, old, rule)
    schema = assign_staff_rule(schema, new, rule)
    return schema, old, new


def test_overdue_task_sorts_above_higher_business_priority() -> None:
    schema, old, new = _two_task_schema("w4")
    # "Neu" has the higher business priority but plenty of buffer; "Alt" is
    # overdue on its target time -> time criticality dominates (Section 5.3).
    schema = set_node_priority(
        schema, new, WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH)
    )
    schema = set_time_constraint(schema, old, TimeConstraint(max_duration_seconds=60))
    schema = set_time_constraint(schema, new, TimeConstraint(max_duration_seconds=6000))
    released = release(schema)
    instance = instantiate(released)
    ctx = TimeContext(
        now=_NOW,
        activated_at={old: _NOW - timedelta(seconds=120), new: _NOW - timedelta(seconds=1)},
    )
    tasks = open_tasks(released, instance, ctx)
    assert [t.node_id for t in tasks] == [old, new]
    assert tasks[0].time_criticality is TimeCriticality.OVERDUE
    assert tasks[0].remaining_seconds < 0


def test_business_priority_breaks_ties_within_a_band() -> None:
    schema, low, high = _two_task_schema("w5")
    schema = set_node_priority(
        schema, low, WorkItemPriority(impact=ImpactUrgency.LOW, urgency=ImpactUrgency.LOW)
    )
    schema = set_node_priority(
        schema, high, WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH)
    )
    # Both ON_TRACK (same band) -> the higher business priority wins.
    schema = set_time_constraint(schema, low, TimeConstraint(max_duration_seconds=1000))
    schema = set_time_constraint(schema, high, TimeConstraint(max_duration_seconds=1000))
    released = release(schema)
    instance = instantiate(released)
    ctx = TimeContext(
        now=_NOW,
        activated_at={low: _NOW - timedelta(seconds=1), high: _NOW - timedelta(seconds=1)},
    )
    tasks = open_tasks(released, instance, ctx)
    assert [t.node_id for t in tasks] == [high, low]


def test_without_context_behaviour_is_unchanged() -> None:
    schema, low, high = _two_task_schema("w6")
    schema = set_node_priority(
        schema, low, WorkItemPriority(impact=ImpactUrgency.LOW, urgency=ImpactUrgency.LOW)
    )
    schema = set_node_priority(
        schema, high, WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH)
    )
    schema = set_time_constraint(schema, low, TimeConstraint(max_duration_seconds=60))
    released = release(schema)
    instance = instantiate(released)
    # No TimeContext -> historic ordering by business priority, all bands NONE.
    tasks = open_tasks(released, instance)
    assert [t.node_id for t in tasks] == [high, low]
    assert all(t.time_criticality is TimeCriticality.NONE for t in tasks)


# --- Z2: model / operations round-trip -----------------------------------


def test_target_lead_seconds_round_trip_and_wellformed() -> None:
    schema = create_empty_schema("Z", schema_id="w7")
    schema = serial_insert(schema, "A", after_node_id="start")
    act = _activity_id(schema, "A")
    schema = set_time_constraint(
        schema, act, TimeConstraint(max_duration_seconds=100, target_lead_seconds=30)
    )
    assert schema.time_constraints[act].target_lead_seconds == 30
    assert [f for f in validate(schema) if f.rule.startswith("T")] == []


def test_negative_target_lead_is_rejected() -> None:
    schema = create_empty_schema("Z", schema_id="w8")
    schema = serial_insert(schema, "A", after_node_id="start")
    act = _activity_id(schema, "A")
    with pytest.raises(CorrectnessError) as exc:
        set_time_constraint(schema, act, TimeConstraint(target_lead_seconds=-1))
    assert any(f.rule == "T1" for f in exc.value.findings)
