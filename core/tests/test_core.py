# SPDX-License-Identifier: BUSL-1.1
"""Tests for the Correctness Validator (K1-K3) and change operations.

These tests demonstrate Correctness by Construction:
  * operations always yield a structurally correct schema (happy path),
  * the validator has teeth: it rejects hand-crafted broken schemas,
  * operation preconditions reject illegal calls.
"""

from __future__ import annotations

import pytest

from procworks import (
    BranchSpec,
    add_data_element,
    conditional_insert,
    connect_data,
    create_empty_schema,
    delete_node,
    parallel_insert,
    release,
    rename_node,
    serial_insert,
    validate,
)
from procworks.model import (
    AccessMode,
    ControlEdge,
    DataType,
    LifecycleState,
    Node,
    NodeType,
    ProcessSchema,
)
from procworks.operations import CorrectnessError


def _xor_after_start(schema, low_label, high_label, *, upper, disc="x"):
    """Insert a discriminator-writing step plus an XOR split partitioned on it.

    The discriminator (INTEGER ``disc``) is written by "Erfassen" before the
    split, so the resulting schema satisfies K7. ``low_label`` runs for
    ``disc < upper``, ``high_label`` for ``disc >= upper``.
    """

    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = add_data_element(schema, disc, DataType.INTEGER, element_id=disc)
    schema = connect_data(schema, erfassen, disc, AccessMode.WRITE)
    return conditional_insert(
        schema,
        after_node_id=erfassen,
        discriminator=disc,
        branches=[BranchSpec(label=low_label, upper=upper), BranchSpec(label=high_label)],
    )


def test_empty_schema_is_correct() -> None:
    schema = create_empty_schema("Leer")
    assert validate(schema) == []
    assert schema.start_node().type is NodeType.START
    assert schema.end_node().type is NodeType.END


def test_serial_insert_keeps_schema_correct() -> None:
    schema = create_empty_schema("Seriell")
    schema = serial_insert(schema, "Antrag prüfen", after_node_id="start")
    schema = serial_insert(schema, "Antrag genehmigen", after_node_id="start")
    assert validate(schema) == []
    activities = [n for n in schema.nodes.values() if n.type is NodeType.ACTIVITY]
    assert {a.label for a in activities} == {"Antrag prüfen", "Antrag genehmigen"}


def test_parallel_insert_builds_balanced_and_block() -> None:
    schema = create_empty_schema("Parallel")
    schema = parallel_insert(schema, ["Fachprüfung", "Budgetprüfung"], after_node_id="start")
    assert validate(schema) == []
    assert sum(1 for n in schema.nodes.values() if n.type is NodeType.AND_SPLIT) == 1
    assert sum(1 for n in schema.nodes.values() if n.type is NodeType.AND_JOIN) == 1


def test_conditional_insert_builds_balanced_xor_block() -> None:
    schema = create_empty_schema("Bedingt")
    schema = _xor_after_start(
        schema, "Freigabe Team", "Freigabe Leitung", upper=1001, disc="betrag"
    )
    assert validate(schema) == []
    xor_edges = [e for e in schema.edges if e.condition is not None]
    assert {e.condition for e in xor_edges} == {"betrag < 1001", "betrag >= 1001"}


def test_nested_block_inside_branch() -> None:
    schema = create_empty_schema("Verschachtelt")
    schema = parallel_insert(schema, ["A", "B"], after_node_id="start")
    branch_a = next(n for n in schema.nodes.values() if n.label == "A")
    schema = serial_insert(schema, "A2", after_node_id=branch_a.id)
    assert validate(schema) == []


def test_validator_rejects_dangling_node() -> None:
    schema = create_empty_schema("Defekt")
    schema.nodes["ghost"] = Node(id="ghost", type=NodeType.ACTIVITY, label="verwaist")
    findings = validate(schema)
    rules = {f.rule for f in findings}
    assert "K2" in rules or "K3" in rules


def test_validator_rejects_unbalanced_gateway() -> None:
    # START -> XOR_SPLIT -> (A, B) -> END  without a join => K1 + K2 violations.
    schema = ProcessSchema(
        id="x",
        name="Unbalanciert",
        nodes={
            "start": Node(id="start", type=NodeType.START),
            "xs": Node(id="xs", type=NodeType.XOR_SPLIT),
            "a": Node(id="a", type=NodeType.ACTIVITY, label="A"),
            "b": Node(id="b", type=NodeType.ACTIVITY, label="B"),
            "end": Node(id="end", type=NodeType.END),
        },
        edges=[
            ControlEdge(source="start", target="xs"),
            ControlEdge(source="xs", target="a"),
            ControlEdge(source="xs", target="b"),
            ControlEdge(source="a", target="end"),
            ControlEdge(source="b", target="end"),
        ],
    )
    findings = validate(schema)
    assert any(f.rule == "K1" for f in findings)


def test_serial_insert_after_unknown_node_is_rejected() -> None:
    schema = create_empty_schema("Fehler")
    with pytest.raises(CorrectnessError):
        serial_insert(schema, "X", after_node_id="does-not-exist")


def test_cannot_insert_after_end() -> None:
    schema = create_empty_schema("Fehler")
    with pytest.raises(CorrectnessError):
        serial_insert(schema, "X", after_node_id="end")


def test_parallel_insert_requires_two_branches() -> None:
    schema = create_empty_schema("Fehler")
    with pytest.raises(CorrectnessError):
        parallel_insert(schema, ["nur eine"], after_node_id="start")


def test_release_requires_entwurf_and_marks_released() -> None:
    schema = create_empty_schema("Release")
    schema = serial_insert(schema, "Schritt", after_node_id="start")
    released = release(schema)
    assert released.lifecycle_state is LifecycleState.RELEASED


def test_released_schema_is_not_editable() -> None:
    schema = create_empty_schema("Immutable")
    released = release(schema)
    with pytest.raises(CorrectnessError):
        serial_insert(released, "X", after_node_id="start")


def test_rename_node_changes_label() -> None:
    schema = create_empty_schema("Umbenennen")
    schema = serial_insert(schema, "Alt", after_node_id="start")
    act = next(n for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    schema = rename_node(schema, act.id, "Neu")
    assert schema.nodes[act.id].label == "Neu"
    assert validate(schema) == []


def test_rename_node_rejects_gateway() -> None:
    schema = create_empty_schema("Umbenennen")
    schema = parallel_insert(schema, ["A", "B"], after_node_id="start")
    split = next(n for n in schema.nodes.values() if n.type is NodeType.AND_SPLIT)
    with pytest.raises(CorrectnessError):
        rename_node(schema, split.id, "x")


def test_rename_node_on_released_schema_is_rejected() -> None:
    schema = create_empty_schema("Umbenennen")
    schema = serial_insert(schema, "S", after_node_id="start")
    act = next(n for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    released = release(schema)
    with pytest.raises(CorrectnessError):
        rename_node(released, act.id, "Neu")


def test_delete_serial_activity_closes_gap() -> None:
    schema = create_empty_schema("Loeschen")
    schema = serial_insert(schema, "A", after_node_id="start")
    schema = serial_insert(schema, "B", after_node_id="start")
    target = next(n for n in schema.nodes.values() if n.label == "B")
    schema = delete_node(schema, target.id)
    assert target.id not in schema.nodes
    assert validate(schema) == []
    labels = {n.label for n in schema.nodes.values() if n.type is NodeType.ACTIVITY}
    assert labels == {"A"}


def test_delete_split_removes_whole_block() -> None:
    schema = create_empty_schema("BlockLoeschen")
    schema = parallel_insert(schema, ["X", "Y"], after_node_id="start")
    split = next(n for n in schema.nodes.values() if n.type is NodeType.AND_SPLIT)
    schema = delete_node(schema, split.id)
    # only START and END remain; the balanced block is gone as a unit
    assert set(n.type for n in schema.nodes.values()) == {NodeType.START, NodeType.END}
    assert validate(schema) == []


def test_delete_split_removes_nested_block() -> None:
    schema = create_empty_schema("Verschachtelt")
    schema = _xor_after_start(schema, "P", "Q", upper=2)
    xsplit = next(n for n in schema.nodes.values() if n.type is NodeType.XOR_SPLIT)
    pbranch = next(n for n in schema.nodes.values() if n.label == "P")
    schema = parallel_insert(schema, ["P1", "P2"], after_node_id=pbranch.id)
    assert validate(schema) == []
    schema = delete_node(schema, xsplit.id)
    # The whole XOR block is gone as a unit; only the upstream writer remains.
    gateways = {NodeType.XOR_SPLIT, NodeType.XOR_JOIN, NodeType.AND_SPLIT, NodeType.AND_JOIN}
    assert not (gateways & {n.type for n in schema.nodes.values()})
    assert validate(schema) == []


def test_delete_join_directly_is_rejected() -> None:
    schema = create_empty_schema("JoinLoeschen")
    schema = parallel_insert(schema, ["A", "B"], after_node_id="start")
    join = next(n for n in schema.nodes.values() if n.type is NodeType.AND_JOIN)
    with pytest.raises(CorrectnessError):
        delete_node(schema, join.id)


def test_delete_start_or_end_is_rejected() -> None:
    schema = create_empty_schema("EndpunktLoeschen")
    with pytest.raises(CorrectnessError):
        delete_node(schema, "start")
    with pytest.raises(CorrectnessError):
        delete_node(schema, "end")


def test_delete_on_released_schema_is_rejected() -> None:
    schema = create_empty_schema("Loeschen")
    schema = serial_insert(schema, "S", after_node_id="start")
    act = next(n for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    released = release(schema)
    with pytest.raises(CorrectnessError):
        delete_node(released, act.id)


def test_delete_node_drops_dependent_bindings() -> None:
    from procworks import add_data_element, assign_service, connect_data
    from procworks.model import AccessMode, DataType

    schema = create_empty_schema("Bindungen")
    schema = serial_insert(schema, "A", after_node_id="start")
    act = next(n for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    schema = add_data_element(schema, "betrag", DataType.FLOAT)
    elem = next(iter(schema.data_elements.values()))
    schema = connect_data(schema, act.id, elem.id, AccessMode.WRITE)
    schema = assign_service(schema, act.id, "Pruefdienst")
    schema = delete_node(schema, act.id)
    assert act.id not in schema.service_bindings
    assert all(a.node_id != act.id for a in schema.data_accesses)
    assert validate(schema) == []


def test_delete_branch_dissolves_and_gateway_keeping_other_branch() -> None:
    schema = create_empty_schema("ZweigLoeschen")
    schema = parallel_insert(schema, ["A", "B"], after_node_id="start")
    a = next(n for n in schema.nodes.values() if n.label == "A")
    schema = delete_node(schema, a.id)
    # The gateway dissolves; only branch B survives inline between START and END.
    types = {n.type for n in schema.nodes.values()}
    assert NodeType.AND_SPLIT not in types
    assert NodeType.AND_JOIN not in types
    labels = {n.label for n in schema.nodes.values() if n.type is NodeType.ACTIVITY}
    assert labels == {"B"}
    b = next(n for n in schema.nodes.values() if n.label == "B")
    assert [e.source for e in schema.incoming(b.id)] == ["start"]
    assert [e.target for e in schema.outgoing(b.id)] == ["end"]
    assert validate(schema) == []


def test_delete_branch_dissolves_xor_gateway_keeping_other_branch() -> None:
    schema = create_empty_schema("XorZweig")
    schema = _xor_after_start(schema, "Ja", "Nein", upper=2)
    yes = next(n for n in schema.nodes.values() if n.label == "Ja")
    schema = delete_node(schema, yes.id)
    types = {n.type for n in schema.nodes.values()}
    assert NodeType.XOR_SPLIT not in types
    assert NodeType.XOR_JOIN not in types
    labels = {n.label for n in schema.nodes.values() if n.type is NodeType.ACTIVITY}
    assert labels == {"Erfassen", "Nein"}
    # The surviving branch is now an unconditional serial step.
    nein = next(n for n in schema.nodes.values() if n.label == "Nein")
    assert schema.incoming(nein.id)[0].condition is None
    assert validate(schema) == []


def test_delete_branch_keeps_gateway_when_two_branches_remain() -> None:
    schema = create_empty_schema("DreiZweige")
    schema = parallel_insert(schema, ["A", "B", "C"], after_node_id="start")
    a = next(n for n in schema.nodes.values() if n.label == "A")
    schema = delete_node(schema, a.id)
    # Three-way AND: removing one branch leaves a clean two-branch gateway
    # (no empty split -> join edge).
    split = next(n for n in schema.nodes.values() if n.type is NodeType.AND_SPLIT)
    join = next(n for n in schema.nodes.values() if n.type is NodeType.AND_JOIN)
    assert len(schema.outgoing(split.id)) == 2
    assert len(schema.incoming(join.id)) == 2
    labels = {n.label for n in schema.nodes.values() if n.type is NodeType.ACTIVITY}
    assert labels == {"B", "C"}
    assert validate(schema) == []
