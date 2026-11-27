# SPDX-License-Identifier: BUSL-1.1
"""Input checks on mask fields.

Masks had no minimum, maximum or pattern; a wrong value was only noticed later,
if at all. ``FormField`` carries optional ``min_value``/``max_value`` (number
fields) and ``pattern``/``max_length`` (text fields). U2 checks at modelling
time that they fit the field; U4 enforces them on every completion at the
boundary -- the web form marks the field first, but the API cannot be used
around the rule. Each test pins the rule code.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

from procworks.api import app

client = TestClient(app)


def _schema(name: str) -> tuple[str, str]:
    """Draft: start → "Erfassen" (writes betrag FLOAT, kennzeichen STRING) → end."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    node = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    for elem, dtype in (("betrag", "FLOAT"), ("kennzeichen", "STRING")):
        client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": elem, "data_type": dtype, "element_id": elem},
        )
    return sid, node


def _form(sid: str, node: str, *fields: dict[str, Any]) -> Any:
    return client.post(f"/schemas/{sid}/nodes/{node}/form", json={"fields": list(fields)})


def _codes(resp: Any) -> set[str]:
    return {f["code"] for f in resp.json()["detail"]["findings"]}


# --- U2: the rules must fit the field -----------------------------------------


def test_bounds_on_a_text_field_are_rejected() -> None:
    sid, node = _schema("Regeln – Grenzen am Text")
    resp = _form(sid, node, {"element_id": "kennzeichen", "widget": "TEXT", "min_value": 1})
    assert resp.status_code == 422
    assert "U2.bounds-not-number" in _codes(resp)


def test_crossed_bounds_are_rejected() -> None:
    sid, node = _schema("Regeln – Grenzen vertauscht")
    resp = _form(
        sid, node, {"element_id": "betrag", "widget": "NUMBER", "min_value": 10, "max_value": 1}
    )
    assert resp.status_code == 422
    assert "U2.bounds-order" in _codes(resp)


def test_pattern_on_a_number_field_and_an_invalid_pattern_are_rejected() -> None:
    sid, node = _schema("Regeln – Muster")
    on_number = _form(sid, node, {"element_id": "betrag", "widget": "NUMBER", "pattern": "\\d+"})
    broken = _form(sid, node, {"element_id": "kennzeichen", "widget": "TEXT", "pattern": "(["})
    assert "U2.text-rule-not-text" in _codes(on_number)
    assert "U2.pattern-invalid" in _codes(broken)


def test_fitting_rules_are_stored() -> None:
    sid, node = _schema("Regeln – passend")
    resp = _form(
        sid,
        node,
        {"element_id": "betrag", "widget": "NUMBER", "min_value": 0, "max_value": 10000},
        {"element_id": "kennzeichen", "widget": "TEXT", "pattern": "[A-Z]{2}-\\d{4}",
         "max_length": 7},
    )
    assert resp.status_code == 200, resp.text
    fields = {f["element_id"]: f for f in resp.json()["forms"][node]["fields"]}
    assert (fields["betrag"]["min_value"], fields["betrag"]["max_value"]) == (0, 10000)
    assert fields["kennzeichen"]["pattern"] == "[A-Z]{2}-\\d{4}"


# --- U4: every completion is checked --------------------------------------------


def _running(name: str) -> tuple[str, str]:
    sid, node = _schema(name)
    assert _form(
        sid,
        node,
        {"element_id": "betrag", "widget": "NUMBER", "min_value": 0, "max_value": 10000},
        {"element_id": "kennzeichen", "widget": "TEXT", "pattern": "[A-Z]{2}-\\d{4}",
         "max_length": 7},
    ).status_code == 200
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return client.post(f"/schemas/{sid}/instances").json()["id"], node


def test_completion_outside_the_bounds_is_rejected() -> None:
    iid, node = _running("Regeln – Abschluss zu hoch")
    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": node, "data": {"betrag": 5_000_000, "kennzeichen": "AB-1234"}},
    )
    assert resp.status_code == 422
    assert _codes(resp) == {"U4.above-max"}
    assert client.get(f"/instances/{iid}").json()["state"] == "RUNNING"


def test_completion_with_a_wrong_format_is_rejected_also_via_v1() -> None:
    iid, node = _running("Regeln – Format über v1")
    resp = client.post(
        f"/v1/instances/{iid}/nodes/{node}/complete",
        json={"data": {"betrag": 10.0, "kennzeichen": "ab1234"}},
    )
    assert resp.status_code == 422
    assert _codes(resp) == {"U4.pattern"}


def test_completion_within_the_rules_passes() -> None:
    iid, node = _running("Regeln – Abschluss passend")
    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": node, "data": {"betrag": 499.99, "kennzeichen": "AB-1234"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "COMPLETED"


def _form_raw(sid: str, node: str, field_json: str) -> Any:
    """POST a mask whose single field is given as raw JSON text.

    The test client's ``json=`` encoder refuses NaN/Infinity, other clients
    send these literals, and the API's JSON parser accepts them.
    """

    return client.post(
        f"/schemas/{sid}/nodes/{node}/form",
        content=f'{{"fields": [{field_json}]}}',
        headers={"content-type": "application/json"},
    )


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_bounds_are_rejected_for_that_reason(literal: str) -> None:
    """A NaN bound compares false with every value: stored, never enforced."""

    sid, node = _schema(f"Regeln – Grenze {literal}")
    resp = _form_raw(
        sid,
        node,
        f'{{"element_id": "betrag", "widget": "NUMBER", "min_value": 1, "max_value": {literal}}}',
    )
    assert resp.status_code == 422
    assert _codes(resp) == {"U2.bounds-not-finite"}
