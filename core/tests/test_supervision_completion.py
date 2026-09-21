# SPDX-License-Identifier: BUSL-1.1
"""Aufsichtseingriff: completing a staff-rule step without an agent binding.

The core checks BZR eligibility only when an ``agent_id`` is present. A login
*without* an agent binding (modeler/admin with no person behind it) therefore
completes a step outside the staff rule. Found in the acceptance test 2026-09
(``docs/Ueberarbeitungskonzept-Abnahmetest-2026-09.md`` §4.1): the audit then
showed "System" and the four-eyes rule was silently bypassable.

The boundary now makes this an explicit, reasoned **supervision** action and
refuses it entirely while licensing is enforced -- otherwise an unbound login
would be unlimited unlicensed labour. These tests pin each branch *and* its
reason, not merely the status code.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app
from procworks.audit import EventType
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)

client = TestClient(app)


def _released_task_schema(name: str) -> tuple[str, str]:
    """Released schema with one activity bound to role 'sb' (agent a1).

    Built in open mode (the fixture swaps the backend afterwards), so setup
    needs no tokens.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Bearbeiten", "after_node_id": "start"},
    ).json()
    act_id = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Bearbeiten")
    client.post(f"/schemas/{sid}/roles", json={"name": "Sachbearbeiter", "role_id": "sb"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"},
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": act_id, "rule": {"kind": "ROLE", "ref": "sb"}},
    )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, act_id


def _login(backend: PasswordAuthBackend, login: str, **kw: object) -> dict[str, str]:
    """Create a ready-to-use user (no forced change) and return auth headers."""

    backend.create_user(subject=login, login=login, **kw)  # type: ignore[arg-type]
    user = backend.store.get_user(login)
    assert user is not None
    backend.store.put_user(
        user.model_copy(
            update={"password_hash": hash_password("secret-pw1"), "must_change": False}
        )
    )
    token = backend.login(login, "secret-pw1").token
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def task() -> Iterator[tuple[str, str, PasswordAuthBackend]]:
    """A running instance on a staffed step, served under password auth."""

    sid, act_id = _released_task_schema("Aufsicht")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    api_module._auth_backend = backend
    try:
        yield iid, act_id, backend
    finally:
        api_module._auth_backend = original


def test_unbound_login_without_reason_is_refused(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])

    resp = client.post(f"/instances/{iid}/complete", json={"node_id": act_id}, headers=headers)

    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("Aufsichtseingriff")
    # Refused before the engine ran: the step is still open.
    inst = client.get(f"/instances/{iid}", headers=headers).json()
    assert inst["node_states"][act_id] != "COMPLETED"


def test_unbound_login_with_blank_reason_is_refused(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "supervision_reason": "   "},
        headers=headers,
    )

    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("Aufsichtseingriff")


def test_unbound_login_with_reason_completes_and_is_audited(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "supervision_reason": "Bearbeiterin krank"},
        headers=headers,
    )

    assert resp.status_code == 200
    events = client.get(f"/instances/{iid}/audit", headers=headers).json()
    done = [e for e in events if e["event_type"] == EventType.ACTIVITY_COMPLETED]
    sup = [e for e in events if e["event_type"] == EventType.ACTIVITY_SUPERVISED]
    # The regular completion names the login instead of leaving it blank ...
    assert done[-1]["agent_id"] is None
    assert done[-1]["detail"]["actor"] == "mara"
    # ... and the exception itself is on record with its reason.
    assert len(sup) == 1
    assert sup[0]["node_id"] == act_id
    assert sup[0]["detail"] == {"actor": "mara", "reason": "Bearbeiterin krank"}


def test_bound_login_needs_no_reason(task: tuple[str, str, PasswordAuthBackend]) -> None:
    iid, act_id, backend = task
    headers = _login(backend, "erika", roles=["operator"], agent_id="a1")

    resp = client.post(f"/instances/{iid}/complete", json={"node_id": act_id}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["performed_by"][act_id] == "a1"
    events = client.get(f"/instances/{iid}/audit", headers=headers).json()
    assert not any(e["event_type"] == EventType.ACTIVITY_SUPERVISED for e in events)
    done = [e for e in events if e["event_type"] == EventType.ACTIVITY_COMPLETED]
    assert "actor" not in done[-1]["detail"]


def test_bound_but_ineligible_login_is_still_refused_by_the_core(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    """A reason never unlocks a *bound* login: the core BZR check stays in charge."""

    iid, act_id, backend = task
    headers = _login(backend, "fremd", roles=["operator"], agent_id="ghost")

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "supervision_reason": "egal"},
        headers=headers,
    )

    assert resp.status_code == 409
    assert "not eligible" in str(resp.json()["detail"])


def test_supervision_is_refused_while_licensing_is_enforced(
    task: tuple[str, str, PasswordAuthBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unbound completion would be unlicensed labour -- no reason can buy it."""

    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])
    monkeypatch.setattr(api_module._license, "_pubkey", "configured")

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "supervision_reason": "Bearbeiterin krank"},
        headers=headers,
    )

    assert resp.status_code == 403
    assert "Lizenzierung" in resp.json()["detail"]
    monkeypatch.undo()
    inst = client.get(f"/instances/{iid}", headers=headers).json()
    assert inst["node_states"][act_id] != "COMPLETED"


def test_open_dev_mode_keeps_working_without_reason() -> None:
    """No identity exists in open mode -- the quickstart must not change."""

    sid, act_id = _released_task_schema("AufsichtOffen")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    resp = client.post(f"/instances/{iid}/complete", json={"node_id": act_id})

    assert resp.status_code == 200


def test_v1_passes_the_supervision_reason_through(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    """The integration mirror must offer the same way out, not a dead end."""

    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])
    url = f"/v1/instances/{iid}/nodes/{act_id}/complete"

    refused = client.post(url, json={}, headers=headers)
    assert refused.status_code == 422
    assert refused.json()["detail"].startswith("Aufsichtseingriff")

    ok = client.post(url, json={"supervision_reason": "Nachtlauf"}, headers=headers)
    assert ok.status_code == 200
