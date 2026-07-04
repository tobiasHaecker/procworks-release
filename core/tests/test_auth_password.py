# SPDX-License-Identifier: BUSL-1.1
"""Password-login tests (CredentialStore + PasswordAuthBackend + API).

Covers the self-contained login flow from ``docs/Auth-Konzept.md`` section 11:

* scrypt hashing round-trips and rejects tampering,
* ``suggest_login`` transliterates umlauts and de-duplicates collisions,
* the in-memory credential store performs basic CRUD,
* the backend issues opaque session tokens, validates/expires/revokes them,
* self-service password change enforces the policy and clears the change flag,
* admin provisioning derives a login + one-off initial password, and
* the ``/auth/*`` and ``/users`` endpoints wire all of this together.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks.api import app
from procworks.auth import AuthError
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    PasswordPolicyError,
    User,
    hash_password,
    suggest_login,
    verify_password,
)

client = TestClient(app)


# --- hashing --------------------------------------------------------------


def test_hash_verify_roundtrip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("scrypt$")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_hash_is_salted() -> None:
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_tampered_hash() -> None:
    encoded = hash_password("secret-pw")
    tampered = encoded[:-1] + ("0" if encoded[-1] != "0" else "1")
    assert not verify_password("secret-pw", tampered)


def test_verify_rejects_garbage() -> None:
    assert not verify_password("x", "not-a-valid-hash")


# --- suggest_login --------------------------------------------------------


def test_suggest_login_basic() -> None:
    assert suggest_login("Erika Musterfrau") == "erika.musterfrau"


def test_suggest_login_transliterates_umlauts() -> None:
    assert suggest_login("Jörg Bäcker") == "joerg.baecker"


def test_suggest_login_collision_suffix() -> None:
    existing = ["erika.musterfrau", "erika.musterfrau2"]
    assert suggest_login("Erika Musterfrau", existing) == "erika.musterfrau3"


def test_suggest_login_empty_falls_back() -> None:
    assert suggest_login("!!!") == "user"


# --- in-memory store ------------------------------------------------------


def test_store_crud() -> None:
    store = InMemoryCredentialStore()
    user = User(login="a.b", password_hash=hash_password("pw"), subject="a.b")
    store.put_user(user)
    assert store.get_user("a.b") is not None
    assert [u.login for u in store.list_users()] == ["a.b"]
    store.delete_user("a.b")
    assert store.get_user("a.b") is None


# --- backend session lifecycle -------------------------------------------


def _backend_with_user() -> tuple[PasswordAuthBackend, str]:
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    _user, initial = backend.create_user(
        subject="erika", roles=["operator"], display_name="Erika Musterfrau"
    )
    return backend, initial


def test_login_issues_token_and_authenticate_round_trips() -> None:
    backend, initial = _backend_with_user()
    result = backend.login("erika.musterfrau", initial)
    assert result.must_change is True
    principal = backend.authenticate(f"Bearer {result.token}")
    assert principal.subject == "erika.musterfrau"
    assert principal.roles == frozenset({"operator"})


def test_login_wrong_password_raises() -> None:
    backend, _initial = _backend_with_user()
    with pytest.raises(AuthError):
        backend.login("erika.musterfrau", "nope")


def test_login_unknown_user_raises() -> None:
    backend, _initial = _backend_with_user()
    with pytest.raises(AuthError):
        backend.login("ghost", "whatever")


def test_authenticate_missing_token_raises() -> None:
    backend, _initial = _backend_with_user()
    with pytest.raises(AuthError):
        backend.authenticate(None)


def test_authenticate_invalid_token_raises() -> None:
    backend, _initial = _backend_with_user()
    with pytest.raises(AuthError):
        backend.authenticate("Bearer made-up")


def test_session_expiry() -> None:
    backend = PasswordAuthBackend(
        InMemoryCredentialStore(), session_ttl=timedelta(seconds=-1)
    )
    _user, initial = backend.create_user(subject="x", roles=["viewer"])
    result = backend.login(_user.login, initial)
    with pytest.raises(AuthError):
        backend.authenticate(f"Bearer {result.token}")


def test_logout_revokes_session() -> None:
    backend, initial = _backend_with_user()
    result = backend.login("erika.musterfrau", initial)
    backend.logout(f"Bearer {result.token}")
    with pytest.raises(AuthError):
        backend.authenticate(f"Bearer {result.token}")


# --- change password ------------------------------------------------------


def test_change_password_clears_flag() -> None:
    backend, initial = _backend_with_user()
    backend.change_password("erika.musterfrau", initial, "brandneu123")
    user = backend.store.get_user("erika.musterfrau")
    assert user is not None
    assert user.must_change is False
    assert verify_password("brandneu123", user.password_hash)


def test_change_password_wrong_current_raises() -> None:
    backend, _initial = _backend_with_user()
    with pytest.raises(AuthError):
        backend.change_password("erika.musterfrau", "bogus", "brandneu123")


def test_change_password_too_short_raises() -> None:
    backend, initial = _backend_with_user()
    with pytest.raises(PasswordPolicyError):
        backend.change_password("erika.musterfrau", initial, "short")


def test_change_password_same_as_current_raises() -> None:
    backend, initial = _backend_with_user()
    with pytest.raises(PasswordPolicyError):
        backend.change_password("erika.musterfrau", initial, initial)


# --- provisioning ---------------------------------------------------------


def test_create_user_suggests_login_and_must_change() -> None:
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    user, initial = backend.create_user(
        subject="erika", roles=["operator"], display_name="Erika Musterfrau"
    )
    assert user.login == "erika.musterfrau"
    assert user.must_change is True
    assert initial
    assert verify_password(initial, user.password_hash)


def test_create_user_unknown_role_raises() -> None:
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    with pytest.raises(PasswordPolicyError):
        backend.create_user(subject="x", roles=["wizard"])


def test_create_user_duplicate_login_raises() -> None:
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    backend.create_user(subject="x", roles=["viewer"], login="taken")
    with pytest.raises(PasswordPolicyError):
        backend.create_user(subject="y", roles=["viewer"], login="taken")


def test_reset_password_forces_change() -> None:
    backend, _initial = _backend_with_user()
    backend.change_password("erika.musterfrau", _initial, "settled123")
    new_initial = backend.reset_password("erika.musterfrau")
    user = backend.store.get_user("erika.musterfrau")
    assert user is not None
    assert user.must_change is True
    assert verify_password(new_initial, user.password_hash)


def test_reset_password_unknown_user_raises() -> None:
    backend, _initial = _backend_with_user()
    with pytest.raises(KeyError):
        backend.reset_password("ghost")


# --- bootstrap admin ------------------------------------------------------


def test_bootstrap_admin_seeds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCWORKS_ADMIN_LOGIN", "root")
    monkeypatch.setenv("PROCWORKS_ADMIN_PASSWORD", "rootpass1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    backend = PasswordAuthBackend.from_env()
    user = backend.store.get_user("root")
    assert user is not None
    assert "admin" in user.roles
    assert user.must_change is True
    assert backend.login("root", "rootpass1").must_change is True


def test_bootstrap_admin_autoseeds_default_without_env(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("PROCWORKS_ADMIN_LOGIN", raising=False)
    monkeypatch.delenv("PROCWORKS_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with caplog.at_level("WARNING", logger="procworks.auth"):
        backend = PasswordAuthBackend.from_env()
    admin = backend.store.get_user("admin")
    assert admin is not None
    assert "admin" in admin.roles
    assert admin.must_change is True
    # The one-off password is written to the server log so the operator can read
    # it; it is never stored or returned in clear text otherwise.
    assert any("Initial admin account created" in r.message for r in caplog.records)


def test_bootstrap_admin_does_not_reseed_existing_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROCWORKS_ADMIN_LOGIN", raising=False)
    monkeypatch.delenv("PROCWORKS_ADMIN_PASSWORD", raising=False)
    store = InMemoryCredentialStore()
    store.put_user(
        User(
            login="ops",
            password_hash=hash_password("pw"),
            subject="ops",
            roles=frozenset({"operator"}),
        )
    )
    backend = PasswordAuthBackend(store)
    backend._bootstrap_admin()
    # A non-empty store is left untouched: no resurrected default admin.
    assert backend.store.get_user("admin") is None
    assert [u.login for u in backend.store.list_users()] == ["ops"]


# --- API integration ------------------------------------------------------


@pytest.fixture
def password_mode() -> Iterator[PasswordAuthBackend]:
    """Swap the module's auth backend to a fresh password backend."""

    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    backend.create_user(
        subject="ada", roles=["admin"], login="admin", display_name="Ada Admin"
    )
    api_module._auth_backend = backend
    try:
        yield backend
    finally:
        api_module._auth_backend = original


def _admin_login(backend: PasswordAuthBackend) -> str:
    # The fixture's admin must change first; do it directly, then log in.
    user = backend.store.get_user("admin")
    assert user is not None
    backend.store.put_user(
        user.model_copy(update={"password_hash": hash_password("admin-pw1"),
                                "must_change": False})
    )
    return backend.login("admin", "admin-pw1").token


def test_api_config_reports_password_mode(password_mode: PasswordAuthBackend) -> None:
    cfg = client.get("/auth/config").json()
    assert cfg["mode"] == "password"
    assert cfg["password_login"] is True


def test_api_me_requires_token(password_mode: PasswordAuthBackend) -> None:
    assert client.get("/auth/me").status_code == 401


def test_api_login_and_me(password_mode: PasswordAuthBackend) -> None:
    token = _admin_login(password_mode)
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["subject"] == "admin"
    assert me["roles"] == ["admin"]


def test_api_login_bad_credentials(password_mode: PasswordAuthBackend) -> None:
    resp = client.post("/auth/login", json={"login": "admin", "password": "x"})
    assert resp.status_code == 401


def test_api_protected_endpoint_requires_auth(
    password_mode: PasswordAuthBackend,
) -> None:
    assert client.get("/schemas").status_code == 401
    token = _admin_login(password_mode)
    resp = client.get("/schemas", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_api_admin_provisions_user_and_new_user_logs_in(
    password_mode: PasswordAuthBackend,
) -> None:
    token = _admin_login(password_mode)
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/users",
        json={"roles": ["operator"], "display_name": "Erika Musterfrau"},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["login"] == "erika.musterfrau"
    initial = body["initial_password"]
    assert body["user"]["must_change"] is True

    login = client.post(
        "/auth/login", json={"login": "erika.musterfrau", "password": initial}
    )
    assert login.status_code == 200
    assert login.json()["must_change"] is True


def test_api_change_password_flow(password_mode: PasswordAuthBackend) -> None:
    token = _admin_login(password_mode)
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/users",
        json={"roles": ["viewer"], "display_name": "Tom Tester"},
        headers=headers,
    )
    initial = resp.json()["initial_password"]
    user_token = client.post(
        "/auth/login", json={"login": "tom.tester", "password": initial}
    ).json()["token"]
    user_headers = {"Authorization": f"Bearer {user_token}"}

    changed = client.post(
        "/auth/change-password",
        json={"current_password": initial, "new_password": "freshpw123"},
        headers=user_headers,
    )
    assert changed.status_code == 204
    assert client.post(
        "/auth/login", json={"login": "tom.tester", "password": "freshpw123"}
    ).json()["must_change"] is False


def test_api_change_password_policy(password_mode: PasswordAuthBackend) -> None:
    token = _admin_login(password_mode)
    resp = client.post(
        "/auth/change-password",
        json={"current_password": "admin-pw1", "new_password": "short"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_api_reset_password(password_mode: PasswordAuthBackend) -> None:
    token = _admin_login(password_mode)
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        "/users",
        json={"roles": ["viewer"], "login": "resetme"},
        headers=headers,
    )
    resp = client.post("/users/resetme/reset-password", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["initial_password"]


def test_api_reset_password_unknown(password_mode: PasswordAuthBackend) -> None:
    token = _admin_login(password_mode)
    resp = client.post(
        "/users/ghost/reset-password",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_api_logout(password_mode: PasswordAuthBackend) -> None:
    token = _admin_login(password_mode)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_api_users_requires_admin(password_mode: PasswordAuthBackend) -> None:
    token = _admin_login(password_mode)
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        "/users",
        json={"roles": ["viewer"], "display_name": "Val Viewer"},
        headers=headers,
    )
    initial = client.post(
        "/users/val.viewer/reset-password", headers=headers
    ).json()["initial_password"]
    viewer_token = client.post(
        "/auth/login", json={"login": "val.viewer", "password": initial}
    ).json()["token"]
    resp = client.get(
        "/users", headers={"Authorization": f"Bearer {viewer_token}"}
    )
    assert resp.status_code == 403


def test_api_list_and_delete_user(password_mode: PasswordAuthBackend) -> None:
    token = _admin_login(password_mode)
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        "/users",
        json={"roles": ["viewer"], "login": "gone"},
        headers=headers,
    )
    logins = {u["login"] for u in client.get("/users", headers=headers).json()}
    assert {"admin", "gone"} <= logins
    assert client.delete("/users/gone", headers=headers).status_code == 204
    logins = {u["login"] for u in client.get("/users", headers=headers).json()}
    assert "gone" not in logins


# --- provisioning a login from an agent (Ressourcensicht "Login anlegen") -----


def _build_agent(headers: dict[str, str]) -> str:
    """Create a schema with one role + agent; return the schema id.

    Uses a unique agent id because the API tests share one in-memory store, so a
    common id like ``a1`` would collide with agents created by other tests and
    make ``_find_agent_name`` resolve the wrong name.
    """

    sid = client.post(
        "/schemas", json={"name": "Org"}, headers=headers
    ).json()["id"]
    client.post(
        f"/schemas/{sid}/roles",
        json={"name": "Sachbearbeiter", "role_id": "sb"},
        headers=headers,
    )
    client.post(
        f"/schemas/{sid}/agents",
        json={
            "name": "Erika Musterfrau",
            "role_ids": ["sb"],
            "agent_id": "login_prov_a1",
        },
        headers=headers,
    )
    return sid


def test_api_provision_login_from_agent_id_derives_name(
    password_mode: PasswordAuthBackend,
) -> None:
    # Mirrors the web client's "Login anlegen" with only the agent_id: the
    # server resolves the display name from the schema's org model.
    token = _admin_login(password_mode)
    headers = {"Authorization": f"Bearer {token}"}
    _build_agent(headers)
    resp = client.post(
        "/users",
        json={"agent_id": "login_prov_a1", "roles": ["operator"]},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["login"] == "erika.musterfrau"
    assert body["user"]["agent_id"] == "login_prov_a1"
    assert body["user"]["must_change"] is True


def test_api_provision_login_with_display_name(
    password_mode: PasswordAuthBackend,
) -> None:
    # The web client also passes display_name directly; the login is derived
    # from it and the agent stays linked via agent_id.
    token = _admin_login(password_mode)
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/users",
        json={
            "agent_id": "x9",
            "display_name": "J\u00F6rg B\u00E4cker",
            "roles": ["operator", "modeler"],
        },
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["login"] == "joerg.baecker"
    assert set(body["user"]["roles"]) == {"operator", "modeler"}
    # The provisioned person can log in with the one-off initial password.
    login = client.post(
        "/auth/login",
        json={"login": "joerg.baecker", "password": body["initial_password"]},
    )
    assert login.status_code == 200

