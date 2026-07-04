# SPDX-License-Identifier: BUSL-1.1
"""Tests for runtime staff resolution and the agent task list (Bearbeiter-Aufgabenliste).

These cover the concrete (runtime) resolution of staff rules against a running
instance, deputy expansion, recursive org-unit assignment, NODE_PERFORMING_AGENT
binding via instance.performed_by, open_tasks aggregation, and the org master-data
validations (manager / deputy, rule Z1) plus eligibility enforcement on complete.
"""

from __future__ import annotations

import pytest

from procworks import (
    add_agent,
    add_org_unit,
    add_role,
    assign_staff_rule,
    complete_activity,
    create_empty_schema,
    eligible_agents,
    instantiate,
    new_revision,
    open_tasks,
    release,
    serial_insert,
    set_agent_deputy,
    set_org_unit_manager,
    set_org_unit_parent,
    validate,
)
from procworks.execution import ExecutionError
from procworks.model import InstanceState, StaffRule, StaffRuleKind
from procworks.validator import CorrectnessError


def _activity_id(schema, label):
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _role_rule(role_id: str) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.ROLE, ref=role_id)


def _single_activity_schema(schema_id="asg"):
    schema = create_empty_schema("Aufgaben", schema_id=schema_id)
    schema = serial_insert(schema, "Bearbeiten", after_node_id="start")
    return schema


# --- concrete resolution --------------------------------------------------


def test_eligible_agents_role_union():
    schema = _single_activity_schema()
    act = _activity_id(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = add_agent(schema, "Max", role_ids=["sb"], agent_id="a2")
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    instance = instantiate(release(schema))

    assert eligible_agents(release(schema), act, instance) == {"a1", "a2"}


def test_eligible_agents_org_unit_recursive():
    schema = _single_activity_schema("asgou")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_org_unit(schema, "Abteilung", org_unit_id="abt")
    schema = add_org_unit(schema, "Team", parent_id="abt", org_unit_id="team")
    schema = add_agent(schema, "Leiterin", org_unit_id="abt", agent_id="a1")
    schema = add_agent(schema, "Mitglied", org_unit_id="team", agent_id="a2")
    rule = StaffRule(kind=StaffRuleKind.ORG_UNIT, ref="abt", recursive=True)
    schema = assign_staff_rule(schema, act, rule)
    rel = release(schema)
    instance = instantiate(rel)

    assert eligible_agents(rel, act, instance) == {"a1", "a2"}


def test_eligible_agents_org_unit_non_recursive():
    schema = _single_activity_schema("asgou2")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_org_unit(schema, "Abteilung", org_unit_id="abt")
    schema = add_org_unit(schema, "Team", parent_id="abt", org_unit_id="team")
    schema = add_agent(schema, "Leiterin", org_unit_id="abt", agent_id="a1")
    schema = add_agent(schema, "Mitglied", org_unit_id="team", agent_id="a2")
    rule = StaffRule(kind=StaffRuleKind.ORG_UNIT, ref="abt", recursive=False)
    schema = assign_staff_rule(schema, act, rule)
    rel = release(schema)
    instance = instantiate(rel)

    assert eligible_agents(rel, act, instance) == {"a1"}


def test_eligible_agents_deputy_transitive():
    schema = _single_activity_schema("asgdep")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = add_agent(schema, "Vertreter1", agent_id="a2")
    schema = add_agent(schema, "Vertreter2", agent_id="a3")
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    rel = release(schema)
    # Vertreterregelung erst zur Laufzeit (Stammdaten, auf RELEASED erlaubt)
    rel = set_agent_deputy(rel, "a1", "a2")
    rel = set_agent_deputy(rel, "a2", "a3")
    instance = instantiate(rel)

    assert eligible_agents(rel, act, instance) == {"a1", "a2", "a3"}


def test_eligible_agents_node_performing_agent():
    schema = create_empty_schema("NPA", schema_id="asgnpa")
    schema = serial_insert(schema, "Pruefen", after_node_id="start")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = _activity_id(schema, "Erfassen")
    pruefen = _activity_id(schema, "Pruefen")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = add_agent(schema, "Max", role_ids=["sb"], agent_id="a2")
    schema = assign_staff_rule(schema, erfassen, _role_rule("sb"))
    schema = assign_staff_rule(
        schema, pruefen, StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT, ref=erfassen)
    )
    rel = release(schema)
    instance = instantiate(rel)

    # noch niemand hat Erfassen ausgefuehrt -> Pruefen hat keine Bearbeiter
    assert eligible_agents(rel, pruefen, instance) == set()

    instance = complete_activity(instance, rel, erfassen, agent_id="a1")
    assert instance.performed_by[erfassen] == "a1"
    assert eligible_agents(rel, pruefen, instance) == {"a1"}


def test_eligible_agents_and_or_except():
    schema = _single_activity_schema("asgbool")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_role(schema, "A", role_id="ra")
    schema = add_role(schema, "B", role_id="rb")
    schema = add_agent(schema, "Beide", role_ids=["ra", "rb"], agent_id="a1")
    schema = add_agent(schema, "NurA", role_ids=["ra"], agent_id="a2")
    schema = add_agent(schema, "NurB", role_ids=["rb"], agent_id="a3")
    rel = release(
        assign_staff_rule(
            schema,
            act,
            StaffRule(kind=StaffRuleKind.AND, operands=[_role_rule("ra"), _role_rule("rb")]),
        )
    )
    instance = instantiate(rel)
    assert eligible_agents(rel, act, instance) == {"a1"}

    rel = release(
        assign_staff_rule(
            schema,
            act,
            StaffRule(kind=StaffRuleKind.OR, operands=[_role_rule("ra"), _role_rule("rb")]),
        )
    )
    instance = instantiate(rel)
    assert eligible_agents(rel, act, instance) == {"a1", "a2", "a3"}

    rel = release(
        assign_staff_rule(
            schema,
            act,
            StaffRule(kind=StaffRuleKind.EXCEPT, operands=[_role_rule("ra"), _role_rule("rb")]),
        )
    )
    instance = instantiate(rel)
    assert eligible_agents(rel, act, instance) == {"a2"}


# --- open_tasks -----------------------------------------------------------


def test_open_tasks_lists_activated_activity_with_rule():
    schema = _single_activity_schema("asgtasks")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    rel = release(schema)
    instance = instantiate(rel)

    tasks = open_tasks(rel, instance)
    assert len(tasks) == 1
    assert tasks[0].node_id == act
    assert tasks[0].eligible_agents == ["a1"]
    # The task carries the schema id and its revision (version) so worklists can
    # show which revision a task belongs to.
    assert tasks[0].schema_id == instance.schema_id
    assert tasks[0].schema_version == rel.version == 1


def test_open_tasks_carry_revision_version():
    # A revision keeps the same name but gets a fresh id and an incremented
    # version; its open tasks must report that revision's version (here 2).
    schema = _single_activity_schema("asgrev")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    rel_v1 = release(schema)
    rev_v2 = release(new_revision(rel_v1))
    assert rev_v2.version == 2

    instance = instantiate(rev_v2)
    tasks = open_tasks(rev_v2, instance)
    assert len(tasks) == 1
    assert tasks[0].schema_version == 2
    assert tasks[0].schema_id == instance.schema_id


def test_open_tasks_empty_when_no_rule():
    schema = _single_activity_schema("asgnorule")
    rel = release(schema)
    instance = instantiate(rel)
    assert open_tasks(rel, instance) == []


def test_open_tasks_empty_when_instance_completed():
    schema = _single_activity_schema("asgdone")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    rel = release(schema)
    instance = instantiate(rel)
    instance = complete_activity(instance, rel, act, agent_id="a1")
    assert instance.state is InstanceState.COMPLETED
    assert open_tasks(rel, instance) == []


def test_open_tasks_deputy_receives_task():
    schema = _single_activity_schema("asgdeptask")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = add_agent(schema, "Vertreter", agent_id="a2")
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    rel = set_agent_deputy(release(schema), "a1", "a2")
    instance = instantiate(rel)

    tasks = open_tasks(rel, instance)
    assert tasks[0].eligible_agents == ["a1", "a2"]


# --- org master data validation (Z1) --------------------------------------


def test_z1_rejects_unknown_manager():
    schema = create_empty_schema("Mgr", schema_id="zmgr")
    schema = add_org_unit(schema, "Abteilung", org_unit_id="abt")
    with pytest.raises(CorrectnessError) as exc:
        set_org_unit_manager(schema, "abt", "ghost")
    assert any(f.rule == "Z1" for f in exc.value.findings)


def test_z1_rejects_self_deputy():
    schema = create_empty_schema("Dep", schema_id="zdep")
    schema = add_agent(schema, "Erika", agent_id="a1")
    with pytest.raises(CorrectnessError) as exc:
        set_agent_deputy(schema, "a1", "a1")
    assert any(f.rule == "Z1" for f in exc.value.findings)


def test_z1_rejects_unknown_deputy():
    schema = create_empty_schema("Dep2", schema_id="zdep2")
    schema = add_agent(schema, "Erika", agent_id="a1")
    with pytest.raises(CorrectnessError) as exc:
        set_agent_deputy(schema, "a1", "ghost")
    assert any(f.rule == "Z1" for f in exc.value.findings)


def test_set_manager_and_deputy_on_released_schema():
    schema = _single_activity_schema("asgrel")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_org_unit(schema, "Abteilung", org_unit_id="abt")
    schema = add_agent(schema, "Leiterin", org_unit_id="abt", agent_id="a1")
    schema = add_agent(schema, "Vertreter", org_unit_id="abt", agent_id="a2")
    schema = assign_staff_rule(
        schema, act, StaffRule(kind=StaffRuleKind.ORG_UNIT, ref="abt")
    )
    rel = release(schema)
    rel = set_org_unit_manager(rel, "abt", "a1")
    rel = set_agent_deputy(rel, "a1", "a2")
    assert rel.org_model.org_units["abt"].manager_id == "a1"
    assert rel.org_model.agents["a1"].deputy_id == "a2"
    assert validate(rel) == []


def test_set_org_unit_parent_moves_unit():
    schema = create_empty_schema("Move", schema_id="mv")
    schema = add_org_unit(schema, "Bereich", org_unit_id="bereich")
    schema = add_org_unit(schema, "Team", org_unit_id="team")
    assert schema.org_model.org_units["team"].parent_id is None
    schema = set_org_unit_parent(schema, "team", "bereich")
    assert schema.org_model.org_units["team"].parent_id == "bereich"
    # Clearing the parent lifts the unit back to the top level.
    schema = set_org_unit_parent(schema, "team", None)
    assert schema.org_model.org_units["team"].parent_id is None


def test_set_org_unit_parent_rejects_cycle():
    schema = create_empty_schema("Cycle", schema_id="cyc")
    schema = add_org_unit(schema, "Bereich", org_unit_id="bereich")
    schema = add_org_unit(schema, "Team", parent_id="bereich", org_unit_id="team")
    # Making the ancestor a child of its own descendant must be rejected.
    with pytest.raises(CorrectnessError) as exc:
        set_org_unit_parent(schema, "bereich", "team")
    assert any(f.rule == "OP" for f in exc.value.findings)


def test_set_org_unit_parent_rejects_self():
    schema = create_empty_schema("Self", schema_id="self")
    schema = add_org_unit(schema, "Bereich", org_unit_id="bereich")
    with pytest.raises(CorrectnessError) as exc:
        set_org_unit_parent(schema, "bereich", "bereich")
    assert any(f.rule == "OP" for f in exc.value.findings)


def test_set_org_unit_parent_on_released_schema():
    schema = _single_activity_schema("mvrel")
    schema = add_org_unit(schema, "Bereich", org_unit_id="bereich")
    schema = add_org_unit(schema, "Team", org_unit_id="team")
    rel = release(schema)
    rel = set_org_unit_parent(rel, "team", "bereich")
    assert rel.org_model.org_units["team"].parent_id == "bereich"
    assert validate(rel) == []



# --- eligibility enforcement on complete ----------------------------------


def test_complete_rejects_ineligible_agent():
    schema = _single_activity_schema("asgenf")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = add_agent(schema, "Fremd", agent_id="a2")
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    rel = release(schema)
    instance = instantiate(rel)
    with pytest.raises(ExecutionError):
        complete_activity(instance, rel, act, agent_id="a2")


def test_complete_without_agent_id_skips_eligibility():
    schema = _single_activity_schema("asgnoagent")
    act = _activity_id(schema, "Bearbeiten")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = assign_staff_rule(schema, act, _role_rule("sb"))
    rel = release(schema)
    instance = instantiate(rel)
    instance = complete_activity(instance, rel, act)
    assert instance.state is InstanceState.COMPLETED
