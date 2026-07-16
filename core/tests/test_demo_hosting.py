# SPDX-License-Identifier: BUSL-1.1
"""Tests for the Demo-Hosting boot conveniences (D0a boot seed, D0b SPA mount).

Both are additive boundary features (docs/Demo-Hosting-Konzept.md) that must
default to *off* and touch no correctness rule. See :func:`procworks.api._lifespan`
and :func:`procworks.api._maybe_mount_web`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import procworks.api as api
from procworks.api import _env_truthy, _maybe_mount_web, app
from procworks.auth_password import InMemoryCredentialStore, PasswordAuthBackend
from procworks.demo import DEMO_AUTOLOGIN, DEMO_PASSWORD, DEMO_USERS

#: Repo-root ``web/`` directory (core/tests -> core -> repo root -> web).
WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def _clear_stores() -> None:
    """Wipe the module singletons so the boot seed sees an empty system."""
    api._store.clear()
    api._instances.clear()
    api._org_store.clear()
    api._audit.clear()
    api._absence_store.clear()


# --- D0a: env parsing -------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_env_truthy_accepts_yes_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PROCWORKS_X", value)
    assert _env_truthy("PROCWORKS_X") is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_env_truthy_rejects_no_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PROCWORKS_X", value)
    assert _env_truthy("PROCWORKS_X") is False


def test_env_truthy_unset_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROCWORKS_X", raising=False)
    assert _env_truthy("PROCWORKS_X") is False


# --- D0a: boot seed via lifespan -------------------------------------------


def test_boot_seed_populates_empty_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the env switch set, entering the lifespan seeds the demo cosmos."""
    monkeypatch.setenv("PROCWORKS_LOAD_DEMO", "1")
    _clear_stores()
    assert _store_empty()

    # Entering the TestClient context manager runs the app lifespan (startup).
    with TestClient(app):
        pass

    schema_ids = api._store.list_ids()
    assert schema_ids, "boot seed should have loaded the demo schemas"
    # The shared demo org with its five agents must be present.
    org_ids = api._org_store.list_ids()
    assert org_ids
    org = api._org_store.get(org_ids[0])
    assert len(org.agents) == 5
    # And the seeded active absence (deputy substitution visible out of the box).
    assert api._absence_store.list_entries()

    _clear_stores()


def test_boot_seed_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second lifespan entry must not re-seed (or raise) on a non-empty store."""
    monkeypatch.setenv("PROCWORKS_LOAD_DEMO", "1")
    _clear_stores()

    with TestClient(app):
        pass
    count_after_first = len(api._store.list_ids())

    # Re-enter: the guard sees a populated store and skips seeding entirely,
    # so demo.load_demo (which assumes an empty system) is never called twice.
    with TestClient(app):
        pass
    assert len(api._store.list_ids()) == count_after_first

    _clear_stores()


def test_boot_seed_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the env switch the lifespan seeds nothing."""
    monkeypatch.delenv("PROCWORKS_LOAD_DEMO", raising=False)
    _clear_stores()

    with TestClient(app):
        pass

    assert _store_empty()


def _store_empty() -> bool:
    return not (api._store.list_ids() or api._org_store.list_ids() or api._instances.list_ids())


# --- D0b: static SPA mount --------------------------------------------------


def test_mount_web_serves_index_without_shadowing_api() -> None:
    """A mounted web dir serves index.html at / but never shadows API routes."""
    if not (WEB_DIR / "index.html").is_file():  # pragma: no cover - repo layout guard
        pytest.skip("web/ SPA not present in this checkout")

    probe = FastAPI()

    @probe.get("/ping")
    def _ping() -> dict[str, str]:
        return {"pong": "1"}

    assert _maybe_mount_web(probe, str(WEB_DIR)) is True

    with TestClient(probe) as c:
        # API route registered before the mount still wins (mount is last).
        assert c.get("/ping").json() == {"pong": "1"}
        # Root falls through to the static index.html.
        root = c.get("/")
        assert root.status_code == 200
        assert "<" in root.text  # served HTML, not JSON


def test_mount_web_noop_when_dir_missing() -> None:
    """An unset/invalid web dir mounts nothing (off by default)."""
    probe = FastAPI()
    assert _maybe_mount_web(probe, "") is False
    assert _maybe_mount_web(probe, "/definitely/not/a/real/dir/procworks") is False


def test_mount_web_installs_api_prefix_shim() -> None:
    """When the SPA is co-served, /api-prefixed calls reach the root-mounted API.

    The single-container demo SPA computes its API base as origin+"/api"; the API
    lives at root, so the shim must strip the prefix. Without it the co-served SPA
    would 404 on every call (the bug that left the demo visitor unable to log in).
    """
    if not (WEB_DIR / "index.html").is_file():  # pragma: no cover - repo layout guard
        pytest.skip("web/ SPA not present in this checkout")

    probe = FastAPI()

    @probe.get("/auth/config")
    def _cfg() -> dict[str, bool]:
        return {"ok": True}

    assert _maybe_mount_web(probe, str(WEB_DIR)) is True

    with TestClient(probe) as c:
        # Root path still works ...
        assert c.get("/auth/config").json() == {"ok": True}
        # ... and the SPA's /api-prefixed call reaches the very same route.
        assert c.get("/api/auth/config").json() == {"ok": True}


def test_api_prefix_shim_absent_without_web_mount() -> None:
    """No SPA co-served -> no shim: /api stays unknown (regular deployment)."""
    probe = FastAPI()

    @probe.get("/auth/config")
    def _cfg() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(probe) as c:
        assert c.get("/auth/config").json() == {"ok": True}
        assert c.get("/api/auth/config").status_code == 404


# --- Demo login: /auth/config advertises the seeded logins in demo mode -----


def _with_password_backend() -> PasswordAuthBackend:
    """Swap the module auth backend to a fresh password backend; caller restores."""
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    api._auth_backend = backend
    return backend


def test_auth_config_exposes_demo_logins_in_demo_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Password mode + PROCWORKS_DEMO_MODE -> /auth/config advertises the demo
    logins, their shared password and the auto-login target, so the SPA can log
    a fresh visitor in without guessing credentials."""
    monkeypatch.setenv("PROCWORKS_DEMO_MODE", "1")
    original = api._auth_backend
    _with_password_backend()
    try:
        cfg = TestClient(app).get("/auth/config").json()
    finally:
        api._auth_backend = original

    assert cfg["mode"] == "password"
    assert cfg["demo"] is True
    assert cfg["demo_password"] == DEMO_PASSWORD
    assert cfg["demo_autologin"] == DEMO_AUTOLOGIN
    logins = {u["login"] for u in cfg["demo_logins"]}
    assert {login for login, *_ in DEMO_USERS} == logins
    # The auto-login target must be one of the advertised logins and a modeler.
    autol0 = next(u for u in cfg["demo_logins"] if u["login"] == DEMO_AUTOLOGIN)
    assert autol0["role"] == "modeler"


def test_auth_config_hides_demo_fields_without_demo_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Password mode WITHOUT the demo switch must not leak any demo credentials."""
    monkeypatch.delenv("PROCWORKS_DEMO_MODE", raising=False)
    original = api._auth_backend
    _with_password_backend()
    try:
        cfg = TestClient(app).get("/auth/config").json()
    finally:
        api._auth_backend = original

    assert cfg["mode"] == "password"
    assert cfg["demo"] is False
    assert cfg["demo_password"] is None
    assert cfg["demo_autologin"] is None
    assert cfg["demo_logins"] == []


def test_auth_config_no_demo_fields_in_open_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the demo switch on, the *open* backend never advertises logins
    (there are none to advertise; demo login is a password-mode convenience)."""
    monkeypatch.setenv("PROCWORKS_DEMO_MODE", "1")
    # Default module backend is the open one (no swap).
    cfg = TestClient(app).get("/auth/config").json()
    assert cfg["mode"] != "password"
    assert cfg["demo"] is False
    assert cfg["demo_password"] is None
