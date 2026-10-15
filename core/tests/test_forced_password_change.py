# SPDX-License-Identifier: BUSL-1.1
"""A pending password change is enforced by the server, not only by the client.

A login created by an administrator (or the start administrator) carries a
one-off initial password and the flag ``must_change``. Until that password is
replaced, the session may only see who it is (``/auth/me``), change the
password and log out. Every other endpoint answers 403 with the code
``PW.change-required`` -- otherwise whoever learns an initial password (log
line, passed-on note) could work with the account indefinitely without ever
replacing it. Machine tokens and JWT logins never carry the flag.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web_source import app_js_source
from web_vm import needs_node, run_app_js

import procworks.api as api_module
from procworks.api import app
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)
from procworks.auth_token import TokenAuthBackend

client = TestClient(app)

WORKER = {"worker-geheim": {"subject": "erp-worker", "roles": ["integration"],
                            "scopes": ["tasks:fetch", "data:read"]}}


@pytest.fixture
def backend() -> Iterator[PasswordAuthBackend]:
    """Password mode with one ready admin (``admin``/``admin-pw1``) and a worker token."""

    original = api_module._auth_backend
    pw = PasswordAuthBackend(InMemoryCredentialStore(), machine_tokens=TokenAuthBackend(WORKER))
    pw.create_user(subject="admin", roles=["admin"], login="admin")
    user = pw.store.get_user("admin")
    assert user is not None
    pw.store.put_user(
        user.model_copy(update={"password_hash": hash_password("admin-pw1"), "must_change": False})
    )
    api_module._auth_backend = pw
    try:
        yield pw
    finally:
        api_module._auth_backend = original


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _fresh_login(pw: PasswordAuthBackend, login: str, roles: list[str]) -> tuple[str, str]:
    """Provision ``login`` like an admin does and log in with its initial password.

    :returns: ``(session token, initial password)``; the session still has the
        password change pending.
    """

    admin = pw.login("admin", "admin-pw1").token
    resp = client.post("/users", json={"login": login, "roles": roles}, headers=_bearer(admin))
    assert resp.status_code == 201, resp.json()
    initial = resp.json()["initial_password"]
    login_resp = client.post("/auth/login", json={"login": login, "password": initial})
    assert login_resp.status_code == 200 and login_resp.json()["must_change"] is True
    return login_resp.json()["token"], initial


def _assert_change_required(resp_status: int, body: object) -> None:
    """The refusal names the pending password change, not a missing role."""

    assert resp_status == 403, body
    assert isinstance(body, dict)
    detail = body["detail"]
    assert isinstance(detail, dict), detail
    assert detail["code"] == "PW.change-required"


def test_pending_change_blocks_everything_but_the_auth_endpoints(
    backend: PasswordAuthBackend,
) -> None:
    token, _ = _fresh_login(backend, "neu.admin", ["admin"])
    headers = _bearer(token)
    schemas_before = client.get(
        "/schemas", headers=_bearer(backend.login("admin", "admin-pw1").token)
    ).json()

    for method, path, body in [
        ("GET", "/me/tasks", None),
        ("GET", "/instances", None),
        ("GET", "/users", None),
        ("GET", "/schemas", None),
        ("POST", "/schemas", {"name": "Darf nicht entstehen"}),
        ("GET", "/v1/connectors", None),
        ("GET", "/agents/a1/absences", None),
    ]:
        resp = client.request(method, path, json=body, headers=headers)
        _assert_change_required(resp.status_code, resp.json())

    # Nothing was created behind the refusal.
    after = client.get(
        "/schemas", headers=_bearer(backend.login("admin", "admin-pw1").token)
    ).json()
    assert after == schemas_before


def test_me_reports_the_pending_change(backend: PasswordAuthBackend) -> None:
    token, _ = _fresh_login(backend, "erika", ["operator"])
    me = client.get("/auth/me", headers=_bearer(token))
    assert me.status_code == 200
    assert me.json()["subject"] == "erika"
    assert me.json()["must_change"] is True


def test_change_password_unlocks_the_same_session(backend: PasswordAuthBackend) -> None:
    token, initial = _fresh_login(backend, "erika", ["operator"])
    headers = _bearer(token)
    changed = client.post(
        "/auth/change-password",
        json={"current_password": initial, "new_password": "eigenes-pw-1"},
        headers=headers,
    )
    assert changed.status_code == 204
    assert client.get("/me/tasks", headers=headers).status_code == 200
    assert client.get("/auth/me", headers=headers).json()["must_change"] is False
    fresh = client.post("/auth/login", json={"login": "erika", "password": "eigenes-pw-1"})
    assert client.get("/me/tasks", headers=_bearer(fresh.json()["token"])).status_code == 200


def test_a_refused_change_keeps_the_lock(backend: PasswordAuthBackend) -> None:
    """Edge case: a change rejected by the policy does not lift the lock."""

    token, initial = _fresh_login(backend, "erika", ["operator"])
    headers = _bearer(token)
    refused = client.post(
        "/auth/change-password",
        json={"current_password": initial, "new_password": initial},
        headers=headers,
    )
    assert refused.status_code == 400 and refused.json()["detail"]["code"] == "PW.unchanged"
    resp = client.get("/me/tasks", headers=headers)
    _assert_change_required(resp.status_code, resp.json())


def test_logout_stays_reachable(backend: PasswordAuthBackend) -> None:
    token, _ = _fresh_login(backend, "erika", ["operator"])
    assert client.post("/auth/logout", headers=_bearer(token)).status_code == 204
    assert client.get("/auth/me", headers=_bearer(token)).status_code == 401


def test_admin_reset_sets_the_lock_again(backend: PasswordAuthBackend) -> None:
    token, initial = _fresh_login(backend, "erika", ["operator"])
    client.post(
        "/auth/change-password",
        json={"current_password": initial, "new_password": "eigenes-pw-1"},
        headers=_bearer(token),
    )
    admin = backend.login("admin", "admin-pw1").token
    reset = client.post("/users/erika/reset-password", headers=_bearer(admin))
    assert reset.status_code == 200
    again = client.post(
        "/auth/login", json={"login": "erika", "password": reset.json()["initial_password"]}
    ).json()["token"]
    resp = client.get("/me/tasks", headers=_bearer(again))
    _assert_change_required(resp.status_code, resp.json())


def test_start_admin_must_change_before_administering(backend: PasswordAuthBackend) -> None:
    """The start administrator of a fresh store is a forced-change login, too."""

    fresh = PasswordAuthBackend(InMemoryCredentialStore())
    fresh._put_initial_admin("start", "start-pw-1", None)
    api_module._auth_backend = fresh
    token = fresh.login("start", "start-pw-1").token
    resp = client.get("/users", headers=_bearer(token))
    _assert_change_required(resp.status_code, resp.json())


def test_machine_tokens_and_ready_logins_are_not_affected(
    backend: PasswordAuthBackend,
) -> None:
    worker = _bearer("worker-geheim")
    assert client.get("/v1/connectors", headers=worker).status_code == 200
    me = client.get("/auth/me", headers=worker).json()
    assert me["must_change"] is False
    admin = _bearer(backend.login("admin", "admin-pw1").token)
    assert client.get("/users", headers=admin).status_code == 200


def test_token_mode_principals_never_carry_the_flag() -> None:
    principal = TokenAuthBackend(WORKER).authenticate("Bearer worker-geheim")
    assert principal.must_change is False


# --- web client -------------------------------------------------------------

# A reload with a stored session whose password change is still pending: the
# client must show the forced change dialog and must not load the app (every
# further call would only collect refusals).
_RELOAD_PENDING = r"""
state.token = "sitzung";
respond((p) => {
  if (p === "/health") return { body: { status: "ok", version: "x" } };
  if (p === "/auth/config") return { body: { mode: "password", password_login: true } };
  if (p === "/auth/me") {
    return { body: { subject: "erika", roles: ["operator"], must_change: __PENDING__ } };
  }
  const detail = { code: "PW.change-required", message: "x", params: {} };
  return { status: 403, body: { detail } };
});
await boot();
return {
  calls: calls.slice(),
  overlay: textOf(byId("auth-overlay")),
  toasts: textOf(byId("toast-root")),
};
"""


@needs_node
def test_reload_with_pending_change_shows_the_change_dialog(tmp_path: Path) -> None:
    snap = run_app_js(_RELOAD_PENDING.replace("__PENDING__", "true"), tmp_path)
    assert "Bitte vergeben Sie ein eigenes Passwort." in snap["overlay"]
    assert snap["calls"] == ["GET /health", "GET /auth/config", "GET /auth/me"], snap["calls"]
    assert snap["toasts"] == ""


@needs_node
def test_reload_without_pending_change_loads_the_app(tmp_path: Path) -> None:
    """Counterpart: without a pending change the app starts as before."""

    snap = run_app_js(_RELOAD_PENDING.replace("__PENDING__", "false"), tmp_path)
    assert "eigenes Passwort" not in snap["overlay"]
    # The app loads its schema selection (one request for all schemas).
    assert any(c.endswith("/schema-summaries") for c in snap["calls"]), snap["calls"]


def test_client_words_the_change_refusal() -> None:
    """The refusal code has a German text in the one message catalogue."""

    assert '"PW.change-required":' in app_js_source()
