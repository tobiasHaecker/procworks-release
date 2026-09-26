# SPDX-License-Identifier: BUSL-1.1
"""Tests for ad-hoc instance changes (roadmap step 10, rules R1/R2).

An ad-hoc change adapts a single running instance through a per-instance
schema variant (``ad_hoc_schema``). R1 guards the state compatibility (only the
not-yet-executed region may change), R2 re-runs validate-before-commit so the
variant stays correct. After a change the Execution Engine continues seamlessly
against the variant.
"""

from __future__ import annotations

import pytest
from staffing import staffed

from procworks import (
    ExecutionContext,
    adhoc_delete_node,
    adhoc_insert_activity,
    adhoc_rename_activity,
    complete_activity,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
    worklist,
)
from procworks.model import (
    InstanceState,
    NodeState,
    NodeType,
    ProcessSchema,
    StaffRule,
    StaffRuleKind,
)
from procworks.store import InMemoryInstanceStore
from procworks.validator import CorrectnessError


def _resolver_for(*schemas: ProcessSchema):
    by_id = {s.id: s for s in schemas}

    def resolve(schema_id: str, version: int | None) -> ProcessSchema | None:
        schema = by_id.get(schema_id)
        if schema is None:
            return None
        if version is not None and schema.version != version:
            return None
        return schema

    return resolve


def _ordered_activities(schema: ProcessSchema) -> list[str]:
    """Activity ids in control-flow order start -> ... -> end."""

    order: list[str] = []
    current = "start"
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        outgoing = schema.outgoing(current)
        if not outgoing:
            break
        nxt = outgoing[0].target
        if schema.nodes[nxt].type is NodeType.ACTIVITY:
            order.append(nxt)
        current = nxt
    return order


def _released_serial() -> ProcessSchema:
    # start -> A -> B -> end
    schema = create_empty_schema("Seriell", schema_id="serial")
    schema = serial_insert(schema, "B", after_node_id="start")
    schema = serial_insert(schema, "A", after_node_id="start")
    return release(staffed(schema))


def test_insert_into_unexecuted_region_runs_through_variant() -> None:
    schema = _released_serial()
    activities = _ordered_activities(schema)
    a_id, b_id = activities[0], activities[1]

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(schema), store)
    instance = instantiate(schema, context=context)

    # A is ready, edge A->B not signaled, B not yet activated -> R1 holds.
    # The new step is worked by the same people as A (B2, VAL-03).
    instance = adhoc_insert_activity(
        instance, schema, a_id, "Zusatzpruefung", staff_rule=schema.staff_rules[a_id]
    )
    assert instance.ad_hoc_schema is not None
    assert len(instance.ad_hoc_deltas) == 1
    new_id = next(
        n.id
        for n in instance.ad_hoc_schema.nodes.values()
        if n.label == "Zusatzpruefung"
    )
    assert instance.node_states[new_id] is NodeState.NOT_ACTIVATED

    # The instance now runs against its variant: A -> new -> B -> end.
    variant = instance.ad_hoc_schema
    instance = complete_activity(instance, variant, a_id, context=context)
    assert new_id in worklist(instance, variant)
    instance = complete_activity(instance, variant, new_id, context=context)
    instance = complete_activity(instance, variant, b_id, context=context)
    assert instance.state is InstanceState.COMPLETED


def test_insert_without_staff_rule_is_rejected_by_b2() -> None:
    """VAL-03 (Validierung 2026-09-25): an ad-hoc step without a staff rule was
    structurally correct, got activated -- and stood in nobody's worklist.

    The core now demands the rule for the *new* step (``check_executable``
    restricted to that node; B2 itself stays outside ``validate()``).
    """

    schema = _released_serial()
    a_id = _ordered_activities(schema)[0]
    context = ExecutionContext(_resolver_for(schema), InMemoryInstanceStore())
    instance = instantiate(schema, context=context)

    with pytest.raises(CorrectnessError, match=r"\[B2\]") as exc:
        adhoc_insert_activity(instance, schema, a_id, "Ohne Bearbeiter")

    [finding] = exc.value.findings
    assert (finding.rule, finding.code) == ("B2", "B2.no-staff")
    assert finding.node_id not in schema.nodes  # it is the new step, nothing else


def test_insert_with_an_unresolvable_staff_rule_is_rejected_by_z() -> None:
    # The rule travels through validate() like every other rule: an unknown
    # role is Z1 -- so an ad-hoc step cannot smuggle in a broken assignment.
    schema = _released_serial()
    a_id = _ordered_activities(schema)[0]
    context = ExecutionContext(_resolver_for(schema), InMemoryInstanceStore())
    instance = instantiate(schema, context=context)

    with pytest.raises(CorrectnessError, match=r"\[Z1\]"):
        adhoc_insert_activity(
            instance, schema, a_id, "Falsch besetzt",
            staff_rule=StaffRule(kind=StaffRuleKind.ROLE, ref="ghost"),
        )


def test_insert_after_executed_node_violates_r1() -> None:
    schema = _released_serial()
    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(schema), store)
    instance = instantiate(schema, context=context)

    # "start" is already completed and its outgoing edge is signaled.
    with pytest.raises(CorrectnessError) as exc:
        adhoc_insert_activity(instance, schema, "start", "ZuSpaet")
    assert exc.value.findings[0].rule == "R1"


def test_delete_unreached_activity_succeeds() -> None:
    schema = _released_serial()
    activities = _ordered_activities(schema)
    b_id = activities[1]

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(schema), store)
    instance = instantiate(schema, context=context)

    instance = adhoc_delete_node(instance, schema, b_id)
    assert instance.ad_hoc_schema is not None
    assert b_id not in instance.ad_hoc_schema.nodes
    assert b_id not in instance.node_states

    # Variant is now start -> A -> end; completing A finishes the instance.
    variant = instance.ad_hoc_schema
    a_id = activities[0]
    instance = complete_activity(instance, variant, a_id, context=context)
    assert instance.state is InstanceState.COMPLETED


def test_delete_reached_node_violates_r1() -> None:
    schema = _released_serial()
    activities = _ordered_activities(schema)
    a_id = activities[0]

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(schema), store)
    instance = instantiate(schema, context=context)

    # A is already ACTIVATED (ready) -> it is "reached".
    with pytest.raises(CorrectnessError) as exc:
        adhoc_delete_node(instance, schema, a_id)
    assert exc.value.findings[0].rule == "R1"


def test_delete_non_activity_violates_r1() -> None:
    schema = _released_serial()
    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(schema), store)
    instance = instantiate(schema, context=context)

    with pytest.raises(CorrectnessError) as exc:
        adhoc_delete_node(instance, schema, "end")
    assert exc.value.findings[0].rule == "R1"


def test_rename_unreached_activity_succeeds() -> None:
    schema = _released_serial()
    activities = _ordered_activities(schema)
    b_id = activities[1]

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(schema), store)
    instance = instantiate(schema, context=context)

    instance = adhoc_rename_activity(instance, schema, b_id, "B (angepasst)")
    assert instance.ad_hoc_schema is not None
    assert instance.ad_hoc_schema.nodes[b_id].label == "B (angepasst)"
    assert len(instance.ad_hoc_deltas) == 1
    # Markings are untouched by a pure relabelling.
    assert instance.node_states[b_id] is NodeState.NOT_ACTIVATED

    # The instance still runs to completion against the variant.
    variant = instance.ad_hoc_schema
    a_id = activities[0]
    instance = complete_activity(instance, variant, a_id, context=context)
    instance = complete_activity(instance, variant, b_id, context=context)
    assert instance.state is InstanceState.COMPLETED


def test_rename_reached_node_violates_r1() -> None:
    schema = _released_serial()
    activities = _ordered_activities(schema)
    a_id = activities[0]

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(schema), store)
    instance = instantiate(schema, context=context)

    # A is already ready (ACTIVATED) -> reached, history must not be rewritten.
    with pytest.raises(CorrectnessError) as exc:
        adhoc_rename_activity(instance, schema, a_id, "Zu spaet")
    assert exc.value.findings[0].rule == "R1"


def test_rename_gateway_violates_r1() -> None:
    schema = _released_serial()
    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(schema), store)
    instance = instantiate(schema, context=context)

    with pytest.raises(CorrectnessError) as exc:
        adhoc_rename_activity(instance, schema, "end", "Neues Ende")
    assert exc.value.findings[0].rule == "R1"

