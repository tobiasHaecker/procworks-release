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


# --- Teilprozesse im Soll/Ist (Nachtest 2026-09-22, Mangel 6) --------------


def _parent_with_subprocess(name: str, *, subprocess_last: bool) -> tuple[str, str, str]:
    """Eltern-Schema mit Teilprozess; gibt (Eltern-Id, Kind-Id, Teilprozess-Knoten)."""

    kid = client.post("/schemas", json={"name": f"{name}-Kind"}).json()["id"]
    client.post(
        f"/schemas/{kid}/serial-insert",
        json={"label": "Kindschritt", "after_node_id": "start"},
    )
    staff_via_api(client, kid)
    assert client.post(f"/schemas/{kid}/release").status_code == 200

    pid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{pid}/serial-insert", json={"label": "Vorher", "after_node_id": "start"})
    vorher = _node_id(pid, "Vorher")
    assert client.post(f"/schemas/{pid}/subprocess", json={
        "label": "Teilprozess", "after_node_id": vorher,
        "target_schema_id": kid, "target_version": 1}).status_code == 200
    sub = _node_id(pid, "Teilprozess")
    if not subprocess_last:
        client.post(
            f"/schemas/{pid}/serial-insert",
            json={"label": "Nachher", "after_node_id": sub},
        )
    staff_via_api(client, pid)
    assert client.post(f"/schemas/{pid}/release").status_code == 200
    return pid, kid, sub


def _node_id(schema_id: str, label: str) -> str:
    nodes = client.get(f"/schemas/{schema_id}").json()["nodes"]
    return next(n["id"] for n in nodes.values() if n["label"] == label)


def test_subprocess_step_counts_as_executed_and_is_no_deviation() -> None:
    """Der Teilprozess galt als „nie ausgefuehrt", der Uebergang darueber als
    Abweichung.

    Ein SUBPROCESS-Knoten wird **in der Engine** abgeschlossen, in einer anderen
    Instanz als der, die der Aufruf vorangetrieben hat -- die Boundary sah das
    nie. Die Historie des Elternvorgangs sprang deshalb vom Schritt davor zu dem
    danach: Der Teilprozess zaehlte 0x, und die Soll/Ist-Karte meldete den
    Sprung als Abweichung vom Modell (im Order-to-Cash-Datensatz waren BEIDE
    gemeldeten Abweichungen genau dieser Fehlalarm).
    """

    pid, kid, sub = _parent_with_subprocess("Teilprozess mittig", subprocess_last=False)
    iid = client.post(f"/schemas/{pid}/instances").json()["id"]
    client.post(f"/instances/{iid}/complete", json={"node_id": _node_id(pid, "Vorher")})
    child = list(client.get(f"/instances/{iid}").json()["child_instances"].values())[0]
    client.post(f"/instances/{child}/complete", json={"node_id": _node_id(kid, "Kindschritt")})
    client.post(f"/instances/{iid}/complete", json={"node_id": _node_id(pid, "Nachher")})

    report = client.get(f"/schemas/{pid}/conformance").json()
    steps = {st["label"]: st["completed"] for st in report["steps"]}

    assert steps == {"Vorher": 1, "Teilprozess": 1, "Nachher": 1}
    assert report["deviations"] == []
    # Das Ereignis haengt am ELTERN-Vorgang, mit dem Knoten des Teilprozesses.
    events = client.get(f"/instances/{iid}/audit").json()
    joined = [e for e in events if e["node_id"] == sub]
    assert len(joined) == 1
    assert joined[0]["event_type"] == EventType.ACTIVITY_COMPLETED
    assert joined[0]["detail"]["child_instance"] == child


def test_parent_finished_by_its_subprocess_reports_its_completion() -> None:
    """Endet der Vorgang MIT dem Teilprozess, fehlte sein Abschluss ganz.

    Die Instanz war COMPLETED, ihr Verlauf sagte es aber nicht -- das kostete
    die KPIs lautlos einen abgeschlossenen Vorgang. Genau einmal, nicht doppelt.
    """

    pid, kid, _sub = _parent_with_subprocess("Teilprozess am Ende", subprocess_last=True)
    iid = client.post(f"/schemas/{pid}/instances").json()["id"]
    client.post(f"/instances/{iid}/complete", json={"node_id": _node_id(pid, "Vorher")})
    child = list(client.get(f"/instances/{iid}").json()["child_instances"].values())[0]
    client.post(f"/instances/{child}/complete", json={"node_id": _node_id(kid, "Kindschritt")})

    assert client.get(f"/instances/{iid}").json()["state"] == "COMPLETED"
    types = [e["event_type"] for e in client.get(f"/instances/{iid}/audit").json()]
    assert types.count(EventType.INSTANCE_COMPLETED) == 1
    assert types.count(EventType.ACTIVITY_COMPLETED) == 2  # Vorher + Teilprozess
