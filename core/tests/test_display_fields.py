# SPDX-License-Identifier: BUSL-1.1
"""Instances and tasks are named by their data.

Instances were called ``instance_14``; a worklist showed only step and process,
so two equal tasks of different instances looked the same. A schema names up
to two INSTANCE data elements (``display_fields``); their values travel with
each open task (``context``) and as instance titles (``GET /instance-titles``).
Presentation only -- U5 just keeps the list meaningful.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

from procworks import create_empty_schema, delete_data_element
from procworks.api import app
from procworks.model import DataType
from procworks.operations import add_data_element, set_display_fields
from procworks.validator import CorrectnessError

client = TestClient(app)


def _schema(name: str) -> tuple[str, str]:
    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Prüfen", "after_node_id": "start"}
    ).json()
    node = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Prüfen")
    for elem, dtype in (("bestellnr", "STRING"), ("lieferant", "STRING"), ("betrag", "FLOAT")):
        client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": elem, "data_type": dtype, "element_id": elem},
        )
    return sid, node


def test_open_tasks_and_titles_carry_the_naming_values() -> None:
    sid, node = _schema("Benennung – Aufgaben")
    resp = client.post(
        f"/schemas/{sid}/display-fields", json={"element_ids": ["bestellnr", "lieferant"]}
    )
    assert resp.status_code == 200, resp.text
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    values = {"bestellnr": "4711", "lieferant": "Müller"}
    client.put(f"/instances/{iid}/data", json={"values": values})

    [task] = client.get(f"/instances/{iid}/tasks").json()
    titles = client.get("/instance-titles").json()

    assert [(c["name"], c["value"]) for c in task["context"]] == [
        ("bestellnr", "4711"), ("lieferant", "Müller")
    ]
    assert [c["value"] for c in titles[iid]] == ["4711", "Müller"]


def test_more_than_two_naming_elements_are_rejected() -> None:
    sid, _ = _schema("Benennung – zu viele")
    resp = client.post(
        f"/schemas/{sid}/display-fields",
        json={"element_ids": ["bestellnr", "lieferant", "betrag"]},
    )
    assert resp.status_code == 422
    assert {f["code"] for f in resp.json()["detail"]["findings"]} == {"U5.too-many"}


def test_unknown_elements_cannot_name_an_instance() -> None:
    schema = create_empty_schema("Benennung – Kern", schema_id="naming-core")
    with pytest.raises(CorrectnessError, match=r"\[U5\]") as unknown:
        set_display_fields(schema, ["ghost"])
    assert [f.code for f in unknown.value.findings] == ["U5.unknown-element"]


def test_deleting_a_naming_element_never_blocks() -> None:
    schema = create_empty_schema("Benennung – Löschen", schema_id="naming-delete")
    schema = add_data_element(schema, "Nummer", DataType.STRING, element_id="nr")
    schema = set_display_fields(schema, ["nr"])

    schema = delete_data_element(schema, "nr")

    assert schema.display_fields == []


def test_naming_elements_survive_the_bpmn_round_trip() -> None:
    from procworks.bpmn import export_bpmn, import_bpmn

    schema = create_empty_schema("Benennung – BPMN", schema_id="naming-bpmn")
    schema = add_data_element(schema, "Nummer", DataType.STRING, element_id="nr")
    schema = set_display_fields(schema, ["nr"])

    assert import_bpmn(export_bpmn(schema), schema_id="copy").display_fields == ["nr"]
