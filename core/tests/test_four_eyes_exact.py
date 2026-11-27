# SPDX-License-Identifier: BUSL-1.1
"""Deputies under the four-eyes principle: exactly as the rule says.

The four-eyes part of a staff rule keeps a performer away from a later step
-- also as a deputy or through an escalation stage. Two places were stricter
than the rule:

* **The rule decides who is excluded.** A performer counted as excluded as
  soon as a "performer of step X" term stood on the excluding side of an
  ``EXCEPT`` -- also when the excluding side, evaluated, did not contain
  them (``sb except (performer of X and sb)``: a performer outside ``sb`` is
  not excluded, but was kept out as a deputy). Now a performer is excluded
  only when the excluding side of the rule, evaluated exactly, contains
  them.
* **The chain may pass an absent excluded person.** When the deputy of an
  absent person is excluded, the excluded person is never offered -- but if
  they are absent themselves, *their* deputy may stand in (unless excluded
  too). Before, the chain stopped there and the step could stay unstaffed
  although an admissible person existed.

The acting person stays checked in every case. The bypass cases measured in
the four-eyes retest are pinned here as regression tests (organisation: A
and N, M in role ``sb``; B and C in ``tl``; E in ``pr``; A's and M's deputy is
B, B's deputy is C; "Prüfen" = ``sb except performer of Erfassen``).
"""

from __future__ import annotations

import pytest

from procworks import (
    EscalationKind,
    EscalationPolicy,
    EscalationStage,
    TimeConstraint,
    add_agent,
    add_role,
    assign_staff_rule,
    complete_activity,
    create_empty_schema,
    eligible_agents,
    instantiate,
    release,
    serial_insert,
    set_escalation_policy,
    set_time_constraint,
)
from procworks.execution import ExecutionError
from procworks.model import ProcessInstance, ProcessSchema, StaffRule, StaffRuleKind

_SB = StaffRule(kind=StaffRuleKind.ROLE, ref="sb")
_TL = StaffRule(kind=StaffRuleKind.ROLE, ref="tl")
_PR = StaffRule(kind=StaffRuleKind.ROLE, ref="pr")


def _npa(step: str) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT, ref=step)


def _except(left: StaffRule, right: StaffRule) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.EXCEPT, operands=[left, right])


def _schema(
    check: StaffRule | None = None,
    *,
    escalation: StaffRule | None = None,
    deputies: dict[str, str] | None = None,
) -> tuple[ProcessSchema, str, str, str]:
    """Erfassen (sb) -> Zweit (sb) -> Prüfen, released.

    :param check: the rule of "Prüfen" (default ``sb except performer of
        Erfassen``); built with the step ids via a callable placeholder.
    :returns: the schema and the ids of Erfassen, Zweit and Prüfen.
    """

    schema = create_empty_schema("VierAugenGenau")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = serial_insert(schema, "Zweit", after_node_id=erfassen)
    zweit = next(n.id for n in schema.nodes.values() if n.label == "Zweit")
    schema = serial_insert(schema, "Prüfen", after_node_id=zweit)
    pruefen = next(n.id for n in schema.nodes.values() if n.label == "Prüfen")
    for role in ("sb", "tl", "pr"):
        schema = add_role(schema, role, role_id=role)
    deputy_of = {"A": "B", "M": "B", "B": "C"} if deputies is None else deputies
    for agent, roles in (("C", ["tl"]), ("B", ["tl"]), ("A", ["sb"]), ("N", ["sb"]),
                         ("M", ["sb"]), ("E", ["pr"])):
        schema = add_agent(schema, agent, role_ids=roles, agent_id=agent)
    org = schema.org_model.model_copy(deep=True)
    for agent, deputy in deputy_of.items():
        org.agents[agent] = org.agents[agent].model_copy(update={"deputy_id": deputy})
    schema = schema.model_copy(update={"org_model": org})
    schema = assign_staff_rule(schema, erfassen, _SB)
    schema = assign_staff_rule(schema, zweit, _SB)
    rule = check if check is not None else _except(_SB, _npa(erfassen))
    rule = StaffRule.model_validate_json(
        rule.model_dump_json().replace("@ERFASSEN", erfassen).replace("@ZWEIT", zweit)
    )
    schema = assign_staff_rule(schema, pruefen, rule)
    if escalation is not None:
        schema = set_time_constraint(schema, pruefen, TimeConstraint(max_duration_seconds=3600))
        schema = set_escalation_policy(
            schema,
            pruefen,
            EscalationPolicy(stages=[EscalationStage(
                after_seconds=0, kind=EscalationKind.FUNCTIONAL, rule=escalation,
            )]),
        )
    return release(schema), erfassen, zweit, pruefen


def _performed(
    schema: ProcessSchema, erfassen: str, zweit: str, by: str, *,
    absent: frozenset[str] = frozenset(), second: str = "N",
) -> ProcessInstance:
    """Run Erfassen by ``by`` (``absent`` at that moment) and Zweit by ``second``."""

    instance = complete_activity(instantiate(schema), schema, erfassen, agent_id=by,
                                 absent_agents=absent)
    return complete_activity(instance, schema, zweit, agent_id=second)


def _refusal(schema: ProcessSchema, instance: ProcessInstance, step: str, agent: str,
             absent: frozenset[str]) -> dict[str, str]:
    with pytest.raises(ExecutionError) as exc:
        complete_activity(instance, schema, step, agent_id=agent, absent_agents=absent)
    assert exc.value.code == "EX.not-eligible"
    return dict(exc.value.params)


# --- regression: the bypass cases stay closed ----------------------------------------


def test_c1_the_deputy_who_did_the_step_is_not_offered_the_check() -> None:
    schema, erfassen, zweit, pruefen = _schema()
    absent = frozenset({"A"})
    instance = _performed(schema, erfassen, zweit, "B", absent=absent)

    assert eligible_agents(schema, pruefen, instance, absent_agents=absent) == {"A", "M", "N"}
    assert _refusal(schema, instance, pruefen, "B", absent)["reason"] == "four-eyes"


def test_c2_a_second_level_deputy_who_did_the_step_is_not_offered() -> None:
    schema, erfassen, zweit, pruefen = _schema()
    absent = frozenset({"A", "B"})
    instance = _performed(schema, erfassen, zweit, "C", absent=absent)

    assert eligible_agents(schema, pruefen, instance, absent_agents=absent) == {
        "A", "B", "M", "N"
    }
    assert _refusal(schema, instance, pruefen, "C", absent)["reason"] == "four-eyes"


def test_c5_a_deputy_who_did_not_do_the_step_stands_in() -> None:
    schema, erfassen, zweit, pruefen = _schema()
    absent = frozenset({"A"})
    instance = _performed(schema, erfassen, zweit, "N")

    assert eligible_agents(schema, pruefen, instance, absent_agents=absent) == {"A", "B", "M"}
    done = complete_activity(instance, schema, pruefen, agent_id="B", absent_agents=absent)
    assert done.performed_by[pruefen] == "B"


def test_c6_an_escalation_stage_never_adds_the_performer_back() -> None:
    schema, erfassen, zweit, pruefen = _schema(
        escalation=StaffRule(kind=StaffRuleKind.OR, operands=[_TL, _PR])
    )
    instance = _performed(schema, erfassen, zweit, "B", absent=frozenset({"A"}))
    instance.escalated_stages[pruefen] = 1

    assert eligible_agents(schema, pruefen, instance) == {"A", "C", "E", "M", "N"}
    assert _refusal(schema, instance, pruefen, "B", frozenset())["reason"] == "four-eyes"


def test_c6a_escalation_to_the_same_role_keeps_the_performer_out() -> None:
    schema, erfassen, zweit, pruefen = _schema(escalation=_SB)
    instance = _performed(schema, erfassen, zweit, "N", second="A")
    instance.escalated_stages[pruefen] = 1

    assert eligible_agents(schema, pruefen, instance) == {"A", "M"}


def test_c7a_two_excluded_performers_stay_out_also_as_deputies() -> None:
    rule = _except(_SB, StaffRule(kind=StaffRuleKind.OR, operands=[
        _npa("@ERFASSEN"), _npa("@ZWEIT")]))
    schema, erfassen, zweit, pruefen = _schema(rule, deputies={"A": "B", "M": "N"})
    absent = frozenset({"A", "M"})
    instance = _performed(schema, erfassen, zweit, "B", absent=frozenset({"A"}), second="N")

    eligible = eligible_agents(schema, pruefen, instance, absent_agents=absent)
    assert "B" not in eligible and "N" not in eligible
    assert eligible == {"A", "M"}


def test_c7b_an_exception_inside_the_exclusion_readmits_as_the_rule_says() -> None:
    """``sb except (performer of Erfassen except tl)``: B (tl) is admitted."""

    rule = _except(_SB, _except(_npa("@ERFASSEN"), _TL))
    schema, erfassen, zweit, pruefen = _schema(rule)
    absent = frozenset({"A"})
    instance = _performed(schema, erfassen, zweit, "B", absent=absent)

    assert "B" in eligible_agents(schema, pruefen, instance, absent_agents=absent)


# --- the decisions: exactly as the rule says ------------------------------------------


def test_c7c_a_performer_the_rule_does_not_exclude_may_stand_in() -> None:
    """``sb except (performer of Erfassen and sb)``: B (tl) is not excluded.

    Before, B was kept out as A's deputy although the rule admits him.
    """

    rule = _except(_SB, StaffRule(kind=StaffRuleKind.AND, operands=[_npa("@ERFASSEN"), _SB]))
    schema, erfassen, zweit, pruefen = _schema(rule)
    absent = frozenset({"A"})
    instance = _performed(schema, erfassen, zweit, "B", absent=absent)

    assert eligible_agents(schema, pruefen, instance, absent_agents=absent) == {"A", "B", "M", "N"}
    done = complete_activity(instance, schema, pruefen, agent_id="B", absent_agents=absent)
    assert done.performed_by[pruefen] == "B"


def test_c7c_counter_a_performer_the_rule_excludes_stays_out_as_deputy() -> None:
    """Negativ: N (sb) did Erfassen -- the same rule excludes N, also as A's deputy."""

    rule = _except(_SB, StaffRule(kind=StaffRuleKind.AND, operands=[_npa("@ERFASSEN"), _SB]))
    schema, erfassen, zweit, pruefen = _schema(rule, deputies={"A": "N"})
    absent = frozenset({"A"})
    instance = _performed(schema, erfassen, zweit, "N", second="M")

    assert eligible_agents(schema, pruefen, instance, absent_agents=absent) == {"A", "M"}
    assert _refusal(schema, instance, pruefen, "N", absent)["reason"] == "four-eyes"


def test_c3_the_deputy_of_an_absent_excluded_person_may_stand_in() -> None:
    """B did Erfassen for A and is absent now: B's deputy C may check, B never."""

    schema, erfassen, zweit, pruefen = _schema()
    instance = _performed(schema, erfassen, zweit, "B", absent=frozenset({"A"}))
    absent = frozenset({"A", "B"})

    via: dict[str, str] = {}
    eligible = eligible_agents(schema, pruefen, instance, absent_agents=absent, deputy_of=via)

    assert eligible == {"A", "C", "M", "N"}
    assert via == {"C": "B"}
    done = complete_activity(instance, schema, pruefen, agent_id="C", absent_agents=absent)
    assert done.performed_by[pruefen] == "C"
    assert _refusal(schema, instance, pruefen, "B", absent)["reason"] == "four-eyes"


def test_c3_counter_a_present_excluded_person_ends_the_chain() -> None:
    """Negativ: B is present -- no hand-over to B's deputy, B still excluded."""

    schema, erfassen, zweit, pruefen = _schema()
    absent = frozenset({"A"})
    instance = _performed(schema, erfassen, zweit, "B", absent=absent)

    assert eligible_agents(schema, pruefen, instance, absent_agents=absent) == {"A", "M", "N"}


def test_c3_edge_the_chain_never_ends_at_an_excluded_person_behind_it() -> None:
    """Grenzfall: A -> B -> C, B absent, and C did Erfassen: nobody of the chain."""

    schema, erfassen, zweit, pruefen = _schema()
    instance = _performed(schema, erfassen, zweit, "C", absent=frozenset({"A", "B"}))
    # Make B excluded too: B did Zweit, and the rule excludes both performers.
    rule_schema, e2, z2, p2 = _schema(_except(_SB, StaffRule(kind=StaffRuleKind.OR, operands=[
        _npa("@ERFASSEN"), _npa("@ZWEIT")])), deputies={"A": "B", "B": "C"})
    both = complete_activity(
        complete_activity(instantiate(rule_schema), rule_schema, e2, agent_id="C",
                          absent_agents=frozenset({"A", "B"})),
        rule_schema, z2, agent_id="B", absent_agents=frozenset({"A"}),
    )
    absent = frozenset({"A", "B"})

    eligible = eligible_agents(rule_schema, p2, both, absent_agents=absent)

    assert "B" not in eligible and "C" not in eligible
    assert eligible == {"A", "M", "N"}
    assert instance is not None  # the plain variant ran without error
