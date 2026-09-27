# SPDX-License-Identifier: BUSL-1.1
"""Monetary amounts as their own data type (Validierung 2026-09-25, VAL-16).

Amounts were modelled as FLOAT, so a value like ``0.1 + 0.2`` (=
``0.30000000000000004``) or ``499.999`` passed silently. ``DataType.DECIMAL``
("Betrag") accepts numbers with at most two decimal places; D3 rejects
anything finer. It stays a JSON number, and it partitions like a number in
XOR decisions (THRESHOLD).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

from procworks.api import app
from procworks.model import (
    DataType,
    WidgetKind,
    discriminator_kind,
    value_matches_type,
    widget_matches_type,
)

client = TestClient(app)


@pytest.mark.parametrize("value", [0, 12, 499.99, 1234.5, -20.1, 10**12])
def test_amounts_with_up_to_two_decimals_are_accepted(value: object) -> None:
    assert value_matches_type(DataType.DECIMAL, value)


@pytest.mark.parametrize(
    "value", [499.999, 0.1 + 0.2, 1e-5, True, "499.99", None, float("inf"), float("nan")]
)
def test_finer_or_non_numeric_values_are_rejected(value: object) -> None:
    assert not value_matches_type(DataType.DECIMAL, value)


def test_an_amount_is_a_number_for_masks_and_decisions() -> None:
    assert widget_matches_type(WidgetKind.NUMBER, DataType.DECIMAL)
    assert discriminator_kind(DataType.DECIMAL) is not None


def test_amount_drives_a_decision_and_rejects_a_third_decimal() -> None:
    sid = client.post("/schemas", json={"name": "Betrag"}).json()["id"]
    s = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in s["nodes"].values() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Bestellwert", "data_type": "DECIMAL", "element_id": "wert"},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": act, "element_id": "wert", "mode": "WRITE", "mandatory": True},
    )
    s = client.post(f"/schemas/{sid}/conditional-insert", json={
        "after_node_id": act, "discriminator": "wert",
        "branches": [{"label": "Klein", "upper": 500}, {"label": "Groß"}],
    })
    assert s.status_code == 200, s.text
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    def complete(value: object) -> object:
        body = {"node_id": act, "data": {"wert": value}}
        return client.post(f"/instances/{iid}/complete", json=body)

    finer = complete(499.999)
    assert finer.status_code == 422  # type: ignore[attr-defined]
    codes = {f["code"] for f in finer.json()["detail"]["findings"]}  # type: ignore[attr-defined]
    assert codes == {"D3.wrong-type"}

    done = complete(499.99)
    assert done.status_code == 200, done.text  # type: ignore[attr-defined]
    ready = client.get(f"/instances/{iid}/worklist").json()["ready_activities"]
    labels = {n["id"]: n["label"] for n in client.get(f"/schemas/{sid}").json()["nodes"].values()}
    assert [labels[r] for r in ready] == ["Klein"]


def test_completion_rejects_a_value_of_the_wrong_type() -> None:
    """Gap found on the way (VAL-16): ``/complete`` stored "vielleicht" in a
    BOOLEAN element, and an XOR decision on it took the "true" branch. The
    boundary now checks the types of known elements like ``PUT …/data`` does."""

    sid = client.post("/schemas", json={"name": "Typ beim Abschluss"}).json()["id"]
    s = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in s["nodes"].values() if n["label"] == "A")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Genehmigt", "data_type": "BOOLEAN", "element_id": "ok"},
    )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    resp = client.post(
        f"/instances/{iid}/complete", json={"node_id": act, "data": {"ok": "vielleicht"}}
    )

    assert resp.status_code == 422
    assert {f["code"] for f in resp.json()["detail"]["findings"]} == {"D3.wrong-type"}
    assert "ok" not in client.get(f"/instances/{iid}").json()["data_values"]
