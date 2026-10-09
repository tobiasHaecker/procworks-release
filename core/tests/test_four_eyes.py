# SPDX-License-Identifier: BUSL-1.1
"""Four eyes: nobody approves their own work via "Vorgesetzte:r".

In the demo, Tom heads "Vertrieb". He filed a leave request and was then the
only one allowed to approve it: the rule "supervisor of the performer of
'Antrag erfassen'" resolved to the manager of Tom's own unit -- Tom.

Now the supervisor is the nearest manager *above* the performer (the parent
unit's manager when the performer heads their own unit or the unit has no
manager), and a deputy substitution never hands the approval back to the
performer.

Org used below::

    leitung   (manager: boss; member: boss)
      └ team  (manager: chef; members: chef, mit)
      └ leer  (no manager; member: solo)
"""

from __future__ import annotations

from staffing import staffed

from procworks import (
    add_agent,
    add_org_unit,
    assign_staff_rule,
    complete_activity,
    create_empty_schema,
    eligible_agents,
    instantiate,
    release,
    serial_insert,
    set_agent_deputy,
    set_org_unit_manager,
)
from procworks.model import ProcessSchema, StaffRule, StaffRuleKind
from procworks.operations import update_agent


def _activity_id(schema: ProcessSchema, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _schema(*, boss_deputy: str | None = None) -> tuple[ProcessSchema, str, str]:
    """Released "Antrag" -> "Genehmigen" (supervisor of the Antrag performer).

    Anyone of the org may file the request (OR over the org-unit rules), so each
    test chooses the applicant by who completes "Antrag".
    """

    schema = create_empty_schema("VierAugen")
    schema = serial_insert(schema, "Antrag", after_node_id="start")
    antrag = _activity_id(schema, "Antrag")
    schema = serial_insert(schema, "Genehmigen", after_node_id=antrag)
    genehmigen = _activity_id(schema, "Genehmigen")

    schema = add_agent(schema, "Boss", agent_id="boss")
    schema = add_org_unit(schema, "Leitung", org_unit_id="leitung", manager_id="boss")
    # Boss is a member of the unit he heads -- like Tom in the demo.
    schema = update_agent(schema, "boss", org_unit_id="leitung")
    schema = add_org_unit(schema, "Team", parent_id="leitung", org_unit_id="team")
    schema = add_org_unit(schema, "Leer", parent_id="leitung", org_unit_id="leer")
    schema = add_agent(schema, "Chef", org_unit_id="team", agent_id="chef")
    schema = add_agent(schema, "Mitarbeiterin", org_unit_id="team", agent_id="mit")
    schema = add_agent(schema, "Solo", org_unit_id="leer", agent_id="solo")
    schema = add_agent(schema, "Vertretung", agent_id="vertretung")
    schema = set_org_unit_manager(schema, "team", "chef")
    if boss_deputy is not None:
        schema = set_agent_deputy(schema, "boss", boss_deputy)

    anyone = StaffRule(
        kind=StaffRuleKind.OR,
        operands=[
            StaffRule(kind=StaffRuleKind.ORG_UNIT, ref="team"),
            StaffRule(kind=StaffRuleKind.ORG_UNIT, ref="leer"),
        ],
    )
    schema = assign_staff_rule(schema, antrag, anyone)
    schema = assign_staff_rule(
        schema,
        genehmigen,
        StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR, ref=antrag),
    )
    return release(staffed(schema)), antrag, genehmigen


def _approvers(applicant: str, *, boss_deputy: str | None = None,
               absent: frozenset[str] = frozenset()) -> set[str]:
    rel, antrag, genehmigen = _schema(boss_deputy=boss_deputy)
    instance = complete_activity(instantiate(rel), rel, antrag, agent_id=applicant)
    return eligible_agents(rel, genehmigen, instance, absent_agents=absent)


def test_employee_is_approved_by_the_head_of_their_unit() -> None:
    assert _approvers("mit") == {"chef"}


def test_unit_head_is_approved_by_the_head_above_not_by_themself() -> None:
    """Tom (head of Vertrieb) must not approve his own leave."""

    assert _approvers("chef") == {"boss"}


def test_unit_without_head_goes_to_the_head_above() -> None:
    assert _approvers("solo") == {"boss"}


def test_top_head_has_nobody_above_and_is_not_their_own_approver() -> None:
    """Nobody above: an empty set -- shown as "Niemand zuständig"."""

    rel, antrag, genehmigen = _schema()
    # "boss" may not file via the Antrag rule; record the performer directly.
    instance = instantiate(rel)
    instance.performed_by[antrag] = "boss"
    assert eligible_agents(rel, genehmigen, instance) == set()


def test_deputy_of_the_absent_supervisor_is_never_the_applicant() -> None:
    """Boss away, Chef is Boss's deputy: Chef must still not approve his own request."""

    assert _approvers("chef", boss_deputy="chef", absent=frozenset({"boss"})) == {"boss"}


def test_deputy_of_the_absent_supervisor_still_steps_in_for_others() -> None:
    assert _approvers(
        "chef", boss_deputy="vertretung", absent=frozenset({"boss"})
    ) == {"boss", "vertretung"}
