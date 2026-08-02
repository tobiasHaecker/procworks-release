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
from procworks.demo import DEMO_AUTOLOGIN, DEMO_PASSWORD, DEMO_USERS, ORG_ID, SCHEMA_URLAUB
from procworks.demo_o2c import O2C_USERS, SCHEMA_MAIN
from procworks.demo_o2c import ORG_ID as O2C_ORG_ID
from procworks.demo_o2c import _build_org as o2c_org

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


def test_boot_seed_loads_both_cosmoses_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both switches set -> both data sets land. This is the shipped demo config.

    Guards the ``was_empty`` trap in :func:`procworks.api._lifespan`: the empty
    check must fall *once, before the first seed*, otherwise the second switch
    would see the store the first one just filled and never run. Since the public
    demo image now sets both, a regression here would silently ship a demo
    without its flagship data set.
    """
    monkeypatch.setenv("PROCWORKS_LOAD_DEMO", "1")
    monkeypatch.setenv("PROCWORKS_LOAD_O2C", "1")
    _clear_stores()

    with TestClient(app):
        pass

    schema_ids = set(api._store.list_ids())
    assert SCHEMA_URLAUB in schema_ids, "base demo missing"
    assert SCHEMA_MAIN in schema_ids, "Order-to-Cash main process missing"
    # Two independent organisations coexist (own agents, own logins).
    assert {ORG_ID, O2C_ORG_ID} <= set(api._org_store.list_ids())

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


def test_auth_config_exposes_feedback_url_in_demo_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PROCWORKS_DEMO_FEEDBACK_URL surfaces on /auth/config so the SPA can POST
    the post-demo survey to the broker; absent -> the SPA shows no survey."""
    monkeypatch.setenv("PROCWORKS_DEMO_MODE", "1")
    monkeypatch.setenv("PROCWORKS_DEMO_FEEDBACK_URL", "https://broker.example/feedback")
    original = api._auth_backend
    _with_password_backend()
    try:
        cfg = TestClient(app).get("/auth/config").json()
    finally:
        api._auth_backend = original

    assert cfg["demo"] is True
    assert cfg["demo_feedback_url"] == "https://broker.example/feedback"


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
    assert cfg["demo_feedback_url"] is None


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


# --- Demo login: the Order-to-Cash cosmos is advertised only when seeded -----


def _demo_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Fetch /auth/config through a fresh password backend in demo mode."""
    monkeypatch.setenv("PROCWORKS_DEMO_MODE", "1")
    original = api._auth_backend
    _with_password_backend()
    try:
        result: dict[str, object] = TestClient(app).get("/auth/config").json()
        return result
    finally:
        api._auth_backend = original


def test_auth_config_omits_o2c_logins_without_the_load_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``PROCWORKS_LOAD_O2C`` the Order-to-Cash logins are NOT advertised.

    They would not have been seeded, so offering them would hand a visitor
    credentials that cannot work. Advertising and seeding hang off one switch.
    """
    monkeypatch.delenv("PROCWORKS_LOAD_O2C", raising=False)
    cfg = _demo_config(monkeypatch)

    logins = {u["login"] for u in cfg["demo_logins"]}  # type: ignore[union-attr]
    assert {login for login, *_ in DEMO_USERS} == logins
    assert not logins.intersection({login for login, *_ in O2C_USERS})


def test_auth_config_adds_o2c_logins_when_that_cosmos_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``PROCWORKS_LOAD_O2C`` both cosmoses' logins are offered together.

    This is what a visitor of the public demo gets: the lean base processes *and*
    the whole Order-to-Cash value stream, switchable per role.
    """
    monkeypatch.setenv("PROCWORKS_LOAD_O2C", "1")
    cfg = _demo_config(monkeypatch)

    logins = {u["login"] for u in cfg["demo_logins"]}  # type: ignore[union-attr]
    assert {login for login, *_ in DEMO_USERS} <= logins
    assert {login for login, *_ in O2C_USERS} <= logins
    # The auto-login target stays the modeller of the base cosmos.
    assert cfg["demo_autologin"] == DEMO_AUTOLOGIN


def test_auth_config_labels_o2c_logins_with_their_business_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch box shows the role *in the process*, not the RBAC role.

    Every Order-to-Cash login is technically an ``operator``; labelling them that
    way would read "Bearbeiter" eight times and help nobody. The label therefore
    comes from the seeded organisation -- and the all-roles persona (Sina
    Springer, the one-login walkthrough) collapses to a short hint instead of
    listing six role names.
    """
    _clear_stores()
    api._org_store.put(o2c_org())
    monkeypatch.setenv("PROCWORKS_LOAD_O2C", "1")
    try:
        cfg = _demo_config(monkeypatch)
    finally:
        _clear_stores()

    by_login = {u["login"]: u["role"] for u in cfg["demo_logins"]}  # type: ignore[union-attr]
    assert by_login["bianca.buch"] == "Debitorenbuchhaltung"
    assert by_login["lars.lange"] == "Lager/Versand"       # exactly two roles -> joined
    assert by_login["sina.springer"] == "alle Rollen"      # six roles -> collapsed
    # The base cosmos keeps its RBAC labels (unchanged behaviour).
    assert by_login[DEMO_AUTOLOGIN] == "modeler"


def test_auth_config_falls_back_to_rbac_role_without_the_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No seeded organisation -> the label degrades to the RBAC role, never fails.

    The label is a convenience, not a correctness input: an unresolvable
    organisation must not break the public config endpoint.
    """
    _clear_stores()
    monkeypatch.setenv("PROCWORKS_LOAD_O2C", "1")
    cfg = _demo_config(monkeypatch)

    by_login = {u["login"]: u["role"] for u in cfg["demo_logins"]}  # type: ignore[union-attr]
    assert by_login["bianca.buch"] == "operator"


# --- The demo image must seed what /auth/config advertises -------------------


def test_demo_image_seeds_both_cosmoses() -> None:
    """Guard: the public demo image sets BOTH load switches.

    ``/auth/config`` advertises the Order-to-Cash logins whenever
    ``PROCWORKS_LOAD_O2C`` is set, so image and endpoint must agree -- otherwise a
    visitor is offered a role switch that cannot log in. Exactly this drifted once:
    the Order-to-Cash cosmos shipped in v1.10.0 while the demo image kept seeding
    only the base data, leaving the flagship data set unreachable in the very
    place prospects click (loading it needs the admin role, which the demo has no
    login for).
    """
    dockerfile = (
        Path(__file__).resolve().parents[2] / "deploy" / "demo" / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "PROCWORKS_LOAD_DEMO=1" in dockerfile
    assert "PROCWORKS_LOAD_O2C=1" in dockerfile
