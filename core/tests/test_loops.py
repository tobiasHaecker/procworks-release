# SPDX-License-Identifier: BUSL-1.1
"""K6: structured REPEAT-UNTIL loops (Schleifen-Konzept, stages S1-S3).

Pins the constructive loop guarantee: a loop can only exist properly paired
(K6a), with a decidable exit condition (K6b) that is freshly written on every
path through the body (K6c) and never empty (K6d). The loop-back edge is never
stored: the graph stays acyclic, and the engine repeats by resetting the block
markings. Since stage S3 the body may also contain SUBPROCESS nodes and
automatic activities (the former K6e restriction): the reset drops the body's
child-instance links so every iteration spawns a fresh child, and the
external-task machinery re-materialises a fresh task per iteration.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from staffing import TEST_AGENT_ID, staffed

from procworks import (
    AccessMode,
    BranchSpec,
    DataType,
    LoopCell,
    add_data_element,
    assign_service,
    complete_activity,
    conditional_insert,
    connect_data,
    create_empty_schema,
    delete_node,
    disconnect_data,
    export_bpmn,
    import_bpmn,
    insert_loop,
    insert_subprocess,
    instantiate,
    move_node,
    release,
    serial_insert,
    set_automation,
    set_deadline,
    set_loop_decision,
    set_mail_binding,
    set_time_constraint,
    update_agent,
    validate,
)
from procworks.api import app
from procworks.execution import ExecutionContext, _propagate_completion
from procworks.integration_runtime import ExternalTaskError, ExternalTaskRuntime
from procworks.model import (
    AutomationKind,
    InstanceState,
    MailBinding,
    NodeState,
    NodeType,
    TimeConstraint,
    XorDecisionKind,
)
from procworks.operations import CorrectnessError
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)

client = TestClient(app)


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _typed(schema: object, node_type: NodeType) -> str:
    return next(n.id for n in schema.nodes.values() if n.type is node_type)  # type: ignore[attr-defined]


def _loop_schema():
    """start -> Erfassen -> [LOOP: Pruefen (writes 'na')] -> end."""

    schema = create_empty_schema("Schleife")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "na", DataType.BOOLEAN, element_id="na")
    return insert_loop(
        schema, _nid(schema, "Erfassen"), "Pruefen", discriminator="na"
    )


# --- modelling: insert_loop -----------------------------------------------


def test_insert_loop_builds_a_paired_decidable_block() -> None:
    schema = _loop_schema()
    ls = _typed(schema, NodeType.LOOP_START)
    le = _typed(schema, NodeType.LOOP_END)
    body = _nid(schema, "Pruefen")

    assert schema.outgoing(_nid(schema, "Erfassen"))[0].target == ls
    assert schema.outgoing(ls)[0].target == body
    assert schema.outgoing(body)[0].target == le
    assert schema.loop_decisions[le].discriminator == "na"
    # Automatic structure completion: the body writes the discriminator (K6c).
    assert any(
        a.node_id == body and a.element_id == "na" and a.mandatory
        for a in schema.data_accesses
    )
    assert validate(schema) == []


def test_insert_loop_rejects_bad_discriminators() -> None:
    schema = create_empty_schema("S")
    schema = serial_insert(schema, "A", after_node_id="start")
    schema = add_data_element(schema, "zahl", DataType.INTEGER, element_id="zahl")

    with pytest.raises(CorrectnessError):
        insert_loop(schema, _nid(schema, "A"), "B", discriminator="zahl")
    with pytest.raises(CorrectnessError):
        insert_loop(schema, _nid(schema, "A"), "B", discriminator="gibtsnicht")
    with pytest.raises(CorrectnessError):
        insert_loop(
            schema, schema.end_node().id, "B", discriminator="zahl"
        )


# --- K6 validator rules ----------------------------------------------------


def test_k6c_rejects_removing_the_only_discriminator_write() -> None:
    schema = _loop_schema()
    body = _nid(schema, "Pruefen")

    with pytest.raises(CorrectnessError) as err:
        disconnect_data(schema, body, "na")
    assert any(f.rule == "K6" for f in err.value.findings)


def test_k6c_allows_relocating_the_write_within_the_body() -> None:
    schema = _loop_schema()
    body = _nid(schema, "Pruefen")
    schema = serial_insert(schema, "Nacharbeit", after_node_id=body)
    zwei = _nid(schema, "Nacharbeit")
    schema = connect_data(schema, zwei, "na", AccessMode.WRITE)

    moved = disconnect_data(schema, body, "na")
    assert validate(moved) == []


def test_s3_automatic_activity_in_the_body_iterates_with_fresh_tasks() -> None:
    """S3: the former K6e automatic-activity restriction is lifted.

    Every iteration re-materialises a fresh external task for the body step (a
    COMPLETED task does not block the open-task dedup), the worker's
    discriminator report decides the repeat, and the stale round-1 task can
    never complete a later round (exactly-once via the LOCKED-state guard).
    """

    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    schema = _loop_schema()
    body = _nid(schema, "Pruefen")
    schema = assign_service(schema, body, "Roboter", automatic=True)
    schema = set_automation(
        schema, body, AutomationKind.EXTERNAL_TASK, topic="pruef"
    )
    schema = release(staffed(schema))
    schemas.put(schema)

    context = ExecutionContext(make_resolver(schemas), instances)
    inst = instantiate(schema, context=context)
    complete_activity(inst, schema, _nid(schema, "Erfassen"), context=context)

    runtime = ExternalTaskRuntime(
        InMemoryExternalTaskStore(), instances, lambda _i: schema, context
    )
    first = runtime.fetch_and_lock("w1", ["pruef"], lock_ms=10_000)[0]
    runtime.complete(first.id, "w1", {"na": True})  # -> repeat

    second = runtime.fetch_and_lock("w1", ["pruef"], lock_ms=10_000)[0]
    assert second.id != first.id
    with pytest.raises(ExternalTaskError):  # round-1 task is spent
        runtime.complete(first.id, "w1", {"na": False})
    runtime.complete(second.id, "w1", {"na": False})  # -> exit

    done = instances.get(inst.id)
    assert done is not None
    assert done.state is InstanceState.COMPLETED
    assert done.loop_iterations[_typed(schema, NodeType.LOOP_END)] == 1


def test_s3_subprocess_in_the_body_spawns_a_fresh_child_each_iteration() -> None:
    """S3: the former K6e SUBPROCESS restriction is lifted.

    The loop reset drops the body's child-instance link, so the re-activated
    SUBPROCESS node spawns a fresh child per iteration instead of waiting
    forever on the completed round-1 child; a replay of that stale child can
    no longer join the parent (guard in ``_propagate_completion``).
    """

    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    resolver = make_resolver(schemas)

    child = create_empty_schema("Zulieferung", schema_id="kind")
    child = serial_insert(child, "Zuarbeit", after_node_id="start")
    child = release(staffed(child))
    schemas.put(child)

    schema = _loop_schema()
    schema = insert_subprocess(
        schema,
        _typed(schema, NodeType.LOOP_START),
        "kind",
        1,
        label="Teilprozess",
        resolver=resolver,
    )
    assert validate(schema) == []
    schema = release(staffed(schema))
    schemas.put(schema)

    context = ExecutionContext(resolver, instances)
    inst = instantiate(schema, context=context)
    complete_activity(inst, schema, _nid(schema, "Erfassen"), context=context)
    sub = _typed(schema, NodeType.SUBPROCESS)
    body = _nid(schema, "Pruefen")

    def finish_child() -> str:
        parent = instances.get(inst.id)
        assert parent is not None
        child_id = parent.child_instances[sub]
        child_inst = instances.get(child_id)
        assert child_inst is not None
        complete_activity(
            child_inst, child, _nid(child, "Zuarbeit"), context=context
        )
        return child_id

    first_child = finish_child()
    parent = instances.get(inst.id)
    assert parent is not None
    complete_activity(parent, schema, body, {"na": True}, context=context)  # repeat

    parent = instances.get(inst.id)
    assert parent is not None
    second_child = parent.child_instances[sub]
    assert second_child != first_child

    # Replaying the completed round-1 child must not touch round 2's markings.
    stale = instances.get(first_child)
    assert stale is not None
    before = parent.model_copy(deep=True)
    _propagate_completion(stale, context)
    after = instances.get(inst.id)
    assert after is not None
    assert after.node_states == before.node_states

    finish_child()
    parent = instances.get(inst.id)
    assert parent is not None
    complete_activity(parent, schema, body, {"na": False}, context=context)  # exit

    done = instances.get(inst.id)
    assert done is not None
    assert done.state is InstanceState.COMPLETED
    assert done.loop_iterations[_typed(schema, NodeType.LOOP_END)] == 1
    first_done = instances.get(first_child)
    assert first_done is not None
    assert first_done.state is InstanceState.COMPLETED


def test_k6a_flags_an_unpaired_loop_start() -> None:
    schema = _loop_schema()
    broken = schema.model_copy(deep=True)
    le = _typed(broken, NodeType.LOOP_END)
    # Simulate a bypass: turn the LOOP_END into a plain activity.
    broken.nodes[le].type = NodeType.ACTIVITY
    findings = validate(broken)
    assert any(f.rule == "K6" for f in findings)


def test_k6a_flags_a_loop_end_claimed_by_two_starts() -> None:
    """Two LOOP_STARTs whose forward scan meets the same LOOP_END (K6a).

    Not constructible through the operations (they always build paired
    blocks); the validator is the No-Bypass backstop, so the graph is built
    by hand: an AND block whose two branches are LOOP_STARTs converging on a
    single LOOP_END.
    """

    from procworks.model import ControlEdge, Node, ProcessSchema

    schema = ProcessSchema(
        id="doppel",
        name="Doppel",
        nodes={
            "start": Node(id="start", type=NodeType.START),
            "sp": Node(id="sp", type=NodeType.AND_SPLIT),
            "l1": Node(id="l1", type=NodeType.LOOP_START),
            "l2": Node(id="l2", type=NodeType.LOOP_START),
            "j": Node(id="j", type=NodeType.AND_JOIN),
            "a": Node(id="a", type=NodeType.ACTIVITY, label="Arbeit"),
            "le": Node(id="le", type=NodeType.LOOP_END),
            "end": Node(id="end", type=NodeType.END),
        },
        edges=[
            ControlEdge(source="start", target="sp"),
            ControlEdge(source="sp", target="l1"),
            ControlEdge(source="sp", target="l2"),
            ControlEdge(source="l1", target="j"),
            ControlEdge(source="l2", target="j"),
            ControlEdge(source="j", target="a"),
            ControlEdge(source="a", target="le"),
            ControlEdge(source="le", target="end"),
        ],
    )
    findings = validate(schema)
    assert any("claimed by two loop starts" in f.message for f in findings)


def test_k6b_flags_a_stale_loop_decision() -> None:
    schema = _loop_schema()
    broken = schema.model_copy(deep=True)
    decision = broken.loop_decisions.pop(_typed(broken, NodeType.LOOP_END))
    broken.loop_decisions[_nid(broken, "Erfassen")] = decision
    findings = validate(broken)
    assert any(f.rule == "K6" for f in findings)


# --- S3: THRESHOLD/ENUM repeat/exit partitions -----------------------------


def _cells_loop_schema(data_type: DataType, cells: list[LoopCell]):
    """start -> Erfassen -> [LOOP: Pruefen (writes 'disc')] -> end."""

    schema = create_empty_schema("Schleife")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "disc", data_type, element_id="disc")
    return insert_loop(
        schema, _nid(schema, "Erfassen"), "Pruefen", discriminator="disc", cells=cells
    )


def test_s3_threshold_cells_repeat_while_the_count_is_high() -> None:
    """Repeat while disc >= 1: [<1 -> exit, >=1 -> repeat] tiles the axis."""

    schema = _cells_loop_schema(
        DataType.INTEGER,
        [LoopCell(repeat=False, upper=1), LoopCell(repeat=True)],
    )
    assert validate(schema) == []
    schema = release(staffed(schema))
    body = _nid(schema, "Pruefen")
    le = _typed(schema, NodeType.LOOP_END)

    inst = instantiate(schema)
    inst = complete_activity(inst, schema, _nid(schema, "Erfassen"))
    inst = complete_activity(inst, schema, body, {"disc": 3})
    assert inst.node_states[body] is NodeState.ACTIVATED
    inst = complete_activity(inst, schema, body, {"disc": 0})
    assert inst.state is InstanceState.COMPLETED
    assert inst.loop_iterations[le] == 1


def test_s3_enum_cells_repeat_on_listed_values() -> None:
    """Repeat while disc == 'nacharbeit'; every other string exits."""

    schema = _cells_loop_schema(
        DataType.STRING,
        [
            LoopCell(repeat=True, values=["nacharbeit"]),
            LoopCell(repeat=False, is_else=True),
        ],
    )
    assert validate(schema) == []
    schema = release(staffed(schema))
    body = _nid(schema, "Pruefen")

    inst = instantiate(schema)
    inst = complete_activity(inst, schema, _nid(schema, "Erfassen"))
    inst = complete_activity(inst, schema, body, {"disc": "nacharbeit"})
    assert inst.node_states[body] is NodeState.ACTIVATED
    inst = complete_activity(inst, schema, body, {"disc": "ok"})
    assert inst.state is InstanceState.COMPLETED


def test_s3_k6b_rejects_partitions_without_both_classes() -> None:
    """All-repeat could never terminate, all-exit never repeat -- both K6."""

    with pytest.raises(CorrectnessError) as err:
        _cells_loop_schema(
            DataType.BOOLEAN,
            [LoopCell(repeat=True, bool_value=True), LoopCell(repeat=True, bool_value=False)],
        )
    assert any("exit cell" in f.message for f in err.value.findings)

    with pytest.raises(CorrectnessError) as err:
        _cells_loop_schema(
            DataType.BOOLEAN,
            [LoopCell(repeat=False, bool_value=True), LoopCell(repeat=False, bool_value=False)],
        )
    assert any("repeat cell" in f.message for f in err.value.findings)


def test_s3_k6b_rejects_malformed_or_mismatched_partitions() -> None:
    # A bounded last threshold cell does not tile the number line.
    with pytest.raises(CorrectnessError) as err:
        _cells_loop_schema(
            DataType.INTEGER,
            [LoopCell(repeat=False, upper=1), LoopCell(repeat=True, upper=5)],
        )
    assert any(f.rule == "K6" for f in err.value.findings)

    # A DATE element cannot be partitioned decidably.
    schema = create_empty_schema("S")
    schema = serial_insert(schema, "A", after_node_id="start")
    schema = add_data_element(schema, "datum", DataType.DATE, element_id="datum")
    with pytest.raises(CorrectnessError):
        insert_loop(
            schema,
            _nid(schema, "A"),
            "B",
            discriminator="datum",
            cells=[LoopCell(repeat=True, is_else=True)],
        )

    # A tampered kind (bypass) is caught by the validator backstop.
    good = _cells_loop_schema(
        DataType.INTEGER,
        [LoopCell(repeat=False, upper=1), LoopCell(repeat=True)],
    )
    broken = good.model_copy(deep=True)
    broken.loop_decisions[_typed(broken, NodeType.LOOP_END)].kind = (
        XorDecisionKind.ENUM
    )
    assert any(f.rule == "K6" for f in validate(broken))


def test_s3_set_loop_decision_retargets_the_exit_condition() -> None:
    schema = _loop_schema()
    body = _nid(schema, "Pruefen")
    le = _typed(schema, NodeType.LOOP_END)
    schema = add_data_element(schema, "fehler", DataType.INTEGER, element_id="fehler")

    # Without a guaranteed body write of the new discriminator K6c rejects.
    with pytest.raises(CorrectnessError) as err:
        set_loop_decision(
            schema,
            le,
            discriminator="fehler",
            cells=[LoopCell(repeat=False, upper=1), LoopCell(repeat=True)],
        )
    assert any(f.rule == "K6" for f in err.value.findings)

    schema = connect_data(schema, body, "fehler", AccessMode.WRITE)
    switched = set_loop_decision(
        schema,
        le,
        discriminator="fehler",
        cells=[LoopCell(repeat=False, upper=1), LoopCell(repeat=True)],
    )
    assert switched.loop_decisions[le].kind is XorDecisionKind.THRESHOLD
    assert validate(switched) == []

    # And back to the boolean shorthand.
    back = set_loop_decision(switched, le, discriminator="na", repeat_value=False)
    assert back.loop_decisions[le].cells == []
    assert back.loop_decisions[le].repeat_value is False
    assert validate(back) == []


# --- S3: max_iterations (hard brake + T2 accuracy) --------------------------


def _bounded_loop_schema(max_iterations: int):
    schema = create_empty_schema("Begrenzt")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "na", DataType.BOOLEAN, element_id="na")
    return insert_loop(
        schema,
        _nid(schema, "Erfassen"),
        "Pruefen",
        discriminator="na",
        max_iterations=max_iterations,
    )


def test_s3_max_iterations_forces_the_exit() -> None:
    """At the bound the loop exits even though the data says repeat."""

    schema = release(staffed(_bounded_loop_schema(2)))
    body = _nid(schema, "Pruefen")
    le = _typed(schema, NodeType.LOOP_END)

    inst = instantiate(schema)
    inst = complete_activity(inst, schema, _nid(schema, "Erfassen"))
    inst = complete_activity(inst, schema, body, {"na": True})  # run 1 -> repeat
    assert inst.node_states[body] is NodeState.ACTIVATED
    inst = complete_activity(inst, schema, body, {"na": True})  # run 2 -> brake
    assert inst.state is InstanceState.COMPLETED
    assert inst.loop_iterations[le] == 1


def test_s3_max_iterations_must_allow_a_repetition() -> None:
    """A bound of 1 would forbid every repetition -- K6b rejects it."""

    with pytest.raises(CorrectnessError) as err:
        _bounded_loop_schema(1)
    assert any(f.rule == "K6" for f in err.value.findings)


def test_s3_t2_charges_a_bounded_loop_body_per_iteration() -> None:
    """T2 counts the body max_iterations times; unbounded keeps one pass."""

    schema = _bounded_loop_schema(3)
    body = _nid(schema, "Pruefen")
    schema = set_time_constraint(schema, body, TimeConstraint(max_duration_seconds=60))
    schema = set_deadline(schema, 180)  # 3 x 60s fits exactly
    assert validate(schema) == []

    with pytest.raises(CorrectnessError) as err:
        set_deadline(schema, 179)
    assert any(f.rule == "T2" for f in err.value.findings)

    # Without the bound the documented one-pass approximation stays.
    unbounded = _loop_schema()
    unbounded = set_time_constraint(
        unbounded, _nid(unbounded, "Pruefen"), TimeConstraint(max_duration_seconds=60)
    )
    unbounded = set_deadline(unbounded, 60)
    assert validate(unbounded) == []


def test_s3_t2_multiplies_nested_bounded_loops() -> None:
    """A bounded loop inside a bounded loop multiplies (innermost first)."""

    schema = create_empty_schema("Verschachtelt")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "na", DataType.BOOLEAN, element_id="na")
    schema = add_data_element(schema, "nb", DataType.BOOLEAN, element_id="nb")
    schema = insert_loop(
        schema,
        _nid(schema, "Erfassen"),
        "Aussen",
        discriminator="na",
        max_iterations=2,
    )
    outer_start = _typed(schema, NodeType.LOOP_START)
    schema = insert_loop(
        schema, outer_start, "Innen", discriminator="nb", max_iterations=2
    )
    assert validate(schema) == []
    schema = set_time_constraint(
        schema, _nid(schema, "Innen"), TimeConstraint(max_duration_seconds=10)
    )
    # Inner: 2 x 10s = 20s per outer pass; outer: 2 x 20s = 40s in total.
    schema = set_deadline(schema, 40)
    assert validate(schema) == []
    with pytest.raises(CorrectnessError) as err:
        set_deadline(schema, 39)
    assert any(f.rule == "T2" for f in err.value.findings)


# --- delete / move semantics ----------------------------------------------


def test_delete_loop_end_is_rejected_delete_start_removes_the_block() -> None:
    schema = _loop_schema()
    ls = _typed(schema, NodeType.LOOP_START)
    le = _typed(schema, NodeType.LOOP_END)

    with pytest.raises(CorrectnessError):
        delete_node(schema, le)

    gone = delete_node(schema, ls)
    assert not any(n.type in (NodeType.LOOP_START, NodeType.LOOP_END) for n in gone.nodes.values())
    assert gone.loop_decisions == {}
    assert _nid(gone, "Erfassen") in gone.nodes
    assert validate(gone) == []


def test_deleting_the_sole_body_node_dissolves_the_loop() -> None:
    schema = _loop_schema()

    gone = delete_node(schema, _nid(schema, "Pruefen"))
    assert not any(n.type in (NodeType.LOOP_START, NodeType.LOOP_END) for n in gone.nodes.values())
    assert gone.loop_decisions == {}
    # Erfassen is reconnected straight to END.
    assert gone.outgoing(_nid(gone, "Erfassen"))[0].target == gone.end_node().id
    assert validate(gone) == []


def test_moving_the_sole_body_node_is_rejected() -> None:
    schema = _loop_schema()

    with pytest.raises(CorrectnessError) as err:
        move_node(schema, _nid(schema, "Pruefen"), after_node_id="start")
    assert "loop body" in err.value.findings[0].message


def test_moving_the_writer_out_of_the_body_is_rejected_k6c() -> None:
    schema = _loop_schema()
    body = _nid(schema, "Pruefen")
    schema = serial_insert(schema, "Zusatz", after_node_id=body)

    with pytest.raises(CorrectnessError) as err:
        move_node(schema, body, after_node_id="start")
    assert any(f.rule == "K6" for f in err.value.findings)


def test_delete_now_cleans_time_and_mail_annotations() -> None:
    """Regression: a step carrying a time constraint or mail binding was
    undeletable -- _drop_nodes left the stale key and T1/N2 rejected the
    deletion (validate-before-commit)."""

    schema = create_empty_schema("Putz")
    schema = serial_insert(schema, "A", after_node_id="start")
    schema = serial_insert(schema, "B", after_node_id="start")
    a = _nid(schema, "A")
    schema = set_time_constraint(schema, a, TimeConstraint(max_duration_seconds=60))
    schema = staffed(schema)
    # N3 verlangt eine Adresse fuer jeden moeglichen Empfaenger, sonst laesst
    # sich das Mail-Binding gar nicht erst setzen.
    schema = update_agent(schema, TEST_AGENT_ID, email="test@example.org")
    schema = set_mail_binding(
        schema, a, MailBinding(subject="Hallo", body="Text")
    )

    gone = delete_node(schema, a)
    assert a not in gone.time_constraints
    assert a not in gone.mail_bindings
    assert validate(gone) == []


# --- execution: repeat and exit -------------------------------------------


def test_loop_repeats_until_the_discriminator_releases_it() -> None:
    schema = release(staffed(_loop_schema()))
    erf = _nid(schema, "Erfassen")
    body = _nid(schema, "Pruefen")
    le = _typed(schema, NodeType.LOOP_END)

    inst = instantiate(schema)
    inst = complete_activity(inst, schema, erf)

    # Two repeats, then exit.
    inst = complete_activity(inst, schema, body, {"na": True})
    assert inst.node_states[body] is NodeState.ACTIVATED
    inst = complete_activity(inst, schema, body, {"na": True})
    assert inst.node_states[body] is NodeState.ACTIVATED
    assert inst.loop_iterations[le] == 2

    inst = complete_activity(inst, schema, body, {"na": False})
    assert inst.state.value == "COMPLETED"
    assert inst.node_states[body] is NodeState.COMPLETED
    assert inst.node_states[le] is NodeState.COMPLETED


def test_loop_inside_an_xor_branch_runs_and_completes() -> None:
    schema = create_empty_schema("Verschachtelt")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erf = _nid(schema, "Erfassen")
    schema = add_data_element(schema, "eilig", DataType.BOOLEAN, element_id="eilig")
    schema = connect_data(schema, erf, "eilig", AccessMode.WRITE)
    schema = conditional_insert(
        schema,
        after_node_id=erf,
        discriminator="eilig",
        branches=[
            BranchSpec(label="Schnell", bool_value=True),
            BranchSpec(label="Gruendlich", bool_value=False),
        ],
    )
    schema = add_data_element(schema, "na", DataType.BOOLEAN, element_id="na")
    schema = insert_loop(
        schema, _nid(schema, "Gruendlich"), "Nachpruefen", discriminator="na"
    )
    assert validate(schema) == []
    schema = release(staffed(schema))

    inst = instantiate(schema)
    inst = complete_activity(inst, schema, erf, {"eilig": False})
    inst = complete_activity(inst, schema, _nid(schema, "Gruendlich"))
    body = _nid(schema, "Nachpruefen")
    inst = complete_activity(inst, schema, body, {"na": True})
    assert inst.node_states[body] is NodeState.ACTIVATED
    inst = complete_activity(inst, schema, body, {"na": False})
    assert inst.state.value == "COMPLETED"


# --- boundary: BPMN + API --------------------------------------------------


def test_s3_bpmn_round_trip_preserves_the_loop() -> None:
    """Export as gateway pair + back flow; import drops the back flow again.

    The loop-back edge exists only in the interchange document -- the imported
    schema is acyclic with LOOP_START/LOOP_END restored and the identical
    structured decision from the extension.
    """

    schema = _loop_schema()
    le = _typed(schema, NodeType.LOOP_END)
    ls = _typed(schema, NodeType.LOOP_START)

    xml = export_bpmn(schema)
    assert f'sourceRef="{le}" targetRef="{ls}"' in xml, "back flow missing"
    assert "== true" in xml, "repeat predicate missing on the back flow"

    imported = import_bpmn(xml)
    assert imported.nodes[ls].type is NodeType.LOOP_START
    assert imported.nodes[le].type is NodeType.LOOP_END
    assert not any(e.source == le and e.target == ls for e in imported.edges), (
        "the loop-back edge must never be stored"
    )
    assert imported.loop_decisions[le] == schema.loop_decisions[le]
    assert validate(imported) == []


def test_s3_bpmn_round_trip_keeps_cells_and_bound() -> None:
    schema = create_empty_schema("Schleife")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "disc", DataType.INTEGER, element_id="disc")
    schema = insert_loop(
        schema,
        _nid(schema, "Erfassen"),
        "Pruefen",
        discriminator="disc",
        cells=[LoopCell(repeat=False, upper=1), LoopCell(repeat=True)],
        max_iterations=3,
    )
    le = _typed(schema, NodeType.LOOP_END)

    imported = import_bpmn(export_bpmn(schema))
    decision = imported.loop_decisions[le]
    assert decision == schema.loop_decisions[le]
    assert decision.kind is XorDecisionKind.THRESHOLD
    assert decision.max_iterations == 3
    assert validate(imported) == []


def test_s3_bpmn_round_trip_keeps_nested_loops() -> None:
    schema = create_empty_schema("Verschachtelt")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "na", DataType.BOOLEAN, element_id="na")
    schema = add_data_element(schema, "nb", DataType.BOOLEAN, element_id="nb")
    schema = insert_loop(
        schema, _nid(schema, "Erfassen"), "Aussen", discriminator="na"
    )
    schema = insert_loop(
        schema, _typed(schema, NodeType.LOOP_START), "Innen", discriminator="nb"
    )
    imported = import_bpmn(export_bpmn(schema))
    starts = [n for n in imported.nodes.values() if n.type is NodeType.LOOP_START]
    ends = [n for n in imported.nodes.values() if n.type is NodeType.LOOP_END]
    assert len(starts) == 2 and len(ends) == 2
    assert imported.loop_decisions == schema.loop_decisions
    assert validate(imported) == []


def test_s3_bpmn_foreign_cycle_without_decision_is_rejected() -> None:
    """A recognised loop without a structured decision fails K6b (No-Bypass)."""

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
        id="defs" targetNamespace="https://example">
      <bpmn:process id="p" name="Fremd" isExecutable="true">
        <bpmn:startEvent id="start"/>
        <bpmn:exclusiveGateway id="gw_in"/>
        <bpmn:task id="arbeit" name="Arbeiten"/>
        <bpmn:exclusiveGateway id="gw_out"/>
        <bpmn:endEvent id="end"/>
        <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="gw_in"/>
        <bpmn:sequenceFlow id="f2" sourceRef="gw_in" targetRef="arbeit"/>
        <bpmn:sequenceFlow id="f3" sourceRef="arbeit" targetRef="gw_out"/>
        <bpmn:sequenceFlow id="f4" sourceRef="gw_out" targetRef="end"/>
        <bpmn:sequenceFlow id="f5" sourceRef="gw_out" targetRef="gw_in"/>
      </bpmn:process>
    </bpmn:definitions>"""
    with pytest.raises(CorrectnessError) as err:
        import_bpmn(xml)
    assert any(f.rule == "K6" for f in err.value.findings)


def test_loop_insert_via_api_and_bpmn_export() -> None:
    resp = client.post("/schemas", json={"name": "Loop-API"})
    sid = resp.json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    erf = next(nid for nid, n in nodes.items() if n.get("label") == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "na", "data_type": "BOOLEAN", "element_id": "na"},
    )

    resp = client.post(
        f"/schemas/{sid}/loop-insert",
        json={"label": "Pruefen", "after_node_id": erf, "discriminator": "na"},
    )
    assert resp.status_code == 200
    schema = resp.json()
    assert any(n["type"] == "LOOP_END" for n in schema["nodes"].values())
    assert schema["loop_decisions"]

    resp = client.get(f"/schemas/{sid}/bpmn")
    assert resp.status_code == 200
    assert "exclusiveGateway" in resp.text and "loopflow_1" in resp.text

    resp = client.post(
        f"/schemas/{sid}/loop-insert",
        json={"label": "X", "after_node_id": erf, "discriminator": "fehlt"},
    )
    assert resp.status_code == 422


def test_s3_loop_cells_and_decision_endpoint_via_api() -> None:
    resp = client.post("/schemas", json={"name": "Loop-Cells-API"})
    sid = resp.json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    erf = next(nid for nid, n in nodes.items() if n.get("label") == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "fehler", "data_type": "INTEGER", "element_id": "fehler"},
    )

    resp = client.post(
        f"/schemas/{sid}/loop-insert",
        json={
            "label": "Pruefen",
            "after_node_id": erf,
            "discriminator": "fehler",
            "cells": [{"repeat": False, "upper": 1}, {"repeat": True}],
        },
    )
    assert resp.status_code == 200
    schema = resp.json()
    le = next(nid for nid, n in schema["nodes"].items() if n["type"] == "LOOP_END")
    assert schema["loop_decisions"][le]["kind"] == "THRESHOLD"

    # An all-repeat partition could never terminate -> 422, schema unchanged.
    resp = client.post(
        f"/schemas/{sid}/loop-decision",
        json={
            "node_id": le,
            "discriminator": "fehler",
            "cells": [{"repeat": True, "upper": 1}, {"repeat": True}],
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        f"/schemas/{sid}/loop-decision",
        json={
            "node_id": le,
            "discriminator": "fehler",
            "cells": [{"repeat": True, "upper": 3}, {"repeat": False}],
        },
    )
    assert resp.status_code == 200
    cells = resp.json()["loop_decisions"][le]["cells"]
    assert [c["repeat"] for c in cells] == [True, False]
