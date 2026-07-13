# SPDX-License-Identifier: BUSL-1.1
"""Absence-driven deputy substitution (Vertretung bei Abwesenheit).

Covers the operational absence feature:

* the pure ``absent_agent_ids`` window resolution,
* absence-gated deputy substitution in ``eligible_agents``/``open_tasks`` --
  a deputy joins *only* while the agent is absent, and the agent is *never*
  removed (the no-stall safety invariant),
* the ``/agents/{id}/absences`` self-service endpoints and their effect on the
  cross-instance worklist and on task completion by the covering deputy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from procworks import (
    absent_agent_ids,
    add_agent,
    add_role,
    assign_staff_rule,
    complete_activity,
    create_empty_schema,
    eligible_agents,
    instantiate,
    release,
    serial_insert,
    set_agent_deputy,
)
from procworks import api as api_module
from procworks.api import app
from procworks.execution import ExecutionError
from procworks.model import AbsenceEntry, StaffRule, StaffRuleKind


def _activity(schema, label):
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _role_rule(role_id: str) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.ROLE, ref=role_id)


def _build_schema(schema_id: str):
    """A single interactive step assigned to role ``sb`` with a1 (deputy a2)."""

    schema = create_empty_schema("Abwesenheit", schema_id=schema_id)
    schema = serial_insert(schema, "Bearbeiten", after_node_id="start")
    act = _activity(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = add_agent(schema, "Vertreter", agent_id="a2")
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    rel = set_agent_deputy(release(schema), "a1", "a2")
    return rel, act


# --- pure window resolution ----------------------------------------------


def test_absent_agent_ids_window_is_inclusive():
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    entries = [
        AbsenceEntry(
            id="e1",
            agent_id="a1",
            start_at=now - timedelta(days=1),
            end_at=now + timedelta(days=1),
        ),
        AbsenceEntry(
            id="e2",
            agent_id="a2",
            start_at=now + timedelta(days=2),
            end_at=now + timedelta(days=3),
        ),
    ]
    # a1's window covers now, a2's is entirely in the future.
    assert absent_agent_ids(entries, now) == frozenset({"a1"})
    # Exactly on the boundary counts as absent (inclusive).
    assert absent_agent_ids(entries, now - timedelta(days=1)) == frozenset({"a1"})
    # Before any window: nobody is absent.
    assert absent_agent_ids(entries, now - timedelta(days=5)) == frozenset()


# --- absence-gated eligibility -------------------------------------------


def test_deputy_only_eligible_during_absence():
    rel, act = _build_schema("absdep")
    instance = instantiate(rel)
    assert eligible_agents(rel, act, instance) == {"a1"}
    assert eligible_agents(rel, act, instance, absent_agents=frozenset({"a1"})) == {
        "a1",
        "a2",
    }


def test_absence_without_deputy_keeps_agent_no_stall():
    """The safety invariant: an absence never empties the eligible set, even
    without a registered deputy -- the task stays with the (absent) agent."""

    schema = create_empty_schema("NoDeputy", schema_id="absnodep")
    schema = serial_insert(schema, "Bearbeiten", after_node_id="start")
    act = _activity(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Allein", role_ids=["sb"], agent_id="a1")  # no deputy
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    rel = release(schema)
    instance = instantiate(rel)

    # Absent, no deputy -> still assigned to a1 (never empty).
    assert eligible_agents(rel, act, instance, absent_agents=frozenset({"a1"})) == {"a1"}


def test_complete_by_deputy_only_during_absence():
    rel, act = _build_schema("abscomplete")
    instance = instantiate(rel)

    # Outside absence the deputy is not eligible -> completion is rejected.
    try:
        complete_activity(instance, rel, act, agent_id="a2")
        raise AssertionError("deputy must not complete when the agent is present")
    except ExecutionError:
        pass

    # During the agent's absence the deputy may complete the task.
    done = complete_activity(
        instance, rel, act, agent_id="a2", absent_agents=frozenset({"a1"})
    )
    assert done.performed_by[act] == "a2"


# --- API self-service + worklist integration -----------------------------


def _now_window() -> dict[str, str]:
    now = datetime.now(UTC)
    return {
        "start_at": (now - timedelta(hours=1)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
    }


def _reset_stores() -> None:
    api_module._store.clear()
    api_module._instances.clear()
    api_module._absence_store.clear()
    api_module._audit.clear()


def test_absence_api_makes_deputy_see_and_complete_task():
    _reset_stores()
    rel, act = _build_schema("absapi")
    api_module._store.put(rel)
    instance = instantiate(rel)
    api_module._instances.put(instance)

    with TestClient(app) as client:
        # Before recording an absence the deputy has no task.
        assert client.get("/agents/a2/tasks").json() == []

        created = client.post("/agents/a1/absences", json=_now_window())
        assert created.status_code == 201
        absence_id = created.json()["id"]
        assert created.json()["agent_id"] == "a1"

        # Now the deputy sees the task in their cross-instance worklist.
        deputy_tasks = client.get("/agents/a2/tasks").json()
        assert [t["node_id"] for t in deputy_tasks] == [act]
        assert "a2" in deputy_tasks[0]["eligible_agents"]

        # ...and may complete it while the agent is absent.
        done = client.post(
            f"/instances/{instance.id}/complete",
            json={"node_id": act, "agent_id": "a2"},
        )
        assert done.status_code == 200

        # Listing and deleting the absence.
        listed = client.get("/agents/a1/absences").json()
        assert [e["id"] for e in listed] == [absence_id]
        assert client.delete(f"/agents/a1/absences/{absence_id}").status_code == 204
        assert client.get("/agents/a1/absences").json() == []


def test_absence_api_rejects_bad_window_and_unknown_agent():
    _reset_stores()
    rel, _ = _build_schema("absbad")
    api_module._store.put(rel)

    with TestClient(app) as client:
        now = datetime.now(UTC)
        bad = client.post(
            "/agents/a1/absences",
            json={
                "start_at": now.isoformat(),
                "end_at": (now - timedelta(hours=1)).isoformat(),
            },
        )
        assert bad.status_code == 422

        unknown = client.post("/agents/ghost/absences", json=_now_window())
        assert unknown.status_code == 404
