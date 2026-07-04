# SPDX-License-Identifier: BUSL-1.1
"""Inbound integration API (/v1) — control of ProcWorks by external tools (P1).

These tests exercise the versioned ``/v1`` endpoints, the integration-scope gate
and the ``Idempotency-Key`` replay behaviour. Two identities are covered: the
default *open* principal (passes by role) and a simulated *integration service
token* injected via FastAPI ``dependency_overrides`` (passes by scope only).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from procworks.api import app, get_principal
from procworks.auth import (
    INTEGRATION,
    SCOPE_DATA_READ,
    SCOPE_DATA_WRITE,
    SCOPE_INSTANCES_START,
    SCOPE_TASKS_COMPLETE,
    Principal,
)

client = TestClient(app)


def _released_schema_with_data() -> str:
    """A released single-activity schema with one INTEGER data element."""

    sid = client.post("/schemas", json={"name": "Inbound"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Bearbeiten", "after_node_id": "start"},
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Betrag", "data_type": "INTEGER", "element_id": "betrag"},
    )
    client.post(f"/schemas/{sid}/release")
    return sid


def _ready_node(instance_id: str) -> str:
    wl = client.get(f"/instances/{instance_id}/worklist").json()
    return wl["ready_activities"][0]


# --- open-mode happy path (role-based access to /v1) ---------------------


def test_v1_start_get_and_write_data() -> None:
    sid = _released_schema_with_data()

    resp = client.post(f"/v1/schemas/{sid}/instances")
    assert resp.status_code == 201
    iid = resp.json()["id"]
    assert resp.json()["state"] == "RUNNING"

    resp = client.get(f"/v1/instances/{iid}/data")
    assert resp.status_code == 200
    assert resp.json() == {}

    resp = client.put(f"/v1/instances/{iid}/data", json={"values": {"betrag": 1200}})
    assert resp.status_code == 200
    assert resp.json() == {"betrag": 1200}

    resp = client.get(f"/v1/instances/{iid}/data")
    assert resp.json() == {"betrag": 1200}


def test_v1_complete_task_with_data() -> None:
    sid = _released_schema_with_data()
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    node_id = _ready_node(iid)

    resp = client.post(
        f"/v1/instances/{iid}/nodes/{node_id}/complete",
        json={"data": {"betrag": 50}},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "COMPLETED"
    assert resp.json()["data_values"]["betrag"] == 50


def test_v1_start_rejects_draft_schema() -> None:
    sid = client.post("/schemas", json={"name": "Entwurf"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "S", "after_node_id": "start"},
    )
    resp = client.post(f"/v1/schemas/{sid}/instances")
    assert resp.status_code == 409


# --- data write validation (runtime D3 at the boundary) ------------------


def test_v1_put_data_type_mismatch_is_422() -> None:
    sid = _released_schema_with_data()
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    resp = client.put(
        f"/v1/instances/{iid}/data", json={"values": {"betrag": "nope"}}
    )
    assert resp.status_code == 422
    rules = [f["rule"] for f in resp.json()["detail"]["findings"]]
    assert "D3" in rules


def test_v1_put_data_unknown_element_is_422() -> None:
    sid = _released_schema_with_data()
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    resp = client.put(
        f"/v1/instances/{iid}/data", json={"values": {"ghost": 1}}
    )
    assert resp.status_code == 422


# --- idempotency ----------------------------------------------------------


def test_v1_start_is_idempotent() -> None:
    sid = _released_schema_with_data()
    headers = {"Idempotency-Key": "start-key-1"}
    first = client.post(f"/v1/schemas/{sid}/instances", headers=headers)
    second = client.post(f"/v1/schemas/{sid}/instances", headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_v1_complete_is_idempotent() -> None:
    sid = _released_schema_with_data()
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    node_id = _ready_node(iid)
    headers = {"Idempotency-Key": "complete-key-1"}

    first = client.post(
        f"/v1/instances/{iid}/nodes/{node_id}/complete", json={}, headers=headers
    )
    second = client.post(
        f"/v1/instances/{iid}/nodes/{node_id}/complete", json={}, headers=headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    # Without the key, completing the now-finished node would fail (409),
    # proving the second call above did not re-execute.
    again = client.post(f"/v1/instances/{iid}/nodes/{node_id}/complete", json={})
    assert again.status_code == 409


# --- integration-scope enforcement (service token) -----------------------


@pytest.fixture
def as_service() -> Iterator[Callable[[set[str]], None]]:
    """Override the request identity with an integration token of given scopes."""

    def _set(scopes: set[str]) -> None:
        app.dependency_overrides[get_principal] = lambda: Principal(
            subject="svc",
            roles=frozenset({INTEGRATION}),
            scopes=frozenset(scopes),
        )

    yield _set
    app.dependency_overrides.pop(get_principal, None)


def test_service_token_needs_start_scope(as_service) -> None:
    sid = _released_schema_with_data()

    as_service(set())
    assert client.post(f"/v1/schemas/{sid}/instances").status_code == 403

    as_service({SCOPE_INSTANCES_START})
    assert client.post(f"/v1/schemas/{sid}/instances").status_code == 201


def test_service_token_data_scopes_are_separate(as_service) -> None:
    sid = _released_schema_with_data()
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]

    # read-only token may read but not write
    as_service({SCOPE_DATA_READ})
    assert client.get(f"/v1/instances/{iid}/data").status_code == 200
    assert (
        client.put(
            f"/v1/instances/{iid}/data", json={"values": {"betrag": 1}}
        ).status_code
        == 403
    )

    # write token may write
    as_service({SCOPE_DATA_WRITE})
    assert (
        client.put(
            f"/v1/instances/{iid}/data", json={"values": {"betrag": 1}}
        ).status_code
        == 200
    )


def test_service_token_complete_scope(as_service) -> None:
    sid = _released_schema_with_data()
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    node_id = _ready_node(iid)

    as_service({SCOPE_DATA_READ})
    assert (
        client.post(
            f"/v1/instances/{iid}/nodes/{node_id}/complete", json={}
        ).status_code
        == 403
    )

    as_service({SCOPE_TASKS_COMPLETE})
    assert (
        client.post(
            f"/v1/instances/{iid}/nodes/{node_id}/complete", json={}
        ).status_code
        == 200
    )
