# SPDX-License-Identifier: BUSL-1.1
"""Every XOR decision a case passes leaves one ``BRANCH_DECIDED`` in its history.

The event type was defined and translated in the client, but nothing wrote
it: the history showed which step came next, never *why* that branch was
taken. The engine now hands each decision it takes over to the boundary
(``ProcessInstance.unaudited_decisions``); the boundary writes one
``BRANCH_DECIDED`` per decision -- with the chosen branch, the element, its
value and the branch condition -- after the event that triggered the advance,
and empties the hand-over before the instance is stored. Test instances write
nothing; KPIs and the process map ignore the event type.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from staffing import staff_via_api
from web_vm import needs_node, run_app_js

from procworks import api as api_module
from procworks.audit import EventType, compute_kpis, discover_process_map

client = TestClient(api_module.app)


def _schema(name: str) -> tuple[str, dict[str, str]]:
    """Draft: Erfassen (writes betrag) → XOR [betrag < 1000: Klein | sonst: Groß] → end.

    Returns the schema id and a label → node id map.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Betrag", "data_type": "INTEGER", "element_id": "betrag"},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": act, "element_id": "betrag", "mode": "WRITE", "mandatory": True},
    )
    split = client.post(
        f"/schemas/{sid}/conditional-insert",
        json={
            "after_node_id": act,
            "discriminator": "betrag",
            "branches": [{"label": "Klein", "upper": 1000}, {"label": "Groß"}],
        },
    )
    assert split.status_code == 200, split.text
    staff_via_api(client, sid)
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    ids = {n.get("label") or n["type"]: nid for nid, n in nodes.items()}
    return sid, ids


def _branch_events(iid: str) -> list[dict[str, Any]]:
    return [
        e for e in client.get(f"/instances/{iid}/audit").json()
        if e["event_type"] == "BRANCH_DECIDED"
    ]


def test_one_event_per_decision_with_branch_value_and_condition() -> None:
    sid, ids = _schema("Verzweigung Verlauf")
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    done = client.post(
        f"/instances/{iid}/complete", json={"node_id": ids["Erfassen"], "data": {"betrag": 1000}}
    )
    assert done.status_code == 200, done.text

    [event] = _branch_events(iid)
    assert event["node_id"] == ids["XOR_SPLIT"]
    assert event["detail"]["branch"] == "Groß"
    assert event["detail"]["element"] == "Betrag"
    assert json.loads(event["detail"]["value"]) == 1000
    assert event["detail"]["condition"] == "Betrag >= 1000"  # the lower bound is inclusive
    # Written after the completion that triggered the advance, never before.
    kinds = [e["event_type"] for e in client.get(f"/instances/{iid}/audit").json()]
    assert kinds.index("ACTIVITY_COMPLETED") < kinds.index("BRANCH_DECIDED")
    # The hand-over is emptied before the instance is stored and answered.
    assert done.json()["unaudited_decisions"] == []
    stored = api_module._instances.get(iid)
    assert stored is not None and stored.unaudited_decisions == []


def test_the_other_branch_is_named_too() -> None:
    sid, ids = _schema("Verzweigung klein")
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    client.post(
        f"/instances/{iid}/complete", json={"node_id": ids["Erfassen"], "data": {"betrag": 999}}
    )
    [event] = _branch_events(iid)
    assert event["detail"]["branch"] == "Klein"
    assert event["detail"]["condition"] == "Betrag < 1000"


def test_a_test_instance_writes_no_decision() -> None:
    sid, ids = _schema("Verzweigung Prüfinstanz")  # stays a draft: test instance
    created = client.post(f"/schemas/{sid}/instances")
    assert created.status_code == 201, created.text
    assert created.json()["is_test"] is True
    iid = created.json()["id"]
    done = client.post(
        f"/instances/{iid}/complete", json={"node_id": ids["Erfassen"], "data": {"betrag": 5}}
    )
    assert done.status_code == 200, done.text
    assert _branch_events(iid) == []
    assert done.json()["unaudited_decisions"] == []


def test_kpis_and_process_map_ignore_the_decision_events() -> None:
    sid, ids = _schema("Verzweigung Kennzahlen")
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    client.post(
        f"/instances/{iid}/complete", json={"node_id": ids["Erfassen"], "data": {"betrag": 7}}
    )
    events = api_module._audit.for_instance(iid)
    without = [e for e in events if e.event_type is not EventType.BRANCH_DECIDED]
    assert len(without) == len(events) - 1
    assert compute_kpis(events, sid) == compute_kpis(without, sid)
    assert discover_process_map(events) == discover_process_map(without)


def test_a_parent_deciding_after_its_sub_process_records_the_decision() -> None:
    """The engine advances the parent when the child ends -- no request held
    the parent, so the boundary picks the decision up from the stored parent."""

    child = client.post("/schemas", json={"name": "Verzweigung Kind"}).json()["id"]
    created = client.post(
        f"/schemas/{child}/serial-insert", json={"label": "Rechnen", "after_node_id": "start"}
    ).json()
    calc = next(n["id"] for n in created["nodes"].values() if n["type"] == "ACTIVITY")
    client.post(
        f"/schemas/{child}/data-elements",
        json={"name": "Ergebnis", "data_type": "INTEGER", "element_id": "ergebnis"},
    )
    client.post(
        f"/schemas/{child}/data-access",
        json={"node_id": calc, "element_id": "ergebnis", "mode": "WRITE"},
    )
    staff_via_api(client, child)
    assert client.post(f"/schemas/{child}/release").status_code == 200

    parent = client.post("/schemas", json={"name": "Verzweigung Eltern"}).json()["id"]
    created = client.post(
        f"/schemas/{parent}/serial-insert", json={"label": "Teil", "after_node_id": "start"}
    ).json()
    step = next(n["id"] for n in created["nodes"].values() if n["type"] == "ACTIVITY")
    client.post(
        f"/schemas/{parent}/data-elements",
        json={"name": "Summe", "data_type": "INTEGER", "element_id": "summe"},
    )
    converted = client.post(
        f"/schemas/{parent}/convert-to-subprocess",
        json={
            "node_id": step,
            "target_schema_id": child,
            "target_version": 1,
            "output_mapping": {"ergebnis": "summe"},
        },
    )
    assert converted.status_code == 200, converted.text
    split = client.post(
        f"/schemas/{parent}/conditional-insert",
        json={
            "after_node_id": step,
            "discriminator": "summe",
            "branches": [{"label": "Wenig", "upper": 10}, {"label": "Viel"}],
        },
    )
    assert split.status_code == 200, split.text
    staff_via_api(client, parent)
    assert client.post(f"/schemas/{parent}/release").status_code == 200
    pid = client.post(f"/schemas/{parent}/instances").json()["id"]
    cid = client.get(f"/instances/{pid}").json()["child_instances"][step]

    done = client.post(
        f"/instances/{cid}/complete", json={"node_id": calc, "data": {"ergebnis": 42}}
    )
    assert done.status_code == 200, done.text

    [event] = _branch_events(pid)
    assert (event["detail"]["branch"], json.loads(event["detail"]["value"])) == ("Viel", 42)
    kinds = [e["event_type"] for e in client.get(f"/instances/{pid}/audit").json()]
    assert kinds.index("ACTIVITY_COMPLETED") < kinds.index("BRANCH_DECIDED")
    stored = api_module._instances.get(pid)
    assert stored is not None and stored.unaudited_decisions == []


@needs_node
def test_the_history_shows_branch_condition_and_value(tmp_path: Path) -> None:
    texts = run_app_js(
        "return ["
        "auditDetailText({event_type: 'BRANCH_DECIDED', label: 'Entscheidung', detail: "
        "{branch: 'Groß', element: 'Betrag', value: '1000', condition: 'Betrag >= 1000'}}),"
        "auditDetailText({event_type: 'BRANCH_DECIDED', label: 'Entscheidung', detail: "
        "{branch: 'leerer Zweig', value: 'true'}}),"
        "];",
        tmp_path,
    )
    assert texts == [
        "Entscheidung: Zweig „Groß“ (Betrag >= 1000, Wert 1000)",
        "Entscheidung: Zweig „leerer Zweig“ (Wert Ja)",
    ]
