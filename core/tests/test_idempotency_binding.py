# SPDX-License-Identifier: BUSL-1.1
"""An ``Idempotency-Key`` stands for exactly one request.

A repeated key replays the first answer only when it repeats the *same*
request: same identity, same endpoint, same path and same body. The same key
with another schema, other data or on another endpoint is a client error and is
refused with 409 ``IDEM.key-reused`` -- never answered with the first request's
result (a confirmation for something that did not happen) and never with a 500.
Another identity has its own keys, and a failed first request is not
remembered, so the key stays usable.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app, get_principal
from procworks.auth import Principal

client = TestClient(app)

_keys = (f"idem-key-{n}" for n in itertools.count(1))


def _schema(name: str, *, release: bool = True, topic: str | None = None) -> tuple[str, str]:
    """One step "Erfassen" writing ``betrag``; automatic external task with ``topic``."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    node = next(n["id"] for n in schema["nodes"].values() if n["type"] == "ACTIVITY")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Betrag", "data_type": "INTEGER", "element_id": "betrag"},
    )
    if topic is not None:
        client.post(f"/schemas/{sid}/service", json={"node_id": node, "name": "Erfassen",
                                                      "automatic": True})
        client.post(f"/schemas/{sid}/automation",
                    json={"node_id": node, "automation": "EXTERNAL_TASK", "topic": topic})
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": node, "element_id": "betrag", "mode": "WRITE", "mandatory": False},
    )
    if topic is None:
        staff_via_api(client, sid)
    if release:
        assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, node


def _instances_of(schema_id: str) -> int:
    ids = api_module._instances.list_ids()
    return sum(
        1 for i in ids if (inst := api_module._instances.get(i)) and inst.schema_id == schema_id
    )


def _refused(resp: object) -> None:
    """Assert the 409 with the idempotency reason (not some other conflict)."""

    assert resp.status_code == 409, resp.text  # type: ignore[attr-defined]
    detail = resp.json()["detail"]  # type: ignore[attr-defined]
    assert detail["code"] == "IDEM.key-reused"


@pytest.fixture
def as_identity() -> Iterator[Callable[[str], None]]:
    def _set(subject: str) -> None:
        app.dependency_overrides[get_principal] = lambda: Principal(
            subject=subject, roles=frozenset({"admin"})
        )

    yield _set
    app.dependency_overrides.pop(get_principal, None)


def test_the_same_request_with_the_same_key_replays_the_first_answer() -> None:
    sid, _ = _schema("Idem gleich")
    headers = {"Idempotency-Key": next(_keys)}

    first = client.post(f"/v1/schemas/{sid}/instances", headers=headers)
    second = client.post(f"/v1/schemas/{sid}/instances", headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert _instances_of(sid) == 1


def test_the_same_key_for_another_schema_is_refused() -> None:
    sid_a, _ = _schema("Idem A")
    sid_b, _ = _schema("Idem B")
    headers = {"Idempotency-Key": next(_keys)}
    assert client.post(f"/v1/schemas/{sid_a}/instances", headers=headers).status_code == 201

    _refused(client.post(f"/v1/schemas/{sid_b}/instances", headers=headers))

    assert _instances_of(sid_b) == 0


def test_the_same_key_with_other_data_is_refused() -> None:
    sid, _ = _schema("Idem Daten")
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    headers = {"Idempotency-Key": next(_keys)}
    first = client.put(f"/v1/instances/{iid}/data", json={"values": {"betrag": 1}},
                       headers=headers)
    assert first.status_code == 200, first.text

    _refused(client.put(f"/v1/instances/{iid}/data", json={"values": {"betrag": 2}},
                        headers=headers))

    assert client.get(f"/v1/instances/{iid}/data").json()["betrag"] == 1


def test_the_same_key_on_another_endpoint_is_refused_never_a_500() -> None:
    sid, node = _schema("Idem Endpunkt")
    headers = {"Idempotency-Key": next(_keys)}
    iid = client.post(f"/v1/schemas/{sid}/instances", headers=headers).json()["id"]

    _refused(client.put(f"/v1/instances/{iid}/data", json={"values": {"betrag": 3}},
                        headers=headers))
    _refused(client.post(f"/v1/instances/{iid}/nodes/{node}/complete",
                         json={"data": {"betrag": 3}}, headers=headers))

    instance = client.get(f"/v1/instances/{iid}").json()
    assert "betrag" not in instance["data_values"]
    assert instance["state"] == "RUNNING"


def test_the_same_key_on_an_external_task_completion_is_refused() -> None:
    topic = next(_keys)
    sid, _ = _schema("Idem Aufgabe", topic=topic)
    headers = {"Idempotency-Key": next(_keys)}
    client.post(f"/v1/schemas/{sid}/instances", headers=headers)
    task = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    ).json()[0]

    _refused(client.post(f"/v1/external-tasks/{task['id']}/complete",
                         json={"worker_id": "w1", "variables": {"betrag": 4}},
                         headers=headers))

    assert client.get(f"/v1/external-tasks/{task['id']}").json()["state"] == "LOCKED"


def test_another_identity_has_its_own_keys(as_identity: Callable[[str], None]) -> None:
    sid, _ = _schema("Idem Identitaet")
    headers = {"Idempotency-Key": next(_keys)}
    as_identity("erp-a")
    first = client.post(f"/v1/schemas/{sid}/instances", headers=headers)
    as_identity("erp-b")
    second = client.post(f"/v1/schemas/{sid}/instances", headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_a_failed_first_request_does_not_bind_the_key() -> None:
    draft, _ = _schema("Idem Entwurf", release=False)
    sid, _ = _schema("Idem danach")
    headers = {"Idempotency-Key": next(_keys)}

    failed = client.post(f"/v1/schemas/{draft}/instances", headers=headers)
    assert failed.status_code == 409
    assert "only released schemas" in failed.json()["detail"]["message"]

    started = client.post(f"/v1/schemas/{sid}/instances", headers=headers)
    assert started.status_code == 201, started.text


def test_starting_a_draft_via_v1_names_the_reason_code() -> None:
    """Die Ablehnung eines Entwurfs trug nur eine englische Meldung. Jetzt
    trägt sie den Code der Engine-Ablehnung, den die Oberfläche formuliert;
    ``message`` bleibt für bestehende Integrationen gleich."""

    draft, _ = _schema("Entwurf mit Code", release=False)

    refused = client.post(f"/v1/schemas/{draft}/instances")

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "EX.not-released"
    assert detail["params"] == {}
    assert detail["message"] == "only released schemas can be instantiated via /v1"
