# SPDX-License-Identifier: BUSL-1.1
"""Tests for the Correctness Validator (K1-K3) and change operations.

These tests demonstrate Correctness by Construction:
  * operations always yield a structurally correct schema (happy path),
  * the validator has teeth: it rejects hand-crafted broken schemas,
  * operation preconditions reject illegal calls.
"""

from __future__ import annotations

import random

import pytest
from staffing import staffed

from procworks import (
    BranchSpec,
    add_data_element,
    add_sync_edge,
    conditional_insert,
    connect_data,
    create_empty_schema,
    delete_node,
    insert_between_node_sets,
    insert_loop,
    move_node,
    operations,
    parallel_insert,
    release,
    remove_empty_branch,
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
    block_join,
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
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        serial_insert(schema, "X", after_node_id="does-not-exist")


def test_cannot_insert_after_end() -> None:
    schema = create_empty_schema("Fehler")
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        serial_insert(schema, "X", after_node_id="end")


def test_parallel_insert_requires_two_branches() -> None:
    schema = create_empty_schema("Fehler")
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        parallel_insert(schema, ["nur eine"], after_node_id="start")


def test_release_requires_entwurf_and_marks_released() -> None:
    schema = create_empty_schema("Release")
    schema = serial_insert(schema, "Schritt", after_node_id="start")
    released = release(staffed(schema))
    assert released.lifecycle_state is LifecycleState.RELEASED


def test_released_schema_is_not_editable() -> None:
    schema = create_empty_schema("Immutable")
    released = release(staffed(schema))
    with pytest.raises(CorrectnessError, match=r"\[R0\]"):
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
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        rename_node(schema, split.id, "x")


def test_rename_node_on_released_schema_is_rejected() -> None:
    schema = create_empty_schema("Umbenennen")
    schema = serial_insert(schema, "S", after_node_id="start")
    act = next(n for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    released = release(staffed(schema))
    with pytest.raises(CorrectnessError, match=r"\[R0\]"):
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
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        delete_node(schema, join.id)


def test_delete_start_or_end_is_rejected() -> None:
    schema = create_empty_schema("EndpunktLoeschen")
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        delete_node(schema, "start")
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        delete_node(schema, "end")


def test_delete_on_released_schema_is_rejected() -> None:
    schema = create_empty_schema("Loeschen")
    schema = serial_insert(schema, "S", after_node_id="start")
    act = next(n for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    released = release(staffed(schema))
    with pytest.raises(CorrectnessError, match=r"\[R0\]"):
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


def test_delete_last_activity_leaves_empty_xor_branch() -> None:
    """Deleting the sole activity of an XOR branch keeps it as an empty branch.

    The gateway stays (so "work only in the other branch" is expressible): the
    XOR split/join remain, the branch becomes a direct split -> join edge that
    keeps its K7 caption, and the schema is still correct by construction.
    """

    schema = create_empty_schema("XorZweig")
    schema = _xor_after_start(schema, "Ja", "Nein", upper=2)
    split = next(n for n in schema.nodes.values() if n.type is NodeType.XOR_SPLIT)
    join = next(n for n in schema.nodes.values() if n.type is NodeType.XOR_JOIN)
    yes = next(n for n in schema.nodes.values() if n.label == "Ja")
    schema = delete_node(schema, yes.id)

    # The gateway survives and the deleted branch is now a direct split -> join.
    assert NodeType.XOR_SPLIT in {n.type for n in schema.nodes.values()}
    labels = {n.label for n in schema.nodes.values() if n.type is NodeType.ACTIVITY}
    assert labels == {"Erfassen", "Nein"}
    empty_edge = next(
        e for e in schema.edges if e.source == split.id and e.target == join.id
    )
    assert empty_edge.condition == "x < 2"  # the emptied branch keeps its cell
    assert validate(schema) == []


def test_remove_empty_branch_dissolves_xor_gateway_keeping_other_branch() -> None:
    """Manually removing the empty branch collapses the two-branch XOR."""

    schema = create_empty_schema("XorZweig")
    schema = _xor_after_start(schema, "Ja", "Nein", upper=2)
    split = next(n for n in schema.nodes.values() if n.type is NodeType.XOR_SPLIT)
    yes = next(n for n in schema.nodes.values() if n.label == "Ja")
    schema = delete_node(schema, yes.id)
    schema = remove_empty_branch(schema, split.id)

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


# --- K1: block structure, not merely balanced counts ----------------------
#
# Counting splits against joins is too weak: two *crossed* blocks have perfectly
# balanced counts. These schemas are the reason K1 pairs each split with the
# join that closes it on every branch. Both were accepted by an earlier
# validator, could be released, and produced instances that could never
# complete (the AND join inherits SKIPPED from a deselected XOR branch and
# cascades it to END, leaving the instance RUNNING with an empty worklist).


def _crossed_and_blocks() -> ProcessSchema:
    """Two AND blocks that overlap instead of nesting: s1 -> s2 ... j1 -> j2."""

    return ProcessSchema(
        id="crossed",
        name="Gekreuzt",
        nodes={
            "start": Node(id="start", type=NodeType.START),
            "s1": Node(id="s1", type=NodeType.AND_SPLIT),
            "s2": Node(id="s2", type=NodeType.AND_SPLIT),
            "p2": Node(id="p2", type=NodeType.ACTIVITY, label="P2"),
            "q1": Node(id="q1", type=NodeType.ACTIVITY, label="Q1"),
            "q2": Node(id="q2", type=NodeType.ACTIVITY, label="Q2"),
            "j1": Node(id="j1", type=NodeType.AND_JOIN),
            "j2": Node(id="j2", type=NodeType.AND_JOIN),
            "end": Node(id="end", type=NodeType.END),
        },
        edges=[
            ControlEdge(source="start", target="s1"),
            ControlEdge(source="s1", target="s2"),
            ControlEdge(source="s1", target="p2"),
            ControlEdge(source="s2", target="q1"),
            ControlEdge(source="s2", target="q2"),
            ControlEdge(source="q1", target="j1"),
            ControlEdge(source="p2", target="j1"),
            ControlEdge(source="q2", target="j2"),
            ControlEdge(source="j1", target="j2"),
            ControlEdge(source="j2", target="end"),
        ],
    )


def test_crossed_blocks_are_rejected_although_gateway_counts_balance() -> None:
    schema = _crossed_and_blocks()
    # The weak check this replaces would have been satisfied here:
    assert sum(1 for n in schema.nodes.values() if n.type is NodeType.AND_SPLIT) == sum(
        1 for n in schema.nodes.values() if n.type is NodeType.AND_JOIN
    )
    findings = validate(schema)
    assert any(
        f.rule == "K1" and "not properly nested" in f.message for f in findings
    ), findings


def test_split_closed_by_the_wrong_join_type_is_rejected() -> None:
    """AND split closed by an XOR join and vice versa -- counts still balance."""

    schema = ProcessSchema(
        id="mismatched",
        name="Falscher Join",
        nodes={
            "start": Node(id="start", type=NodeType.START),
            "as": Node(id="as", type=NodeType.AND_SPLIT),
            "a": Node(id="a", type=NodeType.ACTIVITY, label="A"),
            "b": Node(id="b", type=NodeType.ACTIVITY, label="B"),
            "xj": Node(id="xj", type=NodeType.XOR_JOIN),
            "xs": Node(id="xs", type=NodeType.XOR_SPLIT),
            "c": Node(id="c", type=NodeType.ACTIVITY, label="C"),
            "d": Node(id="d", type=NodeType.ACTIVITY, label="D"),
            "aj": Node(id="aj", type=NodeType.AND_JOIN),
            "end": Node(id="end", type=NodeType.END),
        },
        edges=[
            ControlEdge(source="start", target="as"),
            ControlEdge(source="as", target="a"),
            ControlEdge(source="as", target="b"),
            ControlEdge(source="a", target="xj"),
            ControlEdge(source="b", target="xj"),
            ControlEdge(source="xj", target="xs"),
            ControlEdge(source="xs", target="c"),
            ControlEdge(source="xs", target="d"),
            ControlEdge(source="c", target="aj"),
            ControlEdge(source="d", target="aj"),
            ControlEdge(source="aj", target="end"),
        ],
    )
    messages = [f.message for f in validate(schema) if f.rule == "K1"]
    assert any("AND_SPLIT 'as' is closed by XOR_JOIN" in m for m in messages), messages
    assert any("XOR_SPLIT 'xs' is closed by AND_JOIN" in m for m in messages), messages


def test_crossed_block_cannot_be_released() -> None:
    """The Stufe-B gate must not be the only thing standing in the way either."""

    schema = _crossed_and_blocks()
    with pytest.raises(CorrectnessError) as exc:
        release(staffed(schema))
    assert any(f.rule == "K1" for f in exc.value.findings), exc.value.findings


def test_properly_nested_blocks_still_validate() -> None:
    """Guard against over-tightening: real nesting must stay accepted."""

    schema = create_empty_schema("Sauber verschachtelt")
    schema = parallel_insert(schema, ["A", "B"], after_node_id="start")
    branch_a = next(n for n in schema.nodes.values() if n.label == "A")
    schema = parallel_insert(schema, ["A1", "A2"], after_node_id=branch_a.id)
    assert validate(schema) == []


# --- No-Bypass: the operations cannot construct a crossed block -----------


def test_every_schema_returning_operation_reaches_the_validator() -> None:
    """Architectural guard: no change operation may commit without validating.

    The K1 defect of 2026-09-08 was only *reachable* because one entry point
    (the BPMN import) builds nodes and edges wholesale instead of going through
    the operations. This guard makes sure no second such path appears by
    accident inside ``operations.py`` itself.
    """

    import ast
    import pathlib

    src = pathlib.Path(operations.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def calls_of(fn: ast.FunctionDef) -> set[str]:
        return {
            n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    def validates(name: str, seen: frozenset[str] = frozenset()) -> bool:
        """Is the validator reachable from this function, transitively?

        A plain "does it call some local helper" test is not enough -- almost
        every operation calls private helpers, so that would excuse anything.
        """

        if name in seen or name not in funcs:
            return False
        names = calls_of(funcs[name])
        if {"raise_if_invalid", "validate"} & names:
            return True
        return any(validates(n, seen | {name}) for n in names & funcs.keys())

    offenders: list[str] = []
    for name, fn in funcs.items():
        if name.startswith("_"):
            continue
        if "ProcessSchema" not in (ast.unparse(fn.returns) if fn.returns else ""):
            continue
        body = ast.unparse(fn)
        # Pure metadata toggles touch no node or edge and need no validation.
        if "ControlEdge(" not in body and ".edges" not in body and ".nodes" not in body:
            continue
        if not validates(name):
            offenders.append(name)
    assert offenders == [], f"operations without validate-before-commit: {offenders}"


def test_operations_never_produce_a_crossed_block() -> None:
    """Property check over *all* structure-mutating operations.

    Every control edge an operation creates is either a serial splice on one
    existing edge or part of a complete symmetric block, so a crossing cannot
    arise. This pins that down empirically: after each accepted operation the
    schema must validate and every split must pair with exactly one join.
    Deterministic seed so a failure is reproducible.
    """

    rng = random.Random(20260908)
    for run in range(60):
        schema = add_data_element(
            create_empty_schema(f"prop{run}"), "betrag", DataType.INTEGER, element_id="betrag"
        )
        schema = add_data_element(schema, "wdh", DataType.BOOLEAN, element_id="wdh")
        schema = serial_insert(schema, "Erfassen", after_node_id="start")
        writer = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
        schema = connect_data(schema, writer, "betrag", AccessMode.WRITE)
        schema = connect_data(schema, writer, "wdh", AccessMode.WRITE)

        for _ in range(rng.randint(1, 10)):
            anchors = [
                n.id for n in schema.nodes.values()
                if n.type in (NodeType.ACTIVITY, NodeType.START)
            ]
            activities = [n.id for n in schema.nodes.values() if n.type is NodeType.ACTIVITY]
            splits = [
                n.id for n in schema.nodes.values()
                if n.type in (NodeType.AND_SPLIT, NodeType.XOR_SPLIT)
            ]
            choice = rng.choice(
                ["serial", "parallel", "cond", "loop", "delete", "move", "empty", "sync", "between"]
            )
            try:
                if choice == "serial":
                    schema = serial_insert(schema, "X", after_node_id=rng.choice(anchors))
                elif choice == "parallel":
                    schema = parallel_insert(
                        schema, ["P1", "P2"], after_node_id=rng.choice(anchors)
                    )
                elif choice == "cond":
                    schema = conditional_insert(
                        schema, writer, discriminator="betrag",
                        branches=[BranchSpec(label="k", upper=100), BranchSpec(label="g")],
                    )
                elif choice == "loop":
                    schema = insert_loop(
                        schema, writer, "Nacharbeit", discriminator="wdh", repeat_value=True
                    )
                elif choice == "delete":
                    candidates = [
                        n.id for n in schema.nodes.values()
                        if n.type not in (NodeType.START, NodeType.END)
                    ]
                    if candidates:
                        schema = delete_node(schema, rng.choice(candidates))
                elif choice == "move" and activities:
                    schema = move_node(schema, rng.choice(activities), rng.choice(anchors))
                elif choice == "empty" and splits:
                    schema = remove_empty_branch(schema, rng.choice(splits))
                elif choice in ("sync", "between") and len(activities) >= 2:
                    a, b = rng.sample(activities, 2)
                    schema = (
                        add_sync_edge(schema, a, b) if choice == "sync"
                        else insert_between_node_sets(schema, "Q", [a], [b])
                    )
            except CorrectnessError as exc:
                # Rejecting a precondition is fine (OP), and so is a semantic
                # rule the caller violated (K4/K6/K7/D1/D2). What must never
                # happen is a rejection on K1/K2/K3: keeping the graph
                # structurally intact is the operation's own job, so such a
                # finding means the operation built a broken graph itself.
                structural = {f.rule for f in exc.findings} & {"K1", "K2", "K3"}
                assert not structural, (
                    f"{choice} produced a structurally broken graph: {exc.findings}"
                )
                continue

            assert validate(schema) == []
            for split_id in [
                n.id for n in schema.nodes.values()
                if n.type in (NodeType.AND_SPLIT, NodeType.XOR_SPLIT)
            ]:
                block_join(schema, split_id)  # raises ValueError if not nested
