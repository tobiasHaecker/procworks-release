# SPDX-License-Identifier: BUSL-1.1
"""Tests for sub-process execution (composition at runtime).

A SUBPROCESS node spawns a child instance of its pinned target schema, passes
the bound input data, stays RUNNING while the child runs, and on the child's
completion writes the mapped output back into the parent before advancing.
Without an ExecutionContext the SUBPROCESS node stays an opaque black box.
"""

from __future__ import annotations

from procworks import (
    ExecutionContext,
    add_data_element,
    complete_activity,
    connect_data,
    create_empty_schema,
    insert_subprocess,
    instantiate,
    release,
    serial_insert,
    worklist,
)
from procworks.model import (
    AccessMode,
    DataType,
    InstanceState,
    NodeState,
    NodeType,
    ProcessSchema,
)
from procworks.store import InMemoryInstanceStore


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


def _activity_id(schema: ProcessSchema) -> str:
    return next(n.id for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)


def _subprocess_id(schema: ProcessSchema) -> str:
    return next(n.id for n in schema.nodes.values() if n.type is NodeType.SUBPROCESS)


def test_subprocess_spawns_and_joins_child() -> None:
    sub = create_empty_schema("Sub", schema_id="sub_target")
    sub = serial_insert(sub, "Bearbeiten", after_node_id="start")
    sub = add_data_element(sub, "eingabe", DataType.FLOAT, element_id="eingabe")
    sub = add_data_element(sub, "ergebnis", DataType.FLOAT, element_id="ergebnis")
    child_act = _activity_id(sub)
    # the child guarantees to produce its output so the composition is runnable
    sub = connect_data(sub, child_act, "ergebnis", AccessMode.WRITE)
    sub = release(sub)

    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = serial_insert(parent, "Vorbereiten", after_node_id="start")
    parent = add_data_element(parent, "betrag", DataType.FLOAT, element_id="betrag")
    parent = add_data_element(parent, "summe", DataType.FLOAT, element_id="summe")
    pre_act = _activity_id(parent)
    build_resolver = _resolver_for(sub)
    parent = insert_subprocess(
        parent,
        pre_act,
        "sub_target",
        1,
        input_mapping={"eingabe": "betrag"},
        output_mapping={"ergebnis": "summe"},
        resolver=build_resolver,
    )
    parent = release(parent, build_resolver)
    sub_node = _subprocess_id(parent)

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(sub, parent), store)

    inst = instantiate(parent, context=context)
    assert worklist(inst, parent) == [pre_act]

    # completing the preceding activity reaches the SUBPROCESS node, which
    # spawns the child instance with the mapped input and then waits.
    inst = complete_activity(inst, parent, pre_act, {"betrag": 100.0}, context=context)
    assert inst.node_states[sub_node] is NodeState.RUNNING
    assert inst.state is InstanceState.RUNNING

    child_id = inst.child_instances[sub_node]
    child = store.get(child_id)
    assert child is not None
    assert child.parent_instance_id == inst.id
    assert child.parent_node_id == sub_node
    assert child.data_values["eingabe"] == 100.0
    assert worklist(child, sub) == [child_act]

    # completing the child finishes it and joins back into the parent, writing
    # the mapped output and driving the parent to completion.
    child = complete_activity(child, sub, child_act, {"ergebnis": 250.0}, context=context)
    assert child.state is InstanceState.COMPLETED

    joined = store.get(inst.id)
    assert joined is not None
    assert joined.node_states[sub_node] is NodeState.COMPLETED
    assert joined.data_values["summe"] == 250.0
    assert joined.state is InstanceState.COMPLETED


def test_subprocess_with_empty_child_completes_inline() -> None:
    sub = create_empty_schema("Leer", schema_id="empty_sub")  # START -> END
    sub = release(sub)

    parent = create_empty_schema("HauptInline", schema_id="parent_inline")
    parent = serial_insert(parent, "Schritt", after_node_id="start")
    pre_act = _activity_id(parent)
    build_resolver = _resolver_for(sub)
    parent = insert_subprocess(parent, pre_act, "empty_sub", 1, resolver=build_resolver)
    parent = release(parent, build_resolver)
    sub_node = _subprocess_id(parent)

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(sub, parent), store)

    inst = instantiate(parent, context=context)
    inst = complete_activity(inst, parent, pre_act, context=context)

    # the child has no interactive step, so it completes during the spawn and
    # is joined in place: the parent runs straight through to completion.
    assert inst.node_states[sub_node] is NodeState.COMPLETED
    assert inst.state is InstanceState.COMPLETED


def test_subprocess_is_black_box_without_context() -> None:
    sub = create_empty_schema("SubBlack", schema_id="sub_black")
    sub = serial_insert(sub, "X", after_node_id="start")
    sub = release(sub)

    parent = create_empty_schema("HauptBlack", schema_id="parent_black")
    parent = serial_insert(parent, "Vor", after_node_id="start")
    pre_act = _activity_id(parent)
    build_resolver = _resolver_for(sub)
    parent = insert_subprocess(parent, pre_act, "sub_black", 1, resolver=build_resolver)
    parent = release(parent, build_resolver)
    sub_node = _subprocess_id(parent)

    # no ExecutionContext: the SUBPROCESS node completes immediately as a black
    # box, so no child instance is spawned.
    inst = instantiate(parent)
    inst = complete_activity(inst, parent, pre_act)
    assert inst.node_states[sub_node] is NodeState.COMPLETED
    assert inst.child_instances == {}
    assert inst.state is InstanceState.COMPLETED
