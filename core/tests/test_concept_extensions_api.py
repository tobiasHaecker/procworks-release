# SPDX-License-Identifier: BUSL-1.1
"""End-to-end API tests for the concept extensions E3/E4/E5/E7/E8."""

from __future__ import annotations

from fastapi.testclient import TestClient

from procworks.api import app
from procworks.audit import EventType, InMemoryAuditLog, compute_kpis

client = TestClient(app)


def _new_schema_with_activity(name: str) -> tuple[str, str]:
    sid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "A", "after_node_id": "start"},
    )
    schema = client.get(f"/schemas/{sid}").json()
    act_id = next(
        nid for nid, n in schema["nodes"].items() if n["type"] == "ACTIVITY"
    )
    return sid, act_id


def test_metrics_endpoint() -> None:
    sid, _ = _new_schema_with_activity("Metrik")
    resp = client.get(f"/schemas/{sid}/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metrics"]["activity_count"] == 1
    assert body["hints"] == []
    assert body["value_classes"]["unclassified"] == 1


def test_value_class_endpoint() -> None:
    sid, act_id = _new_schema_with_activity("Wert")
    resp = client.post(
        f"/schemas/{sid}/value-class",
        json={"node_id": act_id, "value_class": "VALUE_ADDING"},
    )
    assert resp.status_code == 200
    assert resp.json()["nodes"][act_id]["value_class"] == "VALUE_ADDING"
    breakdown = client.get(f"/schemas/{sid}/metrics").json()["value_classes"]
    assert breakdown["value_adding"] == 1


def test_priority_endpoint() -> None:
    sid, act_id = _new_schema_with_activity("Prio")
    resp = client.post(
        f"/schemas/{sid}/priority",
        json={
            "node_id": act_id,
            "priority": {"impact": "HIGH", "urgency": "HIGH"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["node_priorities"][act_id]["impact"] == "HIGH"


def test_time_constraint_and_deadline_endpoints() -> None:
    sid, act_id = _new_schema_with_activity("Zeit")
    resp = client.post(
        f"/schemas/{sid}/time-constraint",
        json={"node_id": act_id, "constraint": {"max_duration_seconds": 30}},
    )
    assert resp.status_code == 200
    resp = client.post(f"/schemas/{sid}/deadline", json={"deadline_seconds": 100})
    assert resp.status_code == 200
    assert resp.json()["deadline_seconds"] == 100


def test_deadline_violation_returns_422() -> None:
    sid, act_id = _new_schema_with_activity("ZeitFehler")
    client.post(
        f"/schemas/{sid}/time-constraint",
        json={"node_id": act_id, "constraint": {"max_duration_seconds": 200}},
    )
    resp = client.post(f"/schemas/{sid}/deadline", json={"deadline_seconds": 100})
    assert resp.status_code == 422
    assert any(f["rule"] == "T2" for f in resp.json()["detail"]["findings"])


def test_worklist_stamps_activation_and_reports_time_fields() -> None:
    """The runtime clock of the time-based prioritisation is stamped at the API
    boundary and the worklist reports the derived time fields (concept Z1/Z2)."""

    sid, act_id = _new_schema_with_activity("ZeitPrio")
    # a modelled reaction target time and an eligible performer
    client.post(
        f"/schemas/{sid}/time-constraint",
        json={"node_id": act_id, "constraint": {"target_lead_seconds": 3600}},
    )
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"},
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": act_id, "rule": {"kind": "ROLE", "ref": "sb"}},
    )
    client.post(f"/schemas/{sid}/release")
    iid = client.post(f"/schemas/{sid}/instances", json={}).json()["id"]

    # the instance carries the stamped activation clock ...
    instance = client.get(f"/instances/{iid}").json()
    assert act_id in instance["node_activated_at"]
    assert instance["started_at"] is not None

    # ... and the worklist derives the (freshly activated -> ON_TRACK) band.
    tasks = client.get(f"/instances/{iid}/tasks").json()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["node_id"] == act_id
    assert task["target_seconds"] == 3600
    assert task["time_criticality"] == "ON_TRACK"
    assert task["remaining_seconds"] > 0
    assert task["due_at"] is not None


def test_negative_target_lead_seconds_rejected_by_api() -> None:
    sid, act_id = _new_schema_with_activity("ZeitLeadFehler")
    resp = client.post(
        f"/schemas/{sid}/time-constraint",
        json={"node_id": act_id, "constraint": {"target_lead_seconds": -1}},
    )
    assert resp.status_code == 422
    assert any(f["rule"] == "T1" for f in resp.json()["detail"]["findings"])


def test_kpi_flexibility_ratio() -> None:
    log = InMemoryAuditLog()
    # Instance i1 used an ad-hoc change; i2 did not.
    log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    log.append(EventType.ADHOC_INSERTED, "i1", "s1", node_id="a")
    log.append(EventType.INSTANCE_COMPLETED, "i1", "s1")
    log.append(EventType.INSTANCE_CREATED, "i2", "s1")
    log.append(EventType.INSTANCE_COMPLETED, "i2", "s1")
    report = compute_kpis(log.list_all(), "s1")
    assert report.total_instances == 2
    assert report.adhoc_instances == 1
    assert report.flexibility_adhoc_ratio == 0.5
