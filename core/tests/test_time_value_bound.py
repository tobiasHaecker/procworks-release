# SPDX-License-Identifier: BUSL-1.1
"""Every modelled time value is bounded to 100 years (T1/T3), and a larger
stored value never breaks a worklist.

Due instants are computed as ``datetime + timedelta``. A target time of
``1e12`` seconds lies beyond the year 9999, the addition raised
``OverflowError`` -- inside the lazy escalation sweep that runs before every
worklist read, so a single such step answered every agent's worklist with a
500. The validator now refuses values above ``MAX_TIME_SECONDS`` with their
own code; schemas stored before that bound still exist, so the runtime treats
an unrepresentable due instant as "no due date" (logged) instead of raising.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from planted import plant

from procworks import (
    EscalationKind,
    EscalationPolicy,
    EscalationStage,
    StaffRule,
    StaffRuleKind,
    TimeCriticality,
    add_agent,
    add_role,
    assign_staff_rule,
    create_empty_schema,
    export_bpmn,
    import_bpmn,
    serial_insert,
    set_deadline,
    set_escalation_policy,
    set_time_constraint,
    validate,
    worklist_priority,
)
from procworks import api as api_module
from procworks.model import MAX_TIME_SECONDS, TimeConstraint
from procworks.validator import CorrectnessError

client = TestClient(api_module.app, raise_server_exceptions=False)

HUNDRED_YEARS = 100 * 365.25 * 86400


def _chain(schema_id: str):
    schema = create_empty_schema("Zeitgrenze", schema_id=schema_id)
    schema = serial_insert(schema, "A", after_node_id="start")
    a = next(n.id for n in schema.nodes.values() if n.label == "A")
    return schema, a


def _codes(exc: pytest.ExceptionInfo[CorrectnessError], rule: str) -> list[str]:
    return [f.code for f in exc.value.findings if f.rule == rule]


def test_the_bound_is_one_hundred_years() -> None:
    assert MAX_TIME_SECONDS == HUNDRED_YEARS == 3_155_760_000


# --- T1: deadline, processing duration, reaction time ----------------------


@pytest.mark.parametrize("value", [0, 1, MAX_TIME_SECONDS], ids=["zero", "one", "bound"])
def test_values_up_to_and_including_the_bound_are_accepted(value: float) -> None:
    schema, a = _chain(f"tb-ok-{int(value)}")
    schema = set_time_constraint(
        schema, a, TimeConstraint(max_duration_seconds=value, target_lead_seconds=value)
    )
    schema = set_deadline(schema, MAX_TIME_SECONDS)
    assert [f for f in validate(schema) if f.rule in ("T1", "T2")] == []


def test_duration_above_the_bound_is_refused_for_that_reason() -> None:
    schema, a = _chain("tb-dur")
    with pytest.raises(CorrectnessError) as exc:
        set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=MAX_TIME_SECONDS + 1))
    assert _codes(exc, "T1") == ["T1.duration-too-large"]
    assert exc.value.findings[0].params["node"] == a


def test_reaction_time_above_the_bound_is_refused_for_that_reason() -> None:
    schema, a = _chain("tb-lead")
    with pytest.raises(CorrectnessError) as exc:
        set_time_constraint(schema, a, TimeConstraint(target_lead_seconds=1e12))
    assert _codes(exc, "T1") == ["T1.lead-too-large"]


def test_deadline_above_the_bound_is_refused_for_that_reason() -> None:
    schema, _ = _chain("tb-dl")
    with pytest.raises(CorrectnessError) as exc:
        set_deadline(schema, 1e300)
    assert _codes(exc, "T1") == ["T1.deadline-too-large"]


def test_a_negative_value_keeps_its_own_reason() -> None:
    """The new bound adds a reason, it does not replace the sign check."""

    schema, a = _chain("tb-neg")
    with pytest.raises(CorrectnessError) as exc:
        set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=-1))
    assert _codes(exc, "T1") == ["T1.negative-duration"]


def test_bpmn_import_refuses_a_too_large_value() -> None:
    """The import builds the schema from outside and carries the rule only
    through ``validate()`` -- so the bound must hold there as well."""

    schema, a = _chain("tb-bpmn")
    schema = set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=3600))
    exported = export_bpmn(schema)
    # The export spells the value as JSON inside the extension.
    assert '"max_duration_seconds": 3600.0' in exported
    xml = exported.replace('"max_duration_seconds": 3600.0', '"max_duration_seconds": 1e12')
    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(xml, schema_id="tb-bpmn-imp")
    assert _codes(exc, "T1") == ["T1.duration-too-large"]


# --- T3: escalation offsets ------------------------------------------------


def _escalation_schema(schema_id: str):
    schema, a = _chain(schema_id)
    schema = add_role(schema, "SB", role_id="r-sb")
    schema = add_agent(schema, "Anna", role_ids=["r-sb"], agent_id="a-anna")
    schema = assign_staff_rule(schema, a, StaffRule(kind=StaffRuleKind.ROLE, ref="r-sb"))
    schema = set_time_constraint(schema, a, TimeConstraint(target_lead_seconds=60))
    return schema, a


def _policy(after: float) -> EscalationPolicy:
    return EscalationPolicy(
        stages=[
            EscalationStage(
                after_seconds=after,
                kind=EscalationKind.HIERARCHICAL,
                rule=StaffRule(kind=StaffRuleKind.ROLE, ref="r-sb"),
            )
        ]
    )


def test_escalation_offset_at_the_bound_is_accepted() -> None:
    schema, a = _escalation_schema("tb-esc-ok")
    schema = set_escalation_policy(schema, a, _policy(MAX_TIME_SECONDS))
    assert [f for f in validate(schema) if f.rule == "T3"] == []


def test_escalation_offset_above_the_bound_is_refused_for_that_reason() -> None:
    schema, a = _escalation_schema("tb-esc-big")
    with pytest.raises(CorrectnessError) as exc:
        set_escalation_policy(schema, a, _policy(MAX_TIME_SECONDS + 1))
    assert _codes(exc, "T3") == ["T3.offset-too-large"]


def test_api_refuses_the_value_with_the_code_and_leaves_the_schema_alone() -> None:
    sid = client.post("/schemas", json={"name": "Zeitgrenze-API"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node = next(nid for nid, n in nodes.items() if n.get("label") == "A")
    resp = client.post(
        f"/schemas/{sid}/time-constraint",
        json={"node_id": node, "constraint": {"max_duration_seconds": 1e12}},
    )
    assert resp.status_code == 422
    assert "T1.duration-too-large" in {f["code"] for f in resp.json()["detail"]["findings"]}
    assert client.get(f"/schemas/{sid}").json()["time_constraints"] == {}


# --- runtime: values stored before the bound ---------------------------------


def test_shifted_answers_none_for_an_unrepresentable_instant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    origin = datetime(2026, 10, 10, tzinfo=UTC)
    assert worklist_priority.shifted(origin, 60, "x") == origin + timedelta(seconds=60)
    with caplog.at_level(logging.WARNING, logger="procworks.worklist_priority"):
        assert worklist_priority.shifted(origin, 1e12, "node n_9") is None
        assert worklist_priority.shifted(origin, 1e300, "node n_9") is None
    assert "node n_9" in caplog.text and "no due date" in caplog.text


def test_assess_treats_a_stored_huge_target_as_no_due_date() -> None:
    schema, a = _chain("tb-assess")
    # Stored before the bound: bypasses the operation on purpose.
    schema.time_constraints[a] = TimeConstraint(target_lead_seconds=1e12)
    now = datetime(2026, 10, 10, tzinfo=UTC)
    ctx = worklist_priority.TimeContext(
        now=now,
        activated_at={a: now - timedelta(seconds=10)},
        started_at=now - timedelta(seconds=10),
        deadline_seconds=1e300,
    )
    view = worklist_priority.assess(schema, a, ctx)
    assert view.criticality is TimeCriticality.NONE
    assert view.due_at is None


def _released_with_escalation(name: str) -> tuple[str, str, str]:
    """Released schema (Anna, reaction 60 s, stage after 0 s) + one instance."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node = next(nid for nid, n in nodes.items() if n.get("label") == "A")
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "r-sb"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Anna", "role_ids": ["r-sb"], "agent_id": "a-anna"},
    )
    role_rule = {"kind": "ROLE", "ref": "r-sb"}
    client.post(f"/schemas/{sid}/staff-rule", json={"node_id": node, "rule": role_rule})
    client.post(
        f"/schemas/{sid}/time-constraint",
        json={"node_id": node, "constraint": {"target_lead_seconds": 60}},
    )
    resp = client.post(
        f"/schemas/{sid}/escalation-policy",
        json={
            "node_id": node,
            "policy": {
                "stages": [
                    {"after_seconds": 0, "kind": "HIERARCHICAL", "rule": role_rule}
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    return sid, node, iid


def test_a_stored_huge_value_breaks_neither_worklists_nor_the_sweep() -> None:
    """One legacy schema must not take the other instances down with it."""

    bad_sid, bad_node, bad_iid = _released_with_escalation("Altbestand-Zeitwert")
    good_sid, good_node, good_iid = _released_with_escalation("Gesunder-Zeitwert")
    original = api_module._store.get(bad_sid)
    assert original is not None
    legacy = original.model_copy(deep=True)
    legacy.time_constraints[bad_node] = TimeConstraint(target_lead_seconds=1e12)
    plant(api_module._store, legacy)  # as if stored before the bound existed
    try:
        later = datetime.now(UTC) + timedelta(seconds=120)
        # The healthy instance escalates; the legacy one is skipped, not raised.
        assert api_module._escalation_sweep(now=later) >= 1
        good = client.get(f"/instances/{good_iid}").json()
        assert good["escalated_stages"].get(good_node) == 1
        bad = client.get(f"/instances/{bad_iid}").json()
        assert bad["escalated_stages"].get(bad_node, 0) == 0

        tasks = client.get(f"/instances/{bad_iid}/tasks")
        assert tasks.status_code == 200, tasks.text
        assert [t["time_criticality"] for t in tasks.json() if t["node_id"] == bad_node] == ["NONE"]
        mine = client.get("/agents/a-anna/tasks")
        assert mine.status_code == 200, mine.text
        assert {bad_iid, good_iid} <= {t["instance_id"] for t in mine.json()}
        assert client.post("/admin/escalations/sweep").status_code == 200
    finally:
        plant(api_module._store, original)
