# SPDX-License-Identifier: BUSL-1.1
"""Tests for schema evolution + instance migration (roadmap step 11, M1-M5).

A new schema revision (``new_revision``) keeps every element id, so a running
instance can be migrated onto it iff the executed region is preserved. The
migration criteria are:

  * M1 -- target is a correct, RELEASED schema;
  * M2 -- the executed region (nodes + internal edges) is unchanged;
  * M3 -- markings map cleanly (completed nodes keep successors, running nodes
    stay executable);
  * M4 -- mandatory data for the executed region is available;
  * M5 -- ad-hoc changed instances need manual resolution (conservative block).
"""

from __future__ import annotations

import pytest

from procworks import (
    ExecutionContext,
    add_data_element,
    adhoc_insert_activity,
    check_migration,
    complete_activity,
    connect_data,
    create_empty_schema,
    instantiate,
    is_migratable,
    migrate_instance,
    new_revision,
    release,
    serial_insert,
    start_activity,
    worklist,
)
from procworks.model import (
    AccessMode,
    DataType,
    NodeState,
    NodeType,
    ProcessSchema,
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
    return release(schema)


def _instance_with_a_completed(schema: ProcessSchema):
    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(schema), store)
    instance = instantiate(schema, context=context)
    a_id = _ordered_activities(schema)[0]
    instance = complete_activity(instance, schema, a_id, context=context)
    return instance, context


def test_new_revision_preserves_element_ids_and_bumps_version() -> None:
    source = _released_serial()
    revision = new_revision(source)
    assert revision.id != source.id
    assert revision.version == source.version + 1
    # every node id is preserved so markings stay valid
    assert set(revision.nodes) == set(source.nodes)


def test_new_revision_requires_released_source() -> None:
    draft = create_empty_schema("Entwurf")
    with pytest.raises(CorrectnessError):
        new_revision(draft)


def test_happy_path_migration_remaps_markings() -> None:
    source = _released_serial()
    instance, _ = _instance_with_a_completed(source)
    a_id, b_id = _ordered_activities(source)

    # Add C after B -- strictly ahead of the execution front.
    target = new_revision(source)
    target = serial_insert(target, "C", after_node_id=b_id)
    target = release(target)

    assert is_migratable(instance, source, target)
    migrated = migrate_instance(instance, source, target)
    assert migrated.schema_id == target.id
    assert migrated.schema_version == target.version
    # executed marking preserved, new node starts unmarked
    assert migrated.node_states[a_id] is NodeState.COMPLETED
    new_id = next(n.id for n in target.nodes.values() if n.label == "C")
    assert migrated.node_states[new_id] is NodeState.NOT_ACTIVATED


def test_m1_unreleased_target_blocks_migration() -> None:
    source = _released_serial()
    instance, _ = _instance_with_a_completed(source)
    b_id = _ordered_activities(source)[1]

    target = new_revision(source)
    target = serial_insert(target, "C", after_node_id=b_id)
    # NOT released -> still ENTWURF

    findings = check_migration(instance, source, target)
    assert any(f.rule == "M1" for f in findings)
    assert not is_migratable(instance, source, target)


def test_m3_rewiring_completed_node_blocks_migration() -> None:
    source = _released_serial()
    instance, _ = _instance_with_a_completed(source)
    a_id = _ordered_activities(source)[0]

    # Insert C right after the already-completed A -> rewires its successor.
    target = new_revision(source)
    target = serial_insert(target, "C", after_node_id=a_id)
    target = release(target)

    findings = check_migration(instance, source, target)
    assert any(f.rule == "M3" for f in findings)


def test_m2_changing_executed_edge_blocks_migration() -> None:
    source = _released_serial()
    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(source), store)
    instance = instantiate(source, context=context)
    a_id, b_id = _ordered_activities(source)
    instance = complete_activity(instance, source, a_id, context=context)
    # Make B RUNNING so the A->B edge lies inside the executed region.
    instance = start_activity(instance, source, b_id)
    assert instance.node_states[b_id] is NodeState.RUNNING

    # Splice C between A and B -> the executed edge A->B disappears.
    target = new_revision(source)
    target = serial_insert(target, "C", after_node_id=a_id)
    target = release(target)

    findings = check_migration(instance, source, target)
    assert any(f.rule == "M2" for f in findings)


def test_m4_missing_mandatory_data_blocks_then_mapping_fixes() -> None:
    # start -> W -> A -> B -> end
    source = create_empty_schema("Daten", schema_id="data")
    source = serial_insert(source, "B", after_node_id="start")
    source = serial_insert(source, "A", after_node_id="start")
    source = serial_insert(source, "W", after_node_id="start")
    source = release(source)
    w_id, a_id, _b_id = _ordered_activities(source)

    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(source), store)
    instance = instantiate(source, context=context)
    instance = complete_activity(instance, source, w_id, context=context)
    instance = complete_activity(instance, source, a_id, context=context)

    # Target introduces a mandatory data dependency on the executed node A.
    target = new_revision(source)
    target = add_data_element(target, "Beleg", DataType.STRING, element_id="doc")
    target = connect_data(target, w_id, "doc", AccessMode.WRITE)
    target = connect_data(target, a_id, "doc", AccessMode.READ)
    target = release(target)

    findings = check_migration(instance, source, target)
    assert any(f.rule == "M4" for f in findings)

    # Supplying the value via data_mapping makes the instance migratable.
    mapping: dict[str, object] = {"doc": "B-001"}
    assert is_migratable(instance, source, target, data_mapping=mapping)
    migrated = migrate_instance(instance, source, target, data_mapping=mapping)
    assert migrated.data_values["doc"] == "B-001"


def test_m5_adhoc_instance_blocks_migration() -> None:
    source = _released_serial()
    store = InMemoryInstanceStore()
    context = ExecutionContext(_resolver_for(source), store)
    instance = instantiate(source, context=context)
    a_id, b_id = _ordered_activities(source)

    # Give the instance an ad-hoc delta.
    instance = adhoc_insert_activity(instance, source, a_id, "Sonderschritt")
    assert instance.ad_hoc_deltas

    target = new_revision(source)
    target = serial_insert(target, "C", after_node_id=b_id)
    target = release(target)

    findings = check_migration(instance, source, target)
    assert any(f.rule == "M5" for f in findings)
    with pytest.raises(CorrectnessError):
        migrate_instance(instance, source, target)


def test_worklist_drives_target_after_migration() -> None:
    source = _released_serial()
    instance, _ = _instance_with_a_completed(source)
    b_id = _ordered_activities(source)[1]

    target = new_revision(source)
    target = serial_insert(target, "C", after_node_id=b_id)
    target = release(target)

    migrated = migrate_instance(instance, source, target)
    # B is still the ready activity on the target schema.
    assert b_id in worklist(migrated, target)
