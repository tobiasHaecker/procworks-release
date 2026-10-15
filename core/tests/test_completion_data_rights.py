# SPDX-License-Identifier: BUSL-1.1
"""A step's completion may only set what the step writes (rule D6).

``/complete`` takes over only the keys the step writes: a step that only READS
a value (an approval reading the amount) must not change it, and unknown keys
are not stored. It is the same guarantee ``PUT …/data`` gives, so neither door
bypasses the other. The external-task path refuses non-writable outputs as
well; interactive completion does the same on the web, the internal and the
``/v1`` endpoint.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app

client = TestClient(app)


def _four_eyes(name: str, *, release: bool = True) -> tuple[str, str, str]:
    """start → "Erfassen" (WRITES betrag) → "Genehmigen" (READS betrag) → end."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    s = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    erfassen = next(n["id"] for n in s["nodes"].values() if n["label"] == "Erfassen")
    s = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Genehmigen", "after_node_id": erfassen}
    ).json()
    genehmigen = next(n["id"] for n in s["nodes"].values() if n["label"] == "Genehmigen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Betrag", "data_type": "DECIMAL", "element_id": "betrag"},
    )
    for node, mode in ((erfassen, "WRITE"), (genehmigen, "READ")):
        client.post(
            f"/schemas/{sid}/data-access",
            json={"node_id": node, "element_id": "betrag", "mode": mode, "mandatory": True},
        )
    staff_via_api(client, sid)
    if release:
        assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    return iid, erfassen, genehmigen


def _complete(iid: str, node: str, data: dict[str, object]) -> Any:
    return client.post(f"/instances/{iid}/complete", json={"node_id": node, "data": data})


def _codes(resp: Any) -> set[str]:
    return {f["code"] for f in resp.json()["detail"]["findings"]}


def test_an_approval_cannot_overwrite_the_amount_it_only_reads() -> None:
    iid, erfassen, genehmigen = _four_eyes("D6 – Vier Augen")
    assert _complete(iid, erfassen, {"betrag": 499.99}).status_code == 200

    resp = _complete(iid, genehmigen, {"betrag": 5_000_000})

    assert resp.status_code == 422
    assert _codes(resp) == {"D6.not-writable"}
    instance = api_module._get_instance_or_404(iid)
    assert instance.data_values["betrag"] == 499.99
    assert instance.node_states[genehmigen].value in ("ACTIVATED", "RUNNING")  # still open


def test_unknown_keys_are_refused() -> None:
    iid, erfassen, _ = _four_eyes("D6 – unbekannt")

    resp = _complete(iid, erfassen, {"betrag": 10, "ghost": "x"})

    assert resp.status_code == 422
    assert _codes(resp) == {"D6.unknown-element"}
    assert "ghost" not in api_module._get_instance_or_404(iid).data_values


def test_values_the_step_writes_go_through() -> None:
    iid, erfassen, _ = _four_eyes("D6 – erlaubt")

    resp = _complete(iid, erfassen, {"betrag": 42.5})

    assert resp.status_code == 200, resp.text
    assert api_module._get_instance_or_404(iid).data_values["betrag"] == 42.5


def test_v1_follows_the_same_rule() -> None:
    iid, erfassen, genehmigen = _four_eyes("D6 – v1")
    ok = client.post(
        f"/v1/instances/{iid}/nodes/{erfassen}/complete", json={"data": {"betrag": 1.0}}
    )
    assert ok.status_code == 200, ok.text

    resp = client.post(
        f"/v1/instances/{iid}/nodes/{genehmigen}/complete", json={"data": {"betrag": 2.0}}
    )

    assert resp.status_code == 422
    assert _codes(resp) == {"D6.not-writable"}


def test_test_instances_are_held_to_the_same_rule() -> None:
    iid, erfassen, genehmigen = _four_eyes("D6 – Prüfinstanz", release=False)
    assert _complete(iid, erfassen, {"betrag": 1.0}).status_code == 200

    assert _complete(iid, genehmigen, {"betrag": 2.0}).status_code == 422
