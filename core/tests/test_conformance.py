# SPDX-License-Identifier: BUSL-1.1
"""Target/actual comparison (``audit.conformance``, ``GET /schemas/{id}/conformance``).

The allowed-transition relation must be an **over**-approximation: a deviation
is reported only when the model certainly cannot produce it. The tests pin both
directions -- real deviations are found, and every legitimate ordering the
engine allows (sequence, parallel interleaving, loop repetition, a skipped XOR
branch) is *not* reported.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from staffing import staff_via_api

from procworks import (
    add_data_element,
    create_empty_schema,
    insert_loop,
    parallel_insert,
    serial_insert,
)
from procworks.api import app
from procworks.audit import AuditEvent, EventType, conformance, model_directly_follows
from procworks.model import DataType, NodeType, ProcessSchema

client = TestClient(app)


def _ids(schema: ProcessSchema) -> dict[str, str]:
    return {n.label: n.id for n in schema.nodes.values() if n.type is NodeType.ACTIVITY}


def _trace(schema_id: str, instance: str, steps: list[str]) -> list[AuditEvent]:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    events = [AuditEvent(seq=0, timestamp=base, event_type=EventType.INSTANCE_CREATED,
                         instance_id=instance, schema_id=schema_id)]
    for i, node_id in enumerate(steps, start=1):
        events.append(AuditEvent(seq=i, timestamp=base + timedelta(seconds=i),
                                 event_type=EventType.ACTIVITY_COMPLETED,
                                 instance_id=instance, schema_id=schema_id, node_id=node_id))
    return events


def _serial() -> ProcessSchema:
    s = create_empty_schema("Seriell", schema_id="konf-seriell")
    s = serial_insert(s, "B", after_node_id="start")
    return serial_insert(s, "A", after_node_id="start")


def test_sequence_matches_and_reversal_is_a_deviation() -> None:
    s = _serial()
    ids = _ids(s)
    events = _trace(s.id, "i1", [ids["A"], ids["B"]]) + _trace(s.id, "i2", [ids["B"], ids["A"]])
    report = conformance(s, events)
    assert report.instances == 2
    assert [(d.source_label, d.target_label, d.frequency) for d in report.deviations] == [
        ("B", "A", 1)
    ]
    assert {st.label: st.completed for st in report.steps} == {"A": 2, "B": 2}


def test_parallel_branches_may_interleave_in_any_order() -> None:
    s = create_empty_schema("Parallel", schema_id="konf-par")
    s = parallel_insert(s, ["X", "Y"], after_node_id="start")
    ids = _ids(s)
    events = _trace(s.id, "i1", [ids["X"], ids["Y"]]) + _trace(s.id, "i2", [ids["Y"], ids["X"]])
    assert conformance(s, events).deviations == []


def test_loop_repetition_is_allowed() -> None:
    s = create_empty_schema("Schleife", schema_id="konf-loop")
    s = serial_insert(s, "Vorher", after_node_id="start")
    s = add_data_element(s, "nochmal", DataType.BOOLEAN, element_id="nochmal")
    vorher = _ids(s)["Vorher"]
    s = insert_loop(s, vorher, "Nacharbeit", discriminator="nochmal", repeat_value=True)
    ids = _ids(s)
    trace = [ids["Vorher"], ids["Nacharbeit"], ids["Nacharbeit"], ids["Nacharbeit"]]
    assert (ids["Nacharbeit"], ids["Nacharbeit"]) in model_directly_follows(s)
    assert conformance(s, _trace(s.id, "i1", trace)).deviations == []


def test_never_executed_and_foreign_steps_are_reported() -> None:
    s = _serial()
    ids = _ids(s)
    report = conformance(s, _trace(s.id, "i1", [ids["A"], "adhoc_42"]))
    assert {st.label: st.completed for st in report.steps} == {"A": 1, "B": 0}
    assert report.foreign_steps == ["adhoc_42"]
    assert [d.target for d in report.deviations] == ["adhoc_42"]


def test_api_reports_history_of_a_real_run() -> None:
    sid = client.post("/schemas", json={"name": "Konformitaet"}).json()["id"]
    for label in ("B", "A"):
        client.post(
            f"/schemas/{sid}/serial-insert", json={"label": label, "after_node_id": "start"}
        )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    schema = client.get(f"/schemas/{sid}").json()
    ids = {n["label"]: n["id"] for n in schema["nodes"].values() if n["type"] == "ACTIVITY"}
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    for label in ("A", "B"):
        client.post(f"/instances/{iid}/complete", json={"node_id": ids[label]})

    report = client.get(f"/schemas/{sid}/conformance").json()
    assert report["instances"] == 1 and report["deviations"] == []
    steps = {st["label"]: st for st in report["steps"]}
    assert steps["A"]["completed"] == 1 and steps["A"]["avg_total_seconds"] is not None
