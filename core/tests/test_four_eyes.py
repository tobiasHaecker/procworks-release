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

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from staffing import staffed

from procworks import (
    EscalationKind,
    EscalationPolicy,
    EscalationStage,
    TimeConstraint,
    add_agent,
    add_org_unit,
    add_role,
    assign_staff_rule,
    claim_activity,
    complete_activity,
    create_empty_schema,
    eligible_agents,
    instantiate,
    release,
    serial_insert,
    set_agent_deputy,
    set_escalation_policy,
    set_org_unit_manager,
    set_time_constraint,
)
from procworks import api as api_module
from procworks.api import app
from procworks.assignment import unstaffed_steps
from procworks.execution import ExecutionError
from procworks.model import ProcessInstance, ProcessSchema, StaffRule, StaffRuleKind
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


# --- "anyone of the role, except whoever did step X" -------------------------
#
# The same separation of duties written as an exclusion: "Prüfen" may be done
# by every clerk except the person who did "Erfassen". A deputy edge, a deputy
# chain or a fired escalation stage must never hand "Prüfen" back to that
# person -- the actual performer counts, also when they acted as a deputy.
#
# Org used below: erika and nina are clerks (role ``sb``); tom is erika's
# deputy without the role; lea is tom's deputy (chain).


def _except_schema(*, escalation: StaffRule | None = None) -> tuple[ProcessSchema, str, str]:
    """Released "Erfassen" (role sb) -> "Prüfen" (role sb except performer of Erfassen).

    :param escalation: when given, "Prüfen" gets a deadline and one FUNCTIONAL
        escalation stage with this target rule.
    :returns: the released schema and the ids of both steps.
    """

    schema = create_empty_schema("VierAugenAusnahme")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = _activity_id(schema, "Erfassen")
    schema = serial_insert(schema, "Prüfen", after_node_id=erfassen)
    pruefen = _activity_id(schema, "Prüfen")
    schema = add_role(schema, "Sachbearbeitung", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="erika")
    schema = add_agent(schema, "Nina", role_ids=["sb"], agent_id="nina")
    schema = add_agent(schema, "Tom", agent_id="tom")
    schema = add_agent(schema, "Lea", agent_id="lea")
    schema = set_agent_deputy(schema, "erika", "tom")
    schema = set_agent_deputy(schema, "tom", "lea")
    clerks = StaffRule(kind=StaffRuleKind.ROLE, ref="sb")
    schema = assign_staff_rule(schema, erfassen, clerks)
    schema = assign_staff_rule(
        schema,
        pruefen,
        StaffRule(
            kind=StaffRuleKind.EXCEPT,
            operands=[
                clerks,
                StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT, ref=erfassen),
            ],
        ),
    )
    if escalation is not None:
        schema = set_time_constraint(
            schema, pruefen, TimeConstraint(max_duration_seconds=3600)
        )
        schema = set_escalation_policy(
            schema,
            pruefen,
            EscalationPolicy(stages=[EscalationStage(
                after_seconds=0, kind=EscalationKind.FUNCTIONAL, rule=escalation,
            )]),
        )
    return release(schema), erfassen, pruefen


def _after_erfassen(
    performer: str, absent: frozenset[str], *, escalation: StaffRule | None = None
) -> tuple[ProcessSchema, ProcessInstance, str]:
    """Run "Erfassen" by ``performer`` (``absent`` at that moment) on a fresh case."""

    rel, erfassen, pruefen = _except_schema(escalation=escalation)
    instance = complete_activity(
        instantiate(rel), rel, erfassen, agent_id=performer, absent_agents=absent
    )
    return rel, instance, pruefen


def test_deputy_who_did_the_step_is_not_offered_the_check() -> None:
    """Tom did "Erfassen" for absent Erika -- "Prüfen" must not come back to him."""

    rel, instance, pruefen = _after_erfassen("tom", frozenset({"erika"}))
    eligible = eligible_agents(rel, pruefen, instance, absent_agents=frozenset({"erika"}))
    assert "tom" not in eligible
    assert eligible == {"erika", "nina"}


def test_deputy_who_did_the_step_cannot_claim_or_complete_the_check() -> None:
    """The engine refuses -- with the four-eyes reason and the step it refers to."""

    absent = frozenset({"erika"})
    rel, instance, pruefen = _after_erfassen("tom", absent)
    with pytest.raises(ExecutionError) as claimed:
        claim_activity(instance, rel, pruefen, "tom", absent_agents=absent)
    assert claimed.value.code == "EX.not-eligible"
    assert claimed.value.params == {
        "step": "Prüfen", "reason": "four-eyes", "other_step": "Erfassen", "action": "claim",
    }
    with pytest.raises(ExecutionError) as completed:
        complete_activity(instance, rel, pruefen, agent_id="tom", absent_agents=absent)
    assert completed.value.code == "EX.not-eligible"
    assert completed.value.params["reason"] == "four-eyes"
    assert completed.value.params["other_step"] == "Erfassen"
    # The refused act is named: a completion is no "taking over".
    assert completed.value.params["action"] == "complete"


def test_performer_with_the_role_gets_the_four_eyes_reason_too() -> None:
    """Erika did "Erfassen" herself: refused, and the reason says why."""

    rel, instance, pruefen = _after_erfassen("erika", frozenset())
    with pytest.raises(ExecutionError) as refused:
        complete_activity(instance, rel, pruefen, agent_id="erika")
    assert refused.value.params == {
        "step": "Prüfen", "reason": "four-eyes", "other_step": "Erfassen", "action": "complete",
    }


def test_not_eligible_without_four_eyes_keeps_the_plain_reason() -> None:
    """Negativ: who simply lacks the role gets no four-eyes reason."""

    rel, instance, pruefen = _after_erfassen("erika", frozenset())
    with pytest.raises(ExecutionError) as refused:
        complete_activity(instance, rel, pruefen, agent_id="lea")
    assert refused.value.code == "EX.not-eligible"
    assert refused.value.params == {"step": "Prüfen"}


def test_deputy_chain_never_leads_to_the_performer() -> None:
    """Erika and Nina absent, Nina's deputy is Tom, who did "Erfassen": not via any edge.

    Tom is absent too, so the chain passes him to his deputy Lea, who did
    not do "Erfassen" (decided: the deputy of an absent excluded person may
    stand in; the excluded person never).
    """

    absent = frozenset({"erika", "nina", "tom"})
    rel, erfassen, pruefen = _except_schema()
    rel = set_agent_deputy(rel, "nina", "tom")
    instance = complete_activity(
        instantiate(rel), rel, erfassen, agent_id="tom",
        absent_agents=frozenset({"erika"}),
    )
    eligible = eligible_agents(rel, pruefen, instance, absent_agents=absent)
    assert "tom" not in eligible
    # The chain passes the absent excluded performer: his deputy stands in.
    assert eligible == {"erika", "nina", "lea"}


def test_deputy_still_steps_in_when_someone_else_did_the_step() -> None:
    """Positiv: Nina did "Erfassen"; for absent Erika, Tom still covers "Prüfen"."""

    absent = frozenset({"erika"})
    rel, instance, pruefen = _after_erfassen("nina", frozenset())
    via: dict[str, str] = {}
    eligible = eligible_agents(
        rel, pruefen, instance, absent_agents=absent, deputy_of=via
    )
    assert eligible == {"erika", "tom"}
    assert via == {"tom": "erika"}
    done = complete_activity(instance, rel, pruefen, agent_id="tom", absent_agents=absent)
    assert done.performed_by[pruefen] == "tom"


def test_exclusion_inside_an_exclusion_selects_again() -> None:
    """Grenzfall: "sb except (sb except who did Erfassen)" keeps that performer."""

    rel, erfassen, pruefen = _except_schema()
    clerks = StaffRule(kind=StaffRuleKind.ROLE, ref="sb")
    double = StaffRule(
        kind=StaffRuleKind.EXCEPT,
        operands=[
            StaffRule(kind=StaffRuleKind.OR, operands=[
                clerks, StaffRule(kind=StaffRuleKind.AGENT, ref="tom"),
            ]),
            StaffRule(kind=StaffRuleKind.EXCEPT, operands=[
                clerks,
                StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT, ref=erfassen),
            ]),
        ],
    )
    rel = rel.model_copy(update={"staff_rules": {**rel.staff_rules, pruefen: double}})
    instance = complete_activity(instantiate(rel), rel, erfassen, agent_id="erika")
    # tom (by AGENT) plus erika (excluded from the excluded set) -- both stay.
    assert eligible_agents(rel, pruefen, instance) == {"tom", "erika"}


def test_escalation_stage_never_adds_the_excluded_performer() -> None:
    """A fired FUNCTIONAL stage broadens the offer -- but never to the performer."""

    rel, instance, pruefen = _after_erfassen(
        "tom", frozenset({"erika"}),
        escalation=StaffRule(kind=StaffRuleKind.AGENT, ref="tom"),
    )
    instance.escalated_stages[pruefen] = 1
    assert eligible_agents(rel, pruefen, instance) == {"erika", "nina"}


def test_escalation_stage_still_broadens_for_others() -> None:
    """Positiv: the same stage adds Tom when somebody else did "Erfassen"."""

    rel, instance, pruefen = _after_erfassen(
        "nina", frozenset(),
        escalation=StaffRule(kind=StaffRuleKind.AGENT, ref="tom"),
    )
    instance.escalated_stages[pruefen] = 1
    assert eligible_agents(rel, pruefen, instance) == {"erika", "tom"}


def test_only_the_excluded_deputy_left_reports_the_step_as_unstaffed() -> None:
    """Grenzfall: Erika and Nina absent, their only deputy did "Erfassen"."""

    absent = frozenset({"erika", "nina"})
    rel, erfassen, pruefen = _except_schema()
    rel = set_agent_deputy(rel, "nina", "tom")
    instance = complete_activity(
        instantiate(rel), rel, erfassen, agent_id="tom",
        absent_agents=frozenset({"erika"}),
    )
    [step] = unstaffed_steps(rel, instance, absent_agents=absent)
    assert step.node_id == pruefen
    assert step.reason == "only_absent"
    assert step.agent_ids == ["erika", "nina"]


def test_unstaffed_names_the_deputy_the_four_eyes_rule_excludes() -> None:
    """The report says *why* nobody stands in: the deputy did the step to check.

    "absent without a deputy" was wrong here -- a supervisor would register
    yet another deputy or doubt the deputy rule.
    """

    absent = frozenset({"erika", "nina"})
    rel, erfassen, pruefen = _except_schema()
    rel = set_agent_deputy(rel, "nina", "tom")
    instance = complete_activity(
        instantiate(rel), rel, erfassen, agent_id="tom",
        absent_agents=frozenset({"erika"}),
    )
    [step] = unstaffed_steps(rel, instance, absent_agents=absent)
    assert step.reason == "only_absent"
    assert [
        (d.agent_id, d.stands_in_for, d.step_id, d.step_label) for d in step.excluded_deputies
    ] == [("tom", "erika", erfassen, "Erfassen"), ("tom", "nina", erfassen, "Erfassen")]


def test_unstaffed_without_a_deputy_names_no_excluded_deputy() -> None:
    """Negativ: Nina has no deputy -- the plain "absent without deputy" stays."""

    rel, erfassen, pruefen = _except_schema()
    instance = complete_activity(instantiate(rel), rel, erfassen, agent_id="erika")
    [step] = unstaffed_steps(rel, instance, absent_agents=frozenset({"nina"}))
    assert (step.reason, step.agent_ids, step.excluded_deputies) == ("only_absent", ["nina"], [])


def test_a_present_deputy_still_staffs_the_step_and_nothing_is_reported() -> None:
    """Grenzfall: Erika absent, her deputy Tom did *not* do "Erfassen" -- staffed."""

    rel, erfassen, pruefen = _except_schema()
    instance = complete_activity(instantiate(rel), rel, erfassen, agent_id="nina")
    assert unstaffed_steps(rel, instance, absent_agents=frozenset({"erika", "nina"})) == []


def test_four_eyes_holds_through_the_api_worklist_claim_and_completion() -> None:
    """End to end: the deputy who did "Erfassen" neither sees nor works "Prüfen"."""

    api_module._store.clear()
    api_module._instances.clear()
    api_module._absence_store.clear()
    api_module._audit.clear()
    rel, erfassen, pruefen = _except_schema()
    api_module._store.put(rel)
    instance = instantiate(rel)
    api_module._instances.put(instance)
    now = datetime.now(UTC)
    window = {
        "start_at": (now - timedelta(hours=1)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
    }
    with TestClient(app) as client:
        assert client.post("/agents/erika/absences", json=window).status_code == 201
        done = client.post(
            f"/instances/{instance.id}/complete",
            json={"node_id": erfassen, "agent_id": "tom"},
        )
        assert done.status_code == 200
        assert done.json()["performed_by"][erfassen] == "tom"
        # Not in Tom's worklist, not among the eligibles of the instance view.
        assert client.get("/agents/tom/tasks").json() == []
        [task] = client.get(f"/instances/{instance.id}/tasks").json()
        assert task["node_id"] == pruefen
        assert "tom" not in task["eligible_agents"]
        claim = client.post(
            f"/instances/{instance.id}/claim",
            json={"node_id": pruefen, "agent_id": "tom"},
        )
        assert claim.status_code == 409
        assert claim.json()["detail"]["code"] == "EX.not-eligible"
        assert claim.json()["detail"]["params"]["reason"] == "four-eyes"
        refused = client.post(
            f"/instances/{instance.id}/complete",
            json={"node_id": pruefen, "agent_id": "tom"},
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["params"] == {
            "step": "Prüfen", "reason": "four-eyes", "other_step": "Erfassen",
            "action": "complete",
        }
        # Positiv: Nina, who did not do "Erfassen", completes the check.
        ok = client.post(
            f"/instances/{instance.id}/complete",
            json={"node_id": pruefen, "agent_id": "nina"},
        )
        assert ok.status_code == 200
        assert ok.json()["state"] == "COMPLETED"
