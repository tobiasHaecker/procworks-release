# SPDX-License-Identifier: BUSL-1.1
"""K4: cross-branch synchronisation edges + insertBetweenNodeSets (ADEPT).

A SYNC edge is ordering-only: invisible to every structural rule (degrees,
blocks, data flow, T2 -- ``incoming``/``outgoing`` stay control-only), known
only to the K4 checker and the engine's wait logic. The target additionally
waits until the source is completed **or deselected** (never a dead-wait);
K4 admits sync edges only between activities of different branches of one
AND block and refuses ordering cycles. ``insert_between_node_sets`` embeds a
new activity as a fresh AND branch wired with sync edges. Round-trips through
BPMN via the ProcWorks extension (sync edges are no sequence flows).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from procworks import (
    AccessMode,
    BranchSpec,
    DataType,
    NodeState,
    add_data_element,
    add_sync_edge,
    complete_activity,
    conditional_insert,
    connect_data,
    create_empty_schema,
    delete_node,
    export_bpmn,
    import_bpmn,
    insert_between_node_sets,
    instantiate,
    parallel_insert,
    release,
    remove_sync_edge,
    serial_insert,
    validate,
)
from procworks import api as api_module
from procworks.model import EdgeType
from procworks.operations import CorrectnessError

client = TestClient(api_module.app)


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _parallel_schema():
    """start -> AND[ Links -> Links2 | Rechts ] -> end."""

    schema = create_empty_schema("Parallel")
    schema = parallel_insert(schema, ["Links", "Rechts"], after_node_id="start")
    return serial_insert(schema, "Links2", after_node_id=_nid(schema, "Links"))


# --- modelling: K4 ----------------------------------------------------------


def test_sync_edge_between_and_branches_is_valid_and_removable() -> None:
    schema = _parallel_schema()
    schema = add_sync_edge(schema, _nid(schema, "Links"), _nid(schema, "Rechts"))
    assert validate(schema) == []
    assert sum(1 for e in schema.edges if e.type is EdgeType.SYNC) == 1

    # Structure stays untouched: control degrees ignore the sync edge (K2).
    assert len(schema.outgoing(_nid(schema, "Links"))) == 1
    assert len(schema.incoming(_nid(schema, "Rechts"))) == 1

    cleared = remove_sync_edge(schema, _nid(schema, "Links"), _nid(schema, "Rechts"))
    assert not any(e.type is EdgeType.SYNC for e in cleared.edges)
    with pytest.raises(CorrectnessError):  # removing again: no such edge
        remove_sync_edge(cleared, _nid(schema, "Links"), _nid(schema, "Rechts"))


def test_k4_rejects_same_branch_serial_context_and_cycles() -> None:
    schema = _parallel_schema()

    with pytest.raises(CorrectnessError) as err:  # same branch
        add_sync_edge(schema, _nid(schema, "Links"), _nid(schema, "Links2"))
    assert any(f.rule == "K4" for f in err.value.findings)

    serial = create_empty_schema("Seriell")
    serial = serial_insert(serial, "A", after_node_id="start")
    serial = serial_insert(serial, "B", after_node_id=_nid(serial, "A"))
    with pytest.raises(CorrectnessError):  # no AND block at all
        add_sync_edge(serial, _nid(serial, "A"), _nid(serial, "B"))

    with pytest.raises(CorrectnessError):  # gateway endpoint
        add_sync_edge(schema, "start", _nid(schema, "Rechts"))

    # Opposing syncs would wait on each other forever -> cycle -> K4.
    once = add_sync_edge(schema, _nid(schema, "Links"), _nid(schema, "Rechts"))
    with pytest.raises(CorrectnessError) as err:
        add_sync_edge(once, _nid(schema, "Rechts"), _nid(schema, "Links"))
    assert any("cycle" in f.message for f in err.value.findings)


# --- engine: wait semantics -------------------------------------------------


def test_sync_target_waits_for_completion() -> None:
    from staffing import staffed

    schema = _parallel_schema()
    schema = add_sync_edge(schema, _nid(schema, "Links"), _nid(schema, "Rechts"))
    schema = release(staffed(schema))
    links, links2, rechts = (
        _nid(schema, "Links"), _nid(schema, "Links2"), _nid(schema, "Rechts")
    )

    inst = instantiate(schema)
    # Rechts waits on the sync edge although its control edge is signalled.
    assert inst.node_states[links] is NodeState.ACTIVATED
    assert inst.node_states[rechts] is NodeState.NOT_ACTIVATED

    inst = complete_activity(inst, schema, links)
    assert inst.node_states[rechts] is NodeState.ACTIVATED  # wait resolved
    inst = complete_activity(inst, schema, rechts)
    inst = complete_activity(inst, schema, links2)
    assert inst.state.value == "COMPLETED"


def test_sync_from_a_deselected_source_never_dead_waits() -> None:
    """A skipped source resolves the wait too (completed *or* deselected)."""

    from staffing import staffed

    schema = create_empty_schema("AbwahlSync")
    schema = parallel_insert(schema, ["Zweig1", "Rechts"], after_node_id="start")
    z1 = _nid(schema, "Zweig1")
    schema = add_data_element(schema, "eilig", DataType.BOOLEAN, element_id="eilig")
    schema = connect_data(schema, z1, "eilig", AccessMode.WRITE)
    schema = conditional_insert(
        schema,
        after_node_id=z1,
        discriminator="eilig",
        branches=[
            BranchSpec(label="Schnell", bool_value=True),
            BranchSpec(label="Gruendlich", bool_value=False),
        ],
    )
    schema = add_sync_edge(schema, _nid(schema, "Schnell"), _nid(schema, "Rechts"))
    assert validate(schema) == []
    schema = release(staffed(schema))

    inst = instantiate(schema)
    # Choosing the OTHER branch skips the sync source -> Rechts must not hang.
    inst = complete_activity(inst, schema, z1, {"eilig": False})
    assert inst.node_states[_nid(schema, "Schnell")] is NodeState.SKIPPED
    assert inst.node_states[_nid(schema, "Rechts")] is NodeState.ACTIVATED
    inst = complete_activity(inst, schema, _nid(schema, "Gruendlich"))
    inst = complete_activity(inst, schema, _nid(schema, "Rechts"))
    assert inst.state.value == "COMPLETED"


# --- insertBetweenNodeSets --------------------------------------------------


def test_insert_between_node_sets_orders_across_branches() -> None:
    from staffing import staffed

    schema = _parallel_schema()
    schema = insert_between_node_sets(
        schema, "Zwischen", [_nid(schema, "Links")], [_nid(schema, "Rechts")]
    )
    assert validate(schema) == []
    zw = _nid(schema, "Zwischen")
    syncs = {(e.source, e.target) for e in schema.edges if e.type is EdgeType.SYNC}
    assert (_nid(schema, "Links"), zw) in syncs
    assert (zw, _nid(schema, "Rechts")) in syncs

    schema = release(staffed(schema))
    inst = instantiate(schema)
    links, rechts = _nid(schema, "Links"), _nid(schema, "Rechts")
    assert inst.node_states[zw] is NodeState.NOT_ACTIVATED  # waits for Links
    assert inst.node_states[rechts] is NodeState.NOT_ACTIVATED  # waits for Zw.
    inst = complete_activity(inst, schema, links)
    assert inst.node_states[zw] is NodeState.ACTIVATED
    inst = complete_activity(inst, schema, zw)
    assert inst.node_states[rechts] is NodeState.ACTIVATED
    inst = complete_activity(inst, schema, rechts)
    inst = complete_activity(inst, schema, _nid(schema, "Links2"))
    assert inst.state.value == "COMPLETED"


def test_insert_between_requires_a_common_and_block() -> None:
    serial = create_empty_schema("Seriell")
    serial = serial_insert(serial, "A", after_node_id="start")
    serial = serial_insert(serial, "B", after_node_id=_nid(serial, "A"))
    with pytest.raises(CorrectnessError) as err:
        insert_between_node_sets(serial, "X", [_nid(serial, "A")], [_nid(serial, "B")])
    assert any("AND block" in f.message for f in err.value.findings)


def test_deleting_a_sync_endpoint_takes_its_edges_along() -> None:
    schema = _parallel_schema()
    schema = insert_between_node_sets(
        schema, "Zwischen", [_nid(schema, "Links")], [_nid(schema, "Rechts")]
    )
    gone = delete_node(schema, _nid(schema, "Zwischen"))
    assert not any(e.type is EdgeType.SYNC for e in gone.edges)
    assert validate(gone) == []


# --- boundary: BPMN + API ---------------------------------------------------


def test_sync_edges_round_trip_through_bpmn() -> None:
    schema = _parallel_schema()
    schema = add_sync_edge(schema, _nid(schema, "Links"), _nid(schema, "Rechts"))

    xml = export_bpmn(schema)
    # A sync edge is no sequence flow: the BPMN flow graph stays the pure
    # control flow; the edge travels in the ProcWorks extension.
    assert xml.count("<bpmn:sequenceFlow") == len(
        [e for e in schema.edges if e.type is EdgeType.CONTROL]
    )
    imported = import_bpmn(xml)
    syncs = [e for e in imported.edges if e.type is EdgeType.SYNC]
    assert [(e.source, e.target) for e in syncs] == [
        (_nid(schema, "Links"), _nid(schema, "Rechts"))
    ]
    assert validate(imported) == []


def test_sync_edge_and_insert_between_via_api() -> None:
    sid = client.post("/schemas", json={"name": "Sync-API"}).json()["id"]
    client.post(
        f"/schemas/{sid}/parallel-insert",
        json={"branch_labels": ["Links", "Rechts"], "after_node_id": "start"},
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    links = next(nid for nid, n in nodes.items() if n.get("label") == "Links")
    rechts = next(nid for nid, n in nodes.items() if n.get("label") == "Rechts")

    resp = client.post(
        f"/schemas/{sid}/sync-edge", json={"source_id": links, "target_id": rechts}
    )
    assert resp.status_code == 200
    assert any(e["type"] == "SYNC" for e in resp.json()["edges"])

    resp = client.post(  # cycle -> 422, schema unchanged
        f"/schemas/{sid}/sync-edge", json={"source_id": rechts, "target_id": links}
    )
    assert resp.status_code == 422

    resp = client.post(
        f"/schemas/{sid}/sync-edge/remove",
        json={"source_id": links, "target_id": rechts},
    )
    assert resp.status_code == 200
    assert not any(e["type"] == "SYNC" for e in resp.json()["edges"])

    resp = client.post(
        f"/schemas/{sid}/insert-between",
        json={"label": "Zwischen", "source_ids": [links], "target_ids": [rechts]},
    )
    assert resp.status_code == 200
    assert any(n["label"] == "Zwischen" for n in resp.json()["nodes"].values())
