# SPDX-License-Identifier: BUSL-1.1
"""K7: XOR branch partitions are total, disjoint and decided by construction.

These tests pin the *constructive* guarantee for exclusive splits: a modeller
can never produce a split that deadlocks (no branch matches) or activates
several paths at once (overlapping branches). The partition is expressed over a
typed discriminator that is provably written before the split, so the engine
resolves the branch purely from instance data -- across THRESHOLD, BOOLEAN and
ENUM discriminators -- and the property is preserved under every evolution step.
"""

from __future__ import annotations

import pytest

from procworks import (
    AccessMode,
    BranchSpec,
    DataType,
    ExecutionContext,
    add_data_element,
    complete_activity,
    conditional_insert,
    connect_data,
    create_empty_schema,
    instantiate,
    is_migratable,
    migrate_instance,
    new_revision,
    release,
    serial_insert,
    validate,
    worklist,
)
from procworks.model import NodeState, NodeType
from procworks.operations import CorrectnessError
from procworks.store import InMemoryInstanceStore


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _split_id(schema: object) -> str:
    return next(n.id for n in schema.nodes.values() if n.type is NodeType.XOR_SPLIT)  # type: ignore[attr-defined]


def _threshold_schema():
    schema = create_empty_schema("Schwelle")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = _nid(schema, "Erfassen")
    schema = add_data_element(schema, "betrag", DataType.INTEGER, element_id="betrag")
    schema = connect_data(schema, erfassen, "betrag", AccessMode.WRITE)
    return conditional_insert(
        schema,
        after_node_id=erfassen,
        discriminator="betrag",
        branches=[BranchSpec(label="Team", upper=1001), BranchSpec(label="Leitung")],
    )


def _boolean_schema():
    schema = create_empty_schema("Boolesch")
    schema = serial_insert(schema, "Pruefen", after_node_id="start")
    pruefen = _nid(schema, "Pruefen")
    schema = add_data_element(schema, "eilig", DataType.BOOLEAN, element_id="eilig")
    schema = connect_data(schema, pruefen, "eilig", AccessMode.WRITE)
    return conditional_insert(
        schema,
        after_node_id=pruefen,
        discriminator="eilig",
        branches=[
            BranchSpec(label="Express", bool_value=True),
            BranchSpec(label="Standard", bool_value=False),
        ],
    )


def _enum_schema():
    schema = create_empty_schema("Aufzaehlung")
    schema = serial_insert(schema, "Einstufen", after_node_id="start")
    einstufen = _nid(schema, "Einstufen")
    schema = add_data_element(schema, "segment", DataType.STRING, element_id="segment")
    schema = connect_data(schema, einstufen, "segment", AccessMode.WRITE)
    return conditional_insert(
        schema,
        after_node_id=einstufen,
        discriminator="segment",
        branches=[
            BranchSpec(label="Gold", values=("gold",)),
            BranchSpec(label="Silber", values=("silber",)),
            BranchSpec(label="Sonstige", is_else=True),
        ],
    )


# --- happy paths: the three partition kinds are valid by construction --------


def test_threshold_partition_is_valid() -> None:
    schema = _threshold_schema()
    assert validate(schema) == []
    conditions = {e.condition for e in schema.edges if e.condition is not None}
    assert conditions == {"betrag < 1001", "betrag >= 1001"}


def test_boolean_partition_is_valid() -> None:
    schema = _boolean_schema()
    assert validate(schema) == []
    conditions = {e.condition for e in schema.edges if e.condition is not None}
    assert conditions == {"eilig == true", "eilig == false"}


def test_enum_partition_is_valid() -> None:
    schema = _enum_schema()
    assert validate(schema) == []
    conditions = {e.condition for e in schema.edges if e.condition is not None}
    assert conditions == {"segment in [gold]", "segment in [silber]", "segment: otherwise"}


# --- the validator has teeth: it rejects non-partitions (completeness) -------


def test_threshold_gap_is_rejected() -> None:
    """A bounded last branch leaves the upper tail uncovered -> not total."""

    schema = _threshold_schema()
    broken = schema.model_copy(deep=True)
    decision = broken.xor_decisions[_split_id(broken)]
    decision.branches[-1].upper = 5000.0  # last branch must be unbounded
    findings = validate(broken)
    assert any(f.rule == "K7" for f in findings)


def test_enum_without_catch_all_is_rejected() -> None:
    """Dropping the otherwise branch leaves unknown values uncovered."""

    schema = _enum_schema()
    broken = schema.model_copy(deep=True)
    decision = broken.xor_decisions[_split_id(broken)]
    for branch in decision.branches:
        branch.is_else = False
    findings = validate(broken)
    assert any(f.rule == "K7" for f in findings)


# --- the validator has teeth: it rejects overlaps (mutual exclusion) ---------


def test_threshold_overlap_is_rejected() -> None:
    """An unbounded non-last branch matches every value -> overlap."""

    schema = _threshold_schema()
    broken = schema.model_copy(deep=True)
    decision = broken.xor_decisions[_split_id(broken)]
    decision.branches[0].upper = None  # now both branches match everything
    findings = validate(broken)
    assert any(f.rule == "K7" for f in findings)


def test_enum_overlap_is_rejected() -> None:
    """The same value listed by two branches could activate both paths."""

    schema = _enum_schema()
    broken = schema.model_copy(deep=True)
    decision = broken.xor_decisions[_split_id(broken)]
    decision.branches[1].values = ["gold"]  # also claimed by the Gold branch
    findings = validate(broken)
    assert any(f.rule == "K7" for f in findings)


# --- the discriminator must be guaranteed-written before the split -----------


def test_discriminator_must_be_written_before_split() -> None:
    schema = _threshold_schema()
    broken = schema.model_copy(deep=True)
    broken.data_accesses = []  # remove the prior write of ``betrag``
    findings = validate(broken)
    assert any(f.rule == "K7" for f in findings)


def test_unknown_discriminator_is_rejected_at_build_time() -> None:
    schema = create_empty_schema("Ohne")
    schema = serial_insert(schema, "A", after_node_id="start")
    a = _nid(schema, "A")
    with pytest.raises(CorrectnessError):
        conditional_insert(
            schema,
            after_node_id=a,
            discriminator="missing",
            branches=[BranchSpec(label="X", upper=1), BranchSpec(label="Y")],
        )


# --- engine resolves the branch automatically for every partition kind -------


def test_boolean_execution_resolves_from_data() -> None:
    schema = release(_boolean_schema())
    pruefen = _nid(schema, "Pruefen")
    express = _nid(schema, "Express")
    standard = _nid(schema, "Standard")

    instance = instantiate(schema)
    instance = complete_activity(instance, schema, pruefen, {"eilig": True})
    assert worklist(instance, schema) == [express]
    assert instance.node_states[standard] is NodeState.SKIPPED


def test_enum_execution_takes_catch_all_for_unknown_value() -> None:
    schema = release(_enum_schema())
    einstufen = _nid(schema, "Einstufen")
    gold = _nid(schema, "Gold")
    sonstige = _nid(schema, "Sonstige")

    instance = instantiate(schema)
    instance = complete_activity(instance, schema, einstufen, {"segment": "platin"})
    assert worklist(instance, schema) == [sonstige]
    assert instance.node_states[gold] is NodeState.SKIPPED


# --- evolution: K7 survives revisions and further edits ----------------------


def test_new_revision_preserves_k7() -> None:
    schema = release(_threshold_schema())
    revision = new_revision(schema, new_schema_id="rev2")
    assert validate(revision) == []
    assert revision.version == schema.version + 1
    # the discriminator and its partition carry over verbatim
    assert revision.xor_decisions[_split_id(revision)].discriminator == "betrag"


def test_editing_a_revision_cannot_break_k7() -> None:
    """Adding a branch to an existing split must keep the partition total."""

    schema = release(_threshold_schema())
    revision = new_revision(schema, new_schema_id="rev3")
    leitung = _nid(revision, "Leitung")
    # inserting a nested step inside a branch keeps the schema K7-correct
    revision = serial_insert(revision, "Nacharbeit", after_node_id=leitung)
    assert validate(revision) == []


def test_running_k7_instance_migrates_to_revised_schema() -> None:
    """An in-flight K7 instance migrates onto a revised, still-K7 schema.

    This is the evolution guarantee end-to-end: the branch resolves
    automatically from data, the schema is revised ahead of the execution
    front, and the migrated instance keeps its chosen branch while the target
    re-validates K7.
    """

    schema = release(_threshold_schema())
    store = InMemoryInstanceStore()

    def resolver(schema_id: str, version: int | None) -> object:
        return schema if schema_id == schema.id else None

    context = ExecutionContext(resolver, store)
    instance = instantiate(schema, context=context)
    erfassen = _nid(schema, "Erfassen")
    # 2000 >= 1001 -> the engine auto-resolves to the Leitung branch
    instance = complete_activity(instance, schema, erfassen, {"betrag": 2000}, context=context)
    leitung = _nid(schema, "Leitung")
    assert instance.node_states[leitung] is NodeState.ACTIVATED

    # revise ahead of the execution front (a step after the XOR join)
    join = next(n.id for n in schema.nodes.values() if n.type is NodeType.XOR_JOIN)
    target = new_revision(schema, new_schema_id="rev-mig")
    target = serial_insert(target, "Nachgelagert", after_node_id=join)
    target = release(target)
    assert validate(target) == []  # K7 preserved across the revision

    assert is_migratable(instance, schema, target)
    migrated = migrate_instance(instance, schema, target)
    assert migrated.schema_id == target.id
    assert migrated.node_states[leitung] is NodeState.ACTIVATED
    assert migrated.node_states[_nid(target, "Nachgelagert")] is NodeState.NOT_ACTIVATED
