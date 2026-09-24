# SPDX-License-Identifier: BUSL-1.1
"""Acting in another person's name: only machines delegate, people act as themselves.

A login without an agent binding used to be able to *name* any eligible agent
and work the step in that person's name: the core checked the staff rule
against the named agent, recorded them as performer, and the audit showed the
named person -- not the login that did it. That undermined the four-eyes
principle and the audit trail, and the web client even offered it as a person
picker in "Meine Aufgaben" (found 2026-09-24).

The boundary rule (``api._may_act_for_others``): personal logins -- password
and personal JWT -- act only as themselves (403 when they name an agent); the
supervision path with a mandatory reason stays open to them. Machine
identities keep the documented delegation (static tokens, the ``integration``
role), as do the open dev mode and throw-away test instances -- and the audit
records the sender as ``detail.actor`` next to the named agent.

Each test pins the reason (status *and* detail text or audit fields), and the
counter-examples keep the rule from growing into a ban on legitimate paths.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app
from procworks.audit import AuditEvent, EventType
from procworks.auth import AuthBackend
from procworks.auth_jwt import JwtAuthBackend
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)
from procworks.auth_token import TokenAuthBackend

client = TestClient(app)

#: Start of the 403 detail for a personal login that names an agent.
_REFUSAL = "Dieser Login ist keinem Bearbeiter zugeordnet"


def _task_schema(name: str, *, release: bool = True) -> tuple[str, str]:
    """Schema with one activity bound to role 'sb' (agent a1), built in open mode."""

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
    if release:
        assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, act_id


def _instance(name: str, *, release: bool = True) -> tuple[str, str]:
    """A running instance (a test instance for ``release=False``), in open mode."""

    sid, act_id = _task_schema(name, release=release)
    resp = client.post(f"/schemas/{sid}/instances")
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], act_id


def _completion_event(iid: str, act_id: str) -> AuditEvent:
    """The single ACTIVITY_COMPLETED audit event of one step."""

    events = [
        e
        for e in api_module._audit.for_instance(iid)
        if e.node_id == act_id and e.event_type is EventType.ACTIVITY_COMPLETED
    ]
    assert len(events) == 1, events
    return events[0]


@pytest.fixture
def use_backend() -> Iterator[Any]:
    """Swap the module's auth backend for the test and restore it afterwards."""

    original = api_module._auth_backend

    def _set(backend: AuthBackend) -> None:
        api_module._auth_backend = backend

    try:
        yield _set
    finally:
        api_module._auth_backend = original


def _password_login(backend: PasswordAuthBackend, login: str, **kw: object) -> dict[str, str]:
    """Create a ready-to-use password user and return its auth headers."""

    backend.create_user(subject=login, login=login, **kw)  # type: ignore[arg-type]
    user = backend.store.get_user(login)
    assert user is not None
    backend.store.put_user(
        user.model_copy(update={"password_hash": hash_password("secret-pw1"), "must_change": False})
    )
    return {"Authorization": f"Bearer {backend.login(login, 'secret-pw1').token}"}


# --- personal logins: act only as themselves ---------------------------------


def test_password_login_cannot_complete_in_another_persons_name(use_backend: Any) -> None:
    iid, act_id = _instance("Im Namen – Passwort")
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    use_backend(backend)
    mara = _password_login(backend, "mara", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "agent_id": "a1", "data": {}},
        headers=mara,
    )

    assert resp.status_code == 403
    assert resp.json()["detail"].startswith(_REFUSAL)
    # Refused before the engine ran: nothing completed, nothing recorded.
    inst = client.get(f"/instances/{iid}", headers=mara).json()
    assert inst["node_states"][act_id] != "COMPLETED"
    assert act_id not in inst["performed_by"]


def test_password_login_cannot_claim_in_another_persons_name(use_backend: Any) -> None:
    iid, act_id = _instance("Im Namen – Uebernahme")
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    use_backend(backend)
    mara = _password_login(backend, "mara", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/claim", json={"node_id": act_id, "agent_id": "a1"}, headers=mara
    )

    assert resp.status_code == 403
    assert resp.json()["detail"].startswith(_REFUSAL)


def test_personal_jwt_login_cannot_complete_in_another_persons_name(use_backend: Any) -> None:
    iid, act_id = _instance("Im Namen – Firmenkonto")
    use_backend(_jwt_backend())
    headers = _jwt(sub="mona", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "agent_id": "a1", "data": {}},
        headers=headers,
    )

    assert resp.status_code == 403
    assert resp.json()["detail"].startswith(_REFUSAL)


def test_password_login_still_completes_as_supervision(use_backend: Any) -> None:
    """Gegenprobe: without naming anyone, the reasoned supervision path works."""

    iid, act_id = _instance("Im Namen – Aufsicht")
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    use_backend(backend)
    mara = _password_login(backend, "mara", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "data": {}, "supervision_reason": "Vertretung fehlt"},
        headers=mara,
    )

    assert resp.status_code == 200, resp.text
    event = _completion_event(iid, act_id)
    assert event.agent_id is None
    assert event.detail["actor"] == "mara"


def test_password_login_may_switch_persons_in_a_test_instance(use_backend: Any) -> None:
    """Gegenprobe: a draft's throw-away test run keeps its person switch."""

    iid, act_id = _instance("Im Namen – Pruefinstanz", release=False)
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    use_backend(backend)
    mara = _password_login(backend, "mara", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "agent_id": "a1", "data": {}},
        headers=mara,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["performed_by"][act_id] == "a1"


# --- machine identities: delegation stays, the sender is recorded ------------


def test_static_token_delegates_and_the_audit_names_the_sender(use_backend: Any) -> None:
    iid, act_id = _instance("Im Namen – Token")
    use_backend(TokenAuthBackend({"erp-token": {"subject": "erp", "roles": ["operator"]}}))

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "agent_id": "a1", "data": {}},
        headers={"Authorization": "Bearer erp-token"},
    )

    assert resp.status_code == 200, resp.text
    event = _completion_event(iid, act_id)
    assert event.agent_id == "a1"
    assert event.detail["actor"] == "erp"


def test_jwt_integration_account_delegates_via_v1(use_backend: Any) -> None:
    iid, act_id = _instance("Im Namen – Integration")
    use_backend(_jwt_backend())
    headers = _jwt(sub="svc-erp", roles=["integration"], scope="tasks:complete")

    resp = client.post(
        f"/v1/instances/{iid}/nodes/{act_id}/complete",
        json={"agent_id": "a1", "data": {}},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    event = _completion_event(iid, act_id)
    assert event.agent_id == "a1"
    assert event.detail["actor"] == "svc-erp"


# --- web client: no person picker to act in someone's name --------------------

_APP_JS = Path(__file__).resolve().parents[2] / "web" / "app.js"


def _js_function(src: str, head: str) -> str:
    """Source of one top-level JS function (up to the next top-level item)."""

    start = src.index(head)
    end = src.find("\n}\n", start)
    return src[start : end + 3]


def test_web_task_view_offers_only_supervision_to_personal_logins() -> None:
    """„Meine Aufgaben" offered a person picker plus Übernehmen/Erledigen in that
    person's name. For a personal login without an agent it now only views the
    list and completes as supervision -- mirroring the core rule, not deciding it.
    """

    src = _APP_JS.read_text(encoding="utf-8")
    gate = _js_function(src, "function mayActForOthers(")
    for needle in ('"open"', '"token"', '"integration"'):
        assert needle in gate, f"mayActForOthers kennt {needle} nicht"
    view = _js_function(src, "async function viewTasks(")
    assert "const supervise = !bound && !mayActForOthers();" in view
    assert "supervisionTaskActions(t)" in view
    actions = _js_function(src, "function supervisionTaskActions(")
    assert "completeTask(t, null)" in actions
    for forbidden in ("claimTask(", "agentId", "suspendTask(", "failTask("):
        assert forbidden not in actions, f"Aufsichtsaktionen nennen eine Person: {forbidden}"


# --- JWT helpers ---------------------------------------------------------------

_ISS = "https://idp.example"
_AUD = "procworks"
_SECRET = "act-on-behalf-secret-0123456789"
_NOW = 1_900_000_000


def _jwt_backend() -> JwtAuthBackend:
    return JwtAuthBackend(issuer=_ISS, audience=_AUD, hs256_secret=_SECRET, now=lambda: _NOW)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(**claims: Any) -> dict[str, str]:
    """Signed HS256 token without an agent claim (an unbound login)."""

    payload = {"iss": _ISS, "aud": _AUD, "exp": _NOW + 600, **claims}
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    sig = hmac.new(_SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return {"Authorization": f"Bearer {head}.{body}.{_b64(sig)}"}
