# SPDX-License-Identifier: BUSL-1.1
"""Z2 must prove an EXCEPT rule empty when the org model alone decides it.

``_possible_agents`` over-approximated ``EXCEPT(left, right)`` by ``left`` --
"removing agents cannot add any". True, but an over-approximation can never
show that a set is empty: ``EXCEPT(ROLE sb, ROLE sb)`` passed Z2, the schema was
released, and the step stood in nobody's worklist at runtime (Validierung aus
Außensicht 2026-09-25, VAL-02).

The fix subtracts the right operand when it is statically exact (ROLE, ORG_UNIT,
AGENT and combinations of them). A runtime leaf on the right (a performer
reference) keeps the old bound -- emptiness is then not provable, and the
counter-examples below make sure Z2 does not start rejecting valid four-eyes
rules. Every negative test pins the rule (``[Z2]`` / ``Z2.nobody``).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from procworks import (
    add_agent,
    add_org_unit,
    add_role,
    assign_staff_rule,
    create_empty_schema,
    serial_insert,
    validate,
)
from procworks.api import app
from procworks.model import ProcessSchema, StaffRule, StaffRuleKind
from procworks.validator import CorrectnessError

client = TestClient(app)


def _role(ref: str) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.ROLE, ref=ref)


def _agent(ref: str) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.AGENT, ref=ref)


def _except(left: StaffRule, right: StaffRule) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.EXCEPT, operands=[left, right])


def _schema(*holders: str) -> tuple[ProcessSchema, str, str]:
    """start → "Erfassen" → "Prüfen" → end; role ``sb`` held by ``holders``.

    ``a9`` holds only role ``other``. Returns ``(schema, erfassen, pruefen)``.
    """

    schema = create_empty_schema("Z2 Except", schema_id="z2except")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = serial_insert(schema, "Prüfen", after_node_id=erfassen)
    pruefen = next(n.id for n in schema.nodes.values() if n.label == "Prüfen")
    schema = add_role(schema, "Sachbearbeitung", role_id="sb")
    schema = add_role(schema, "Andere", role_id="other")
    for agent_id in holders:
        schema = add_agent(schema, agent_id, role_ids=["sb"], agent_id=agent_id)
    schema = add_agent(schema, "Außen", role_ids=["other"], agent_id="a9")
    schema = assign_staff_rule(schema, erfassen, _role("sb"))
    return schema, erfassen, pruefen


def _assert_z2(exc: pytest.ExceptionInfo[CorrectnessError], node_id: str) -> None:
    assert [(f.rule, f.code, f.node_id) for f in exc.value.findings] == [
        ("Z2", "Z2.nobody", node_id)
    ]


# --- provably empty: rejected -------------------------------------------------


def test_except_of_a_role_with_itself_is_rejected() -> None:
    schema, _, pruefen = _schema("a1", "a2")

    with pytest.raises(CorrectnessError, match=r"\[Z2\]") as exc:
        assign_staff_rule(schema, pruefen, _except(_role("sb"), _role("sb")))

    _assert_z2(exc, pruefen)


def test_except_removing_the_only_holder_is_rejected() -> None:
    schema, _, pruefen = _schema("a1")

    with pytest.raises(CorrectnessError, match=r"\[Z2\]") as exc:
        assign_staff_rule(schema, pruefen, _except(_role("sb"), _agent("a1")))

    _assert_z2(exc, pruefen)


def test_nested_exact_combination_is_evaluated_exactly() -> None:
    # sb minus (sb OR other) is empty, whatever the org looks like.
    schema, _, pruefen = _schema("a1", "a2")
    right = StaffRule(kind=StaffRuleKind.OR, operands=[_role("sb"), _role("other")])

    with pytest.raises(CorrectnessError, match=r"\[Z2\]") as exc:
        assign_staff_rule(schema, pruefen, _except(_role("sb"), right))

    _assert_z2(exc, pruefen)


def test_except_on_an_org_unit_emptied_by_a_role_is_rejected() -> None:
    schema, _, pruefen = _schema("a1")
    schema = add_org_unit(schema, "Einkauf", org_unit_id="ek")
    schema = add_agent(schema, "Ina", role_ids=["sb"], agent_id="a3", org_unit_id="ek")
    rule = _except(StaffRule(kind=StaffRuleKind.ORG_UNIT, ref="ek"), _role("sb"))

    with pytest.raises(CorrectnessError, match=r"\[Z2\]") as exc:
        assign_staff_rule(schema, pruefen, rule)

    _assert_z2(exc, pruefen)


# --- not empty, or not provably empty: accepted (counter-examples) ------------


def test_except_leaving_another_holder_is_accepted() -> None:
    schema, _, pruefen = _schema("a1", "a2")

    schema = assign_staff_rule(schema, pruefen, _except(_role("sb"), _agent("a1")))

    assert validate(schema) == []


def test_four_eyes_with_a_runtime_performer_stays_accepted() -> None:
    # EXCEPT(sb, performer of "Erfassen") with a single sb holder *may* be empty
    # at runtime -- but only if that holder did "Erfassen", which the model
    # cannot know. Z2 must not guess; the old upper bound stays in force here.
    schema, erfassen, pruefen = _schema("a1")
    performer = StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT, ref=erfassen)

    schema = assign_staff_rule(schema, pruefen, _except(_role("sb"), performer))

    assert validate(schema) == []


# --- the same rule across models: an org change that empties it --------------


def test_shared_org_change_that_empties_a_released_except_rule_is_rejected() -> None:
    org_id = "org_z2_except"
    client.post("/org-models", json={"name": "Org", "org_model_id": org_id})
    client.post(f"/org-models/{org_id}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(f"/org-models/{org_id}/roles", json={"name": "Gesperrt", "role_id": "lock"})
    for agent_id, roles in (("a1", ["sb"]), ("a2", ["sb", "lock"])):
        client.post(
            f"/org-models/{org_id}/agents",
            json={"name": agent_id, "role_ids": roles, "agent_id": agent_id},
        )
    sid = client.post("/schemas", json={"name": "Z2 Except geteilt"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Prüfen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Prüfen")
    linked = client.post(f"/schemas/{sid}/org-model", json={"org_model_id": org_id})
    assert linked.status_code == 200
    rule = {"kind": "EXCEPT", "operands": [
        {"kind": "ROLE", "ref": "sb"}, {"kind": "ROLE", "ref": "lock"}]}
    assert client.post(
        f"/schemas/{sid}/staff-rule", json={"node_id": act, "rule": rule}
    ).status_code == 200
    assert client.post(f"/schemas/{sid}/release", json={}).status_code == 200

    # Locking a1 as well leaves nobody who holds sb but not lock.
    resp = client.patch(f"/org-models/{org_id}/agents/a1", json={"role_ids": ["sb", "lock"]})

    assert resp.status_code == 422
    assert "Z2" in resp.text
    assert client.get(f"/org-models/{org_id}").json()["agents"]["a1"]["role_ids"] == ["sb"]
