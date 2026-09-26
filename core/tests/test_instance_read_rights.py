# SPDX-License-Identifier: BUSL-1.1
"""Operators read only the instances they are involved in (Validierung 2026-09-25, VAL-05).

``erika.sander`` (operator) read all 26 demo instances with their data and
audit -- a colleague's leave request included. A personal login whose only
role is ``operator`` now reads an instance only when it appears in its
history as the acting agent or may work (or has claimed) an open step; any
other instance is 404, like a missing one. Viewers (revision), modellers,
admins and machine identities keep reading everything; the global audit is
for viewer/modeler/admin.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)

client = TestClient(app)


def _instance(name: str) -> tuple[str, str, str]:
    """Running instance: "Erfassen" (role sb: a1) → "Genehmigen" (role lead: a3).

    ``a2`` (role other) is never responsible. Built in open mode. Returns
    ``(instance_id, erfassen_id, genehmigen_id)``.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    erfassen = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Genehmigen", "after_node_id": erfassen}
    ).json()
    genehmigen = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Genehmigen")
    for role in ("sb", "other", "lead"):
        client.post(f"/schemas/{sid}/roles", json={"name": role, "role_id": role})
    for agent_id, role in (("a1", "sb"), ("a2", "other"), ("a3", "lead")):
        client.post(
            f"/schemas/{sid}/agents",
            json={"name": agent_id, "role_ids": [role], "agent_id": agent_id},
        )
    for node, role in ((erfassen, "sb"), (genehmigen, "lead")):
        rule = {"kind": "ROLE", "ref": role}
        client.post(f"/schemas/{sid}/staff-rule", json={"node_id": node, "rule": rule})
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    return iid, erfassen, genehmigen


@pytest.fixture
def login() -> Iterator[Any]:
    """``login(name, roles, agent_id)`` → headers; password mode from the first call."""

    backend = PasswordAuthBackend(InMemoryCredentialStore())
    original = api_module._auth_backend

    def _login(name: str, roles: list[str], agent_id: str | None = None) -> dict[str, str]:
        api_module._auth_backend = backend
        backend.create_user(subject=name, login=name, roles=roles, agent_id=agent_id)
        user = backend.store.get_user(name)
        assert user is not None
        backend.store.put_user(
            user.model_copy(
                update={"password_hash": hash_password("secret-pw1"), "must_change": False}
            )
        )
        return {"Authorization": f"Bearer {backend.login(name, 'secret-pw1').token}"}

    try:
        yield _login
    finally:
        api_module._auth_backend = original


_READS = (
    "/instances/{iid}",
    "/instances/{iid}/tasks",
    "/instances/{iid}/worklist",
    "/instances/{iid}/audit",
    "/instances/{iid}/migration-target",
    "/v1/instances/{iid}",
    "/v1/instances/{iid}/tasks",
    "/v1/instances/{iid}/data",
)


def test_uninvolved_operator_gets_404_everywhere(login: Any) -> None:
    iid, _, _ = _instance("Lesen – fremd")
    paul = login("paul", ["operator"], "a2")

    for path in _READS:
        resp = client.get(path.format(iid=iid), headers=paul)
        assert resp.status_code == 404, path
    assert iid not in client.get("/instances", headers=paul).json()


def test_responsible_operator_reads_the_instance(login: Any) -> None:
    iid, _, _ = _instance("Lesen – zuständig")
    erika = login("erika", ["operator"], "a1")

    for path in _READS:
        assert client.get(path.format(iid=iid), headers=erika).status_code == 200, path
    assert iid in client.get("/instances", headers=erika).json()


def test_operator_keeps_reading_after_its_step_is_done(login: Any) -> None:
    # Involvement from the history: erika completed "Erfassen", the open step
    # now belongs to the lead -- erika still sees the instance she worked on.
    iid, erfassen, _ = _instance("Lesen – nach eigenem Schritt")
    erika = login("erika", ["operator"], "a1")
    assert client.post(
        f"/instances/{iid}/complete", json={"node_id": erfassen}, headers=erika
    ).status_code == 200

    assert client.get(f"/instances/{iid}", headers=erika).status_code == 200


def test_the_starter_is_involved(login: Any) -> None:
    iid, _, _ = _instance("Lesen – Starter")
    schema_id = client.get(f"/instances/{iid}").json()["schema_id"]
    paul = login("paul", ["operator"], "a2")

    started = client.post(f"/schemas/{schema_id}/instances", headers=paul)

    assert started.status_code == 201
    assert client.get(f"/instances/{started.json()['id']}", headers=paul).status_code == 200


def test_viewer_modeler_and_admin_read_everything(login: Any) -> None:
    iid, _, _ = _instance("Lesen – Revision")
    for name, roles in (("vera", ["viewer"]), ("mara", ["modeler"]), ("ada", ["admin"])):
        headers = login(name, roles)
        assert client.get(f"/instances/{iid}", headers=headers).status_code == 200, name
        assert client.get(f"/instances/{iid}/audit", headers=headers).status_code == 200, name
        assert client.get("/audit", headers=headers).status_code == 200, name


def test_global_audit_is_not_an_operator_right(login: Any) -> None:
    _instance("Lesen – globales Audit")
    erika = login("erika", ["operator"], "a1")

    assert client.get("/audit", headers=erika).status_code == 403


def test_aggregated_kpis_stay_visible_to_operators(login: Any) -> None:
    _instance("Lesen – Kennzahlen")
    erika = login("erika", ["operator"], "a1")

    assert client.get("/monitoring/kpis", headers=erika).status_code == 200
