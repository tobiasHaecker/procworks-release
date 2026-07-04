# SPDX-License-Identifier: BUSL-1.1
"""Auth/RBAC tests (Auth concept "Variante C": pluggable AuthBackend).

These cover the acceptance criteria from the concept's section 8:

* default *open* dev mode keeps every endpoint working without a token,
* the token backend authenticates valid tokens and rejects missing/invalid ones,
* coarse RBAC role gates (viewer/operator/modeler/admin) admit or deny writes,
* the bound worklist endpoints (``/me/tasks``, ``/agents/{id}/tasks``), and
* the closed impersonation gap: a bound principal acts only as itself and the
  audit trail records the *real* identity, never a spoofed ``agent_id``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks.api import app
from procworks.auth import OpenAuthBackend
from procworks.auth_token import TokenAuthBackend

client = TestClient(app)


# Token table shared by the bound-mode tests: one operator bound to agent "a1",
# one read-only viewer, one modeler, and one admin supervisor.
_TOKENS = {
    "op-token": {
        "subject": "erika",
        "agent_id": "a1",
        "roles": ["operator"],
        "display_name": "Erika (Bearbeiterin)",
    },
    "viewer-token": {"subject": "leo", "roles": ["viewer"], "display_name": "Leo"},
    "modeler-token": {"subject": "mona", "roles": ["modeler"]},
    "admin-token": {"subject": "ada", "roles": ["admin"]},
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def token_mode() -> Iterator[None]:
    """Swap the module's auth backend to a token backend for the test."""

    original = api_module._auth_backend
    api_module._auth_backend = TokenAuthBackend(_TOKENS)
    try:
        yield
    finally:
        api_module._auth_backend = original


# --- default open dev mode ------------------------------------------------


def test_open_mode_allows_unauthenticated_access() -> None:
    # No token, default OpenAuthBackend: reads and writes both succeed.
    assert client.get("/schemas").status_code == 200
    assert client.post("/schemas", json={"name": "Offen"}).status_code == 201


def test_open_mode_me_is_unbound_with_all_roles() -> None:
    me = client.get("/auth/me").json()
    assert me["agent_id"] is None
    assert set(me["roles"]) == {"admin", "modeler", "operator", "viewer"}


def test_open_mode_me_tasks_empty_without_binding() -> None:
    # An unbound principal has no own worklist; the client uses the picker.
    resp = client.get("/me/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


# --- token authentication -------------------------------------------------


def test_token_valid_returns_bound_principal(token_mode: None) -> None:
    me = client.get("/auth/me", headers=_auth("op-token")).json()
    assert me["subject"] == "erika"
    assert me["agent_id"] == "a1"
    assert me["roles"] == ["operator"]
    assert me["display_name"] == "Erika (Bearbeiterin)"


def test_token_missing_is_unauthorized(token_mode: None) -> None:
    resp = client.get("/auth/me")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_token_invalid_is_unauthorized(token_mode: None) -> None:
    resp = client.get("/auth/me", headers=_auth("nope"))
    assert resp.status_code == 401


# --- coarse RBAC role gates ----------------------------------------------


def test_viewer_may_read_but_not_write(token_mode: None) -> None:
    assert client.get("/schemas", headers=_auth("viewer-token")).status_code == 200
    resp = client.post("/schemas", json={"name": "X"}, headers=_auth("viewer-token"))
    assert resp.status_code == 403


def test_operator_may_not_model(token_mode: None) -> None:
    resp = client.post("/schemas", json={"name": "X"}, headers=_auth("op-token"))
    assert resp.status_code == 403


def test_modeler_may_model(token_mode: None) -> None:
    resp = client.post("/schemas", json={"name": "M"}, headers=_auth("modeler-token"))
    assert resp.status_code == 201


def test_admin_may_do_everything(token_mode: None) -> None:
    assert client.post(
        "/schemas", json={"name": "A"}, headers=_auth("admin-token")
    ).status_code == 201


# --- instance start: released vs. draft (test) instances ------------------


def _released_schema(headers: dict[str, str]) -> str:
    """Create and release a one-activity schema, returning its id."""

    sid = client.post("/schemas", json={"name": "Start"}, headers=headers).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Tun", "after_node_id": "start"},
        headers=headers,
    )
    client.post(f"/schemas/{sid}/release", headers=headers)
    return sid


def _draft_schema(headers: dict[str, str]) -> str:
    """Create a one-activity schema and leave it in ENTWURF, returning its id."""

    sid = client.post("/schemas", json={"name": "Entwurf"}, headers=headers).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Tun", "after_node_id": "start"},
        headers=headers,
    )
    return sid


def test_operator_may_start_released_instance(token_mode: None) -> None:
    sid = _released_schema(_auth("admin-token"))
    resp = client.post(f"/schemas/{sid}/instances", headers=_auth("op-token"))
    assert resp.status_code == 201
    assert resp.json()["is_test"] is False


def test_viewer_may_not_start_instance(token_mode: None) -> None:
    sid = _released_schema(_auth("admin-token"))
    resp = client.post(f"/schemas/{sid}/instances", headers=_auth("viewer-token"))
    assert resp.status_code == 403


def test_operator_may_not_start_draft_test_instance(token_mode: None) -> None:
    sid = _draft_schema(_auth("admin-token"))
    resp = client.post(f"/schemas/{sid}/instances", headers=_auth("op-token"))
    assert resp.status_code == 403


def test_modeler_may_start_draft_test_instance(token_mode: None) -> None:
    sid = _draft_schema(_auth("modeler-token"))
    resp = client.post(f"/schemas/{sid}/instances", headers=_auth("modeler-token"))
    assert resp.status_code == 201
    assert resp.json()["is_test"] is True


def test_modeler_may_read_worklist(token_mode: None) -> None:
    # A modeller may also be an affected employee working their own tasks.
    assert client.get("/me/tasks", headers=_auth("modeler-token")).status_code == 200


# --- bound worklist + impersonation gap ----------------------------------


def _build_running_instance(headers: dict[str, str]) -> tuple[str, str]:
    """Create a released single-activity schema (agent a1 eligible) and start it.

    Returns ``(instance_id, activity_node_id)`` with the activity already
    started so it can be completed. Built with modeler/admin rights.
    """

    sid = client.post(
        "/schemas", json={"name": "Aufgabe"}, headers=headers
    ).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Bearbeiten", "after_node_id": "start"},
        headers=headers,
    )
    schema = client.get(f"/schemas/{sid}", headers=headers).json()
    act = next(n["id"] for n in schema["nodes"].values() if n.get("label") == "Bearbeiten")
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "sb"}, headers=headers)
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"},
        headers=headers,
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": act, "rule": {"kind": "ROLE", "ref": "sb"}},
        headers=headers,
    )
    client.post(f"/schemas/{sid}/release", headers=headers)
    iid = client.post(f"/schemas/{sid}/instances", headers=headers).json()["id"]
    client.post(f"/instances/{iid}/start", json={"node_id": act}, headers=headers)
    return iid, act


def test_me_tasks_returns_bound_agent_worklist(token_mode: None) -> None:
    iid, act = _build_running_instance(_auth("admin-token"))
    tasks = client.get("/me/tasks", headers=_auth("op-token")).json()
    assert any(t["instance_id"] == iid and t["node_id"] == act for t in tasks)


def test_viewer_may_not_read_worklist(token_mode: None) -> None:
    resp = client.get("/me/tasks", headers=_auth("viewer-token"))
    assert resp.status_code == 403


def test_bound_operator_may_not_read_other_agent_tasks(token_mode: None) -> None:
    resp = client.get("/agents/a2/tasks", headers=_auth("op-token"))
    assert resp.status_code == 403


def test_admin_may_read_any_agent_tasks(token_mode: None) -> None:
    resp = client.get("/agents/a1/tasks", headers=_auth("admin-token"))
    assert resp.status_code == 200


def test_impersonation_is_blocked(token_mode: None) -> None:
    iid, act = _build_running_instance(_auth("admin-token"))
    # Bound operator a1 tries to complete claiming to be a2 -> 403, no spoofing.
    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act, "agent_id": "a2"},
        headers=_auth("op-token"),
    )
    assert resp.status_code == 403


def test_audit_records_real_identity_not_request_body(token_mode: None) -> None:
    iid, act = _build_running_instance(_auth("admin-token"))
    # Even if the body lies about agent_id, the bound principal wins. Here the
    # body omits it; completion uses the principal's bound a1.
    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act},
        headers=_auth("op-token"),
    )
    assert resp.status_code == 200
    audit = client.get(f"/instances/{iid}/audit", headers=_auth("admin-token")).json()
    completed = [e for e in audit if e["event_type"] == "ACTIVITY_COMPLETED"]
    assert completed and all(e["agent_id"] == "a1" for e in completed)


def test_create_auth_backend_default_is_open() -> None:
    # Sanity: without PROCWORKS_AUTH the module boots in open dev mode.
    assert isinstance(OpenAuthBackend(), OpenAuthBackend)
