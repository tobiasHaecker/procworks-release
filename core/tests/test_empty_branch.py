# SPDX-License-Identifier: BUSL-1.1
"""Empty XOR branches: "work occurs in only one branch of the split".

Deleting the sole activity of an XOR branch does not remove the branch -- it
leaves it standing as an *empty* branch (a direct ``split -> join`` edge that
keeps its K7 partition cell). The gateway therefore stays total and disjoint by
construction (the emptied cell is retained), the engine simply skips to the join
when that cell is selected, and the modeller can express "only in one branch
does work occur". An XOR split may carry at most one empty branch (it must keep
at least one non-empty branch); the empty branch is removed on demand via
``remove_empty_branch``.
"""

from __future__ import annotations

import pytest

from procworks import (
    AccessMode,
    BranchSpec,
    DataType,
    add_data_element,
    complete_activity,
    conditional_insert,
    connect_data,
    create_empty_schema,
    delete_node,
    instantiate,
    release,
    remove_empty_branch,
    serial_insert,
    validate,
    worklist,
)
from procworks.bpmn import export_bpmn, import_bpmn
from procworks.model import NodeState, NodeType
from procworks.operations import CorrectnessError


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _split(schema: object) -> str:
    return next(n.id for n in schema.nodes.values() if n.type is NodeType.XOR_SPLIT)  # type: ignore[attr-defined]


def _join(schema: object) -> str:
    return next(n.id for n in schema.nodes.values() if n.type is NodeType.XOR_JOIN)  # type: ignore[attr-defined]


def _threshold_schema():
    """A two-way THRESHOLD split: "Team" (< 1001) vs. "Leitung" (>= 1001)."""

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


def _enum_schema():
    """A three-way ENUM split: Gold / Silber / Sonstige(otherwise)."""

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


# --- deleting the last activity leaves an empty branch (K7 preserved) ---------


def test_delete_last_activity_leaves_empty_branch() -> None:
    schema = _threshold_schema()
    split, join = _split(schema), _join(schema)
    schema = delete_node(schema, _nid(schema, "Team"))

    # The gateway is intact; the "Team" branch is now a direct split -> join.
    assert schema.nodes[split].type is NodeType.XOR_SPLIT
    assert not any(n.label == "Team" for n in schema.nodes.values())
    empty = [e for e in schema.edges if e.source == split and e.target == join]
    assert len(empty) == 1
    assert empty[0].condition == "betrag < 1001"  # the cell is retained
    # The decision still tiles the whole domain -> K7 holds.
    assert validate(schema) == []
    decision = schema.xor_decisions[split]
    assert {b.target for b in decision.branches} == {join, _nid(schema, "Leitung")}


def test_emptied_branch_is_selectable_by_the_engine() -> None:
    """A value in the empty cell skips straight to the join; the other runs."""

    schema = _threshold_schema()
    join = _join(schema)
    schema = delete_node(schema, _nid(schema, "Team"))  # Team (< 1001) now empty
    schema = release(schema)
    leitung = _nid(schema, "Leitung")

    # betrag 100 falls into the *empty* branch -> Leitung is skipped, join taken.
    inst = instantiate(schema)
    inst = complete_activity(inst, schema, _nid(schema, "Erfassen"), {"betrag": 100})
    assert inst.node_states[leitung] is NodeState.SKIPPED
    assert inst.node_states[join] in (NodeState.ACTIVATED, NodeState.COMPLETED)
    assert worklist(inst, schema) == []  # no interactive work in the empty branch

    # betrag 5000 falls into the non-empty branch -> Leitung becomes work.
    inst2 = instantiate(schema)
    inst2 = complete_activity(inst2, schema, _nid(schema, "Erfassen"), {"betrag": 5000})
    assert worklist(inst2, schema) == [leitung]


# --- the one-empty-branch cap (operation + validator backstop) ----------------


def test_second_empty_branch_is_rejected_by_operation() -> None:
    """Emptying a second branch of the same split is refused (keep one non-empty)."""

    schema = _enum_schema()
    schema = delete_node(schema, _nid(schema, "Gold"))  # first empty branch: ok
    assert validate(schema) == []
    with pytest.raises(CorrectnessError):
        delete_node(schema, _nid(schema, "Silber"))  # would be the second empty


def test_two_empty_branches_are_rejected_by_the_validator() -> None:
    """K7 backstop: a hand-built schema with two empty branches is invalid.

    Guards every non-operation path (BPMN import, ad-hoc, migration): starting
    from a valid one-empty schema, we hand-reroute a second branch onto the join
    (bypassing the operation cap) and confirm the validator refuses it.
    """

    from procworks.model import ControlEdge

    schema = _enum_schema()
    schema = delete_node(schema, _nid(schema, "Gold"))
    split, join = _split(schema), _join(schema)
    broken = schema.model_copy(deep=True)
    silber = _nid(broken, "Silber")
    broken.edges = [e for e in broken.edges if e.source != silber and e.target != silber]
    del broken.nodes[silber]
    broken.data_accesses = [a for a in broken.data_accesses if a.node_id != silber]
    broken.edges.append(ControlEdge(source=split, target=join))  # 2nd empty branch
    for branch in broken.xor_decisions[split].branches:
        if branch.target == silber:
            branch.target = join

    findings = validate(broken)
    assert any(
        f.rule == "K7" and "at most one empty branch" in f.message for f in findings
    )


# --- manual removal of the empty branch --------------------------------------


def test_remove_empty_branch_dissolves_two_way_split() -> None:
    schema = _threshold_schema()
    split = _split(schema)
    schema = delete_node(schema, _nid(schema, "Team"))
    schema = remove_empty_branch(schema, split)

    types = {n.type for n in schema.nodes.values()}
    assert NodeType.XOR_SPLIT not in types and NodeType.XOR_JOIN not in types
    leitung = _nid(schema, "Leitung")
    assert schema.incoming(leitung)[0].condition is None  # now unconditional
    assert validate(schema) == []


def test_remove_empty_enum_branch_keeps_gateway_values_fall_to_catch_all() -> None:
    """Dropping an empty ENUM values-branch stays total (values hit otherwise)."""

    schema = _enum_schema()
    split = _split(schema)
    schema = delete_node(schema, _nid(schema, "Silber"))  # "silber" branch empty
    schema = remove_empty_branch(schema, split)

    assert schema.nodes[split].type is NodeType.XOR_SPLIT  # gateway kept
    assert len(schema.xor_decisions[split].branches) == 2  # Gold + Sonstige
    assert validate(schema) == []


def test_remove_empty_branch_without_empty_branch_is_rejected() -> None:
    schema = _threshold_schema()
    with pytest.raises(CorrectnessError):
        remove_empty_branch(schema, _split(schema))


def test_remove_empty_branch_on_non_split_is_rejected() -> None:
    schema = _threshold_schema()
    with pytest.raises(CorrectnessError):
        remove_empty_branch(schema, _nid(schema, "Erfassen"))


# --- data flow: the empty branch writes nothing (D1) --------------------------


def test_mandatory_read_after_join_rejects_write_only_in_non_empty_branch() -> None:
    """D1: an element written only in the surviving branch cannot be a mandatory
    read after the join, because the empty branch is a path that writes nothing.
    """

    schema = _threshold_schema()
    leitung = _nid(schema, "Leitung")
    schema = add_data_element(schema, "notiz", DataType.STRING, element_id="notiz")
    schema = connect_data(schema, leitung, "notiz", AccessMode.WRITE)
    schema = delete_node(schema, _nid(schema, "Team"))  # Team branch now empty
    # A step after the join that *mandatorily* reads "notiz" must be rejected:
    # on the empty path "notiz" is never written.
    join = _join(schema)
    successor = schema.outgoing(join)[0].target
    with pytest.raises(CorrectnessError):
        connect_data(schema, successor, "notiz", AccessMode.READ)


# --- BPMN round-trip preserves the empty branch -------------------------------


def test_empty_branch_survives_bpmn_round_trip() -> None:
    schema = _threshold_schema()
    split, join = _split(schema), _join(schema)
    schema = delete_node(schema, _nid(schema, "Team"))

    restored = import_bpmn(export_bpmn(schema))
    assert validate(restored) == []
    empty = [e for e in restored.edges if e.source == split and e.target == join]
    assert len(empty) == 1
    decision = restored.xor_decisions[split]
    assert any(b.target == join for b in decision.branches)
