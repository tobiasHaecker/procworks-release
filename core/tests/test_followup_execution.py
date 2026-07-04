# SPDX-License-Identifier: BUSL-1.1
"""Tests for follow-up execution (lateral composition at runtime, F1-F4).

When an instance completes, each follow-up link whose trigger fires starts a new
instance of the target. ON_COMPLETE links always fire; CONDITIONAL links fire
only when their predicate holds against the instance's data. ASYNC coupling
starts a fully decoupled top-level instance (no parent linkage, F3); SYNC
coupling starts a coupled instance that records its originating instance id.
"""

from __future__ import annotations

from procworks import (
    ExecutionContext,
    add_data_element,
    complete_activity,
    create_empty_schema,
    instantiate,
    link_follow_up,
    release,
    serial_insert,
    worklist,
)
from procworks.model import (
    DataType,
    FollowUpMode,
    FollowUpTrigger,
    InstanceState,
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


def test_follow_up_starts_decoupled_instance_with_handover() -> None:
    target = create_empty_schema("Folge", schema_id="follow_target")
    target = serial_insert(target, "Nacharbeit", after_node_id="start")
    target = add_data_element(target, "vorgang", DataType.STRING, element_id="vorgang")
    target = release(target)
    target_act = _activity_id(target)

    source = create_empty_schema("Quelle", schema_id="source")
    source = serial_insert(source, "Bearbeiten", after_node_id="start")
    source = add_data_element(source, "referenz", DataType.STRING, element_id="referenz")
    src_act = _activity_id(source)
    build_resolver = _resolver_for(target)
    source = link_follow_up(
        source,
        "follow_target",
        handover_mapping={"vorgang": "referenz"},
        resolver=build_resolver,
    )
    source = release(source, build_resolver)

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(source, target), store)

    inst = instantiate(source, context=context)
    assert inst.follow_up_instances == []  # not completed yet

    inst = complete_activity(inst, source, src_act, {"referenz": "ABC-1"}, context=context)
    assert inst.state is InstanceState.COMPLETED
    assert len(inst.follow_up_instances) == 1

    follow_id = inst.follow_up_instances[0]
    follow = store.get(follow_id)
    assert follow is not None
    # decoupled: the follow-up has no parent linkage (F3)
    assert follow.parent_instance_id is None
    assert follow.state is InstanceState.RUNNING
    assert follow.data_values["vorgang"] == "ABC-1"
    assert worklist(follow, target) == [target_act]


def test_sync_follow_up_starts_coupled_instance() -> None:
    target = create_empty_schema("FolgeSync", schema_id="sync_target")
    target = serial_insert(target, "T", after_node_id="start")
    target = release(target)

    source = create_empty_schema("QuelleSync", schema_id="source_sync")
    source = serial_insert(source, "S", after_node_id="start")
    src_act = _activity_id(source)
    build_resolver = _resolver_for(target)
    source = link_follow_up(
        source, "sync_target", mode=FollowUpMode.SYNC, resolver=build_resolver
    )
    source = release(source, build_resolver)

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(source, target), store)

    inst = instantiate(source, context=context)
    inst = complete_activity(inst, source, src_act, context=context)
    assert inst.state is InstanceState.COMPLETED
    assert len(inst.follow_up_instances) == 1

    follow = store.get(inst.follow_up_instances[0])
    assert follow is not None
    # SYNC coupling records the originating instance id for lineage.
    assert follow.parent_instance_id == inst.id
    assert follow.state is InstanceState.RUNNING


def test_conditional_follow_up_starts_when_predicate_holds() -> None:
    target = create_empty_schema("FolgeCond", schema_id="cond_target")
    target = serial_insert(target, "T", after_node_id="start")
    target = release(target)

    source = create_empty_schema("QuelleCond", schema_id="source_cond")
    source = serial_insert(source, "S", after_node_id="start")
    source = add_data_element(source, "betrag", DataType.INTEGER, element_id="betrag")
    src_act = _activity_id(source)
    build_resolver = _resolver_for(target)
    source = link_follow_up(
        source,
        "cond_target",
        trigger=FollowUpTrigger.CONDITIONAL,
        condition="betrag > 100",
        resolver=build_resolver,
    )
    source = release(source, build_resolver)

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(source, target), store)

    inst = instantiate(source, context=context)
    inst = complete_activity(inst, source, src_act, {"betrag": 250}, context=context)
    assert inst.state is InstanceState.COMPLETED
    assert len(inst.follow_up_instances) == 1
    follow = store.get(inst.follow_up_instances[0])
    assert follow is not None
    # CONDITIONAL defaults to ASYNC coupling: decoupled top-level instance.
    assert follow.parent_instance_id is None


def test_conditional_follow_up_skipped_when_predicate_fails() -> None:
    target = create_empty_schema("FolgeCond2", schema_id="cond_target2")
    target = serial_insert(target, "T", after_node_id="start")
    target = release(target)

    source = create_empty_schema("QuelleCond2", schema_id="source_cond2")
    source = serial_insert(source, "S", after_node_id="start")
    source = add_data_element(source, "betrag", DataType.INTEGER, element_id="betrag")
    src_act = _activity_id(source)
    build_resolver = _resolver_for(target)
    source = link_follow_up(
        source,
        "cond_target2",
        trigger=FollowUpTrigger.CONDITIONAL,
        condition="betrag > 100",
        resolver=build_resolver,
    )
    source = release(source, build_resolver)

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(source, target), store)

    inst = instantiate(source, context=context)
    inst = complete_activity(inst, source, src_act, {"betrag": 10}, context=context)
    assert inst.state is InstanceState.COMPLETED
    # predicate is false -> the conditional link does not fire
    assert inst.follow_up_instances == []


def test_follow_up_not_started_without_context() -> None:
    target = create_empty_schema("FolgeBlack", schema_id="black_target")
    target = serial_insert(target, "T", after_node_id="start")
    target = release(target)

    source = create_empty_schema("QuelleBlack", schema_id="source_black")
    source = serial_insert(source, "S", after_node_id="start")
    src_act = _activity_id(source)
    build_resolver = _resolver_for(target)
    source = link_follow_up(source, "black_target", resolver=build_resolver)
    source = release(source, build_resolver)

    inst = instantiate(source)  # no context
    inst = complete_activity(inst, source, src_act)
    assert inst.state is InstanceState.COMPLETED
    assert inst.follow_up_instances == []
