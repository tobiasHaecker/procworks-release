# SPDX-License-Identifier: BUSL-1.1
"""An ad-hoc change cannot lift the four-eyes principle of a running case.

A modeller or admin could delete the checking step of a running case ad hoc
("anyone of the role, except whoever did step X") and insert a replacement
without that exclusion -- then the person who did step X also did the check.
The four-eyes principle holds without exception (no supervision overrides
it), so the ad-hoc path refuses to remove

* a step whose staff rule carries the principle (it excludes the performer of
  another step, or has their supervisor approve), and
* a step such a rule refers to;

with ``R1.four-eyes`` (``params``: ``step``, ``other_step``, ``relation``
``carries``/``referenced``). Whoever must lift the rule revises the process
and migrates. Steps whose rule merely *selects* the performer of another step
("the same person as in step X") are no four-eyes relation and stay
removable. Inserting and renaming cannot touch a rule and stay allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web_vm import needs_node, run_app_js

import procworks.api as api_module
from procworks import (
    StaffRule,
    StaffRuleKind,
    add_agent,
    add_org_unit,
    add_role,
    adhoc_delete_node,
    adhoc_insert_activity,
    adhoc_rename_activity,
    assign_staff_rule,
    complete_activity,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
)
from procworks.model import ProcessSchema
from procworks.validator import CorrectnessError

_CLERKS = StaffRule(kind=StaffRuleKind.ROLE, ref="sb")


def _id(schema: ProcessSchema, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _schema(check_rule: str) -> tuple[ProcessSchema, dict[str, str]]:
    """Vorbereiten -> Erfassen -> Prüfen -> Archivieren, released.

    :param check_rule: the rule of "Prüfen": ``except`` (role sb except the
        performer of Erfassen), ``supervisor`` (supervisor of that performer)
        or ``same`` (the performer of Erfassen again -- no four eyes).
    """

    schema = create_empty_schema("VierAugenAdHoc")
    schema = serial_insert(schema, "Vorbereiten", after_node_id="start")
    schema = serial_insert(schema, "Erfassen", after_node_id=_id(schema, "Vorbereiten"))
    schema = serial_insert(schema, "Prüfen", after_node_id=_id(schema, "Erfassen"))
    schema = serial_insert(schema, "Archivieren", after_node_id=_id(schema, "Prüfen"))
    labels = ("Vorbereiten", "Erfassen", "Prüfen", "Archivieren")
    ids = {label: _id(schema, label) for label in labels}
    schema = add_role(schema, "Sachbearbeitung", role_id="sb")
    schema = add_agent(schema, "Lea", agent_id="lea")
    schema = add_org_unit(schema, "Team", org_unit_id="team", manager_id="lea")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="erika", org_unit_id="team")
    schema = add_agent(schema, "Nina", role_ids=["sb"], agent_id="nina", org_unit_id="team")
    for label in ("Vorbereiten", "Erfassen", "Archivieren"):
        schema = assign_staff_rule(schema, ids[label], _CLERKS)
    performer = StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT, ref=ids["Erfassen"])
    rules = {
        "except": StaffRule(kind=StaffRuleKind.EXCEPT, operands=[_CLERKS, performer]),
        "supervisor": StaffRule(
            kind=StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR, ref=ids["Erfassen"]
        ),
        "same": performer,
    }
    schema = assign_staff_rule(schema, ids["Prüfen"], rules[check_rule])
    return release(schema), ids


def _refusal(exc: pytest.ExceptionInfo[CorrectnessError]) -> tuple[str | None, dict[str, str]]:
    [finding] = exc.value.findings
    return finding.code, dict(finding.params)


@pytest.mark.parametrize("check_rule", ["except", "supervisor"])
def test_the_step_carrying_the_rule_cannot_be_deleted(check_rule: str) -> None:
    schema, ids = _schema(check_rule)
    instance = instantiate(schema)
    instance = complete_activity(instance, schema, ids["Vorbereiten"], agent_id="erika")
    # Erfassen is open, Prüfen not reached yet -- the moment the rule could be
    # lifted before anyone did Erfassen.

    with pytest.raises(CorrectnessError) as exc:
        adhoc_delete_node(instance, schema, ids["Prüfen"])

    assert _refusal(exc) == (
        "R1.four-eyes",
        {"node": ids["Prüfen"], "step": "Prüfen", "other_step": "Erfassen",
         "relation": "carries"},
    )


def test_the_step_the_rule_refers_to_cannot_be_deleted() -> None:
    schema, ids = _schema("except")
    instance = instantiate(schema)  # Vorbereiten is open, Erfassen not reached

    with pytest.raises(CorrectnessError) as exc:
        adhoc_delete_node(instance, schema, ids["Erfassen"])

    assert _refusal(exc) == (
        "R1.four-eyes",
        {"node": ids["Erfassen"], "step": "Erfassen", "other_step": "Prüfen",
         "relation": "referenced"},
    )


def test_an_unrelated_step_is_still_deleted() -> None:
    """Negativ: Archivieren has nothing to do with the rule."""

    schema, ids = _schema("except")
    instance = instantiate(schema)

    changed = adhoc_delete_node(instance, schema, ids["Archivieren"])

    assert ids["Archivieren"] not in changed.ad_hoc_schema.nodes  # type: ignore[union-attr]


def test_a_rule_that_selects_the_same_performer_is_no_four_eyes() -> None:
    """Grenzfall: "the performer of Erfassen again" -- the check may go."""

    schema, ids = _schema("same")
    instance = instantiate(schema)
    instance = complete_activity(instance, schema, ids["Vorbereiten"], agent_id="erika")

    changed = adhoc_delete_node(instance, schema, ids["Prüfen"])

    assert ids["Prüfen"] not in changed.ad_hoc_schema.nodes  # type: ignore[union-attr]


def test_insert_and_rename_stay_allowed() -> None:
    """Negativ: they cannot change a rule, so they are not restricted."""

    schema, ids = _schema("except")
    instance = instantiate(schema)
    instance = adhoc_rename_activity(instance, schema, ids["Prüfen"], "Prüfen (neu)")
    instance = adhoc_insert_activity(
        instance, schema, ids["Erfassen"], "Nachfragen", staff_rule=_CLERKS
    )
    assert "Nachfragen" in {n.label for n in instance.ad_hoc_schema.nodes.values()}  # type: ignore[union-attr]


def test_the_api_refuses_with_the_code_and_keeps_the_case() -> None:
    client = TestClient(api_module.app)
    schema, ids = _schema("except")
    api_module._store.put(schema)
    instance = instantiate(schema)
    instance = complete_activity(instance, schema, ids["Vorbereiten"], agent_id="erika")
    api_module._instances.put(instance)

    resp = client.post(
        f"/instances/{instance.id}/adhoc/delete",
        json={"node_id": ids["Prüfen"], "reason": "Prüfung entfällt"},
    )

    assert resp.status_code == 422
    [finding] = resp.json()["detail"]["findings"]
    assert (finding["code"], finding["params"]["relation"]) == ("R1.four-eyes", "carries")
    stored = api_module._instances.get(instance.id)
    assert stored is not None and stored.ad_hoc_schema is None


@needs_node
def test_the_client_explains_and_does_not_offer_the_step(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const carries = findingText({ code: "R1.four-eyes", params: { node: "p", step: "Prüfen",
  other_step: "Erfassen", relation: "carries" } }, { withHint: true });
const referenced = findingText({ code: "R1.four-eyes", params: { node: "e", step: "Erfassen",
  other_step: "Prüfen", relation: "referenced" } });
const rules = {
  e: { kind: "ROLE", ref: "sb", operands: [] },
  p: { kind: "EXCEPT", operands: [{ kind: "ROLE", ref: "sb", operands: [] },
    { kind: "NODE_PERFORMING_AGENT", ref: "e", operands: [] }] },
  s: { kind: "NODE_PERFORMING_AGENT", ref: "e", operands: [] },
  a: { kind: "ROLE", ref: "sb", operands: [] },
};
const schema = { staff_rules: rules, nodes: { e: { id: "e", label: "Erfassen" },
  p: { id: "p", label: "Prüfen" }, s: { id: "s", label: "Gleich" },
  a: { id: "a", label: "Archiv" } } };
return { carries, referenced,
  why: ["e", "p", "s", "a"].map((n) => adhocDeleteBlockReason(schema, n)) };
""",
        tmp_path,
    )
    assert "„Prüfen“" in res["carries"] and "Vier-Augen" in res["carries"]
    assert "Revision" in res["carries"]
    assert "„Erfassen“" in res["referenced"] and "„Prüfen“" in res["referenced"]
    e, p, s, a = res["why"]
    assert e and "Vier-Augen" in e and p and "Vier-Augen" in p
    assert s is None and a is None
