# SPDX-License-Identifier: BUSL-1.1
"""Slow outside work never holds up the other writers of the in-memory API.

With the stores in memory the units of work of the one API process run one
after another (``api._write_serialization``): a unit cannot be rolled back, so
its repetition after a conflict must never see another unit half done. Every
mutating endpoint ran *entirely* under that lock -- also the ones whose work is
mostly outside the stores:

* a connection check or sample read against an unreachable connector waited
  for its network timeout (measured 75 s), and every login and every model
  change of the instance waited with it;
* the key derivation of a login (and of a new or reset password) ran under the
  lock, so simultaneous logins came in one after another instead of in
  parallel.

Pinned here: such work runs outside the lock (an endpoint that stores nothing
carries ``manages_own_units``; one that stores after the slow part runs only
that part as a unit, ``api._run_as_unit``), and the store part re-checks what
the slow part relied on -- a password changed in between is not accepted, a
deleted login is not brought back.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks import auth_password, outbox
from procworks.api import app
from procworks.auth import AuthError
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
    new_initial_password,
    verify_password,
)

client = TestClient(app)

#: How long the stand-in for slow outside work waits at most. A writer that is
#: held up by it answers after about this long; one that is not, at once.
_HOLD_SECONDS = 3.0

#: The answer time a writer must stay below while the slow work is under way.
_PROMPT_SECONDS = 1.0


class _Hold:
    """Stand-in for slow outside work: reports its arrival, then waits.

    ``wait`` is called from inside the endpoint under test (patched over the
    network call or the key derivation); the test starts its writer once
    ``entered`` is set and lets the slow work end with ``release``.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self) -> None:
        """Signal arrival and wait for the release (at most :data:`_HOLD_SECONDS`)."""

        self.entered.set()
        self.release.wait(_HOLD_SECONDS)


def _writer_time_beside(
    slow_request: Callable[[], httpx.Response],
    hold: _Hold,
    writer: Callable[[], httpx.Response],
) -> tuple[float, httpx.Response, httpx.Response]:
    """Run ``writer`` while ``slow_request`` waits in ``hold``.

    :param slow_request: the request whose endpoint reaches ``hold.wait``.
    :param hold: the hold patched into that endpoint.
    :param writer: a quick mutating request.
    :returns: the writer's answer time in seconds, its response and the slow
        request's response.
    """

    with ThreadPoolExecutor(max_workers=1) as pool:
        slow = pool.submit(slow_request)
        try:
            assert hold.entered.wait(5), "the slow request never reached its outside work"
            started = time.monotonic()
            written = writer()
            elapsed = time.monotonic() - started
        finally:
            hold.release.set()
        return elapsed, written, slow.result(timeout=10)


def _new_schema() -> httpx.Response:
    return client.post("/schemas", json={"name": "Neben der Prüfung"})


# --- endpoints that only talk to the outside -----------------------------------


@pytest.fixture
def connector(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Hold]:
    """A connector ``tot`` whose check and sample read hang like an unreachable host."""

    hold = _Hold()

    def hang(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        hold.wait()
        return [{"id": "K1"}]

    monkeypatch.setattr(api_module, "_require_connector", lambda _cid: None)
    monkeypatch.setattr(api_module, "_check_sample_entity", lambda _cid, _entity: None)
    monkeypatch.setattr(api_module._connections, "test", hang)
    monkeypatch.setattr(api_module._connections, "sample_read", hang)
    yield hold


@pytest.mark.parametrize(
    "call",
    [
        lambda: client.post("/v1/connectors/tot/test"),
        lambda: client.post("/v1/connectors/tot/sample-read", json={"entity": "Kunde"}),
    ],
    ids=["test", "sample-read"],
)
def test_a_hanging_connector_does_not_hold_up_a_model_change(
    connector: _Hold, call: Callable[[], httpx.Response]
) -> None:
    elapsed, written, slow = _writer_time_beside(call, connector, _new_schema)

    assert written.status_code == 201, written.text
    assert slow.status_code == 200, slow.text
    assert elapsed < _PROMPT_SECONDS, (
        f"POST /schemas waited {elapsed:.2f} s for the connector check (write lock held)"
    )


def test_a_slow_webhook_ping_does_not_hold_up_a_model_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = _Hold()

    def ping(subscription_id: str) -> Any:
        hold.wait()
        raise outbox.WebhookError(f"subscription '{subscription_id}' not found", 404)

    monkeypatch.setattr(api_module._outbox, "test_delivery", ping)

    elapsed, written, slow = _writer_time_beside(
        lambda: client.post("/v1/webhooks/wh_x/test"), hold, _new_schema
    )

    assert written.status_code == 201
    assert slow.status_code == 404
    assert elapsed < _PROMPT_SECONDS, f"POST /schemas waited {elapsed:.2f} s for a ping"


@pytest.mark.parametrize("path", ["/v1/webhooks/preview", "/v1/webhooks"])
def test_a_slow_name_lookup_of_a_webhook_does_not_hold_up_a_model_change(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Preview and subscription resolve the target name -- outside the lock."""

    hold = _Hold()
    lookup = outbox._lookup

    def slow_lookup(*args: Any, **kwargs: Any) -> Any:
        hold.wait()
        return lookup(*args, **kwargs)

    monkeypatch.setattr(outbox, "_lookup", slow_lookup)
    body = {"url": "https://hooks.example.com/pw", "events": ["task.completed"]}

    elapsed, written, slow = _writer_time_beside(
        lambda: client.post(path, json=body), hold, _new_schema
    )

    assert written.status_code == 201
    assert slow.status_code in (200, 201), slow.text
    assert elapsed < _PROMPT_SECONDS, f"POST /schemas waited {elapsed:.2f} s for a lookup"
    if path == "/v1/webhooks":
        created = slow.json()
        assert created["url"] == body["url"]  # the subscription is still stored
        api_module._outbox.unsubscribe(created["id"])


def test_a_refused_webhook_target_is_still_refused_with_its_reason() -> None:
    """Checking the target before the unit keeps the refusal and stores nothing."""

    before = len(api_module._outbox.list_subscriptions())
    resp = client.post(
        "/v1/webhooks", json={"url": "http://127.0.0.1:9/pw", "events": ["task.completed"]}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"].startswith("WH.")
    assert len(api_module._outbox.list_subscriptions()) == before


def test_unknown_webhook_events_are_refused_before_any_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    looked_up: list[object] = []
    monkeypatch.setattr(outbox, "_lookup", lambda *a, **k: looked_up.append(a))

    resp = client.post(
        "/v1/webhooks", json={"url": "https://hooks.example.com/pw", "events": ["nope"]}
    )

    assert resp.status_code == 422
    assert "unknown webhook events" in resp.json()["detail"]["message"]
    assert looked_up == []


# --- password mode: the key derivation ------------------------------------------


@pytest.fixture
def password_mode() -> Iterator[tuple[PasswordAuthBackend, dict[str, str]]]:
    """Password mode with a ready admin; yields the backend and its auth header."""

    original = api_module._auth_backend
    pw = PasswordAuthBackend(InMemoryCredentialStore())
    pw.create_user(subject="admin", roles=["admin"], login="admin")
    user = pw.store.get_user("admin")
    assert user is not None
    pw.store.put_user(
        user.model_copy(update={"password_hash": hash_password("admin-pw1"), "must_change": False})
    )
    api_module._auth_backend = pw
    api_module._login_throttle.success("admin")
    try:
        token = pw.login("admin", "admin-pw1").token
        yield pw, {"Authorization": f"Bearer {token}"}
    finally:
        api_module._auth_backend = original


def _schema_as(headers: dict[str, str]) -> Callable[[], httpx.Response]:
    return lambda: client.post("/schemas", json={"name": "Neben der Anmeldung"}, headers=headers)


def test_a_login_does_not_hold_up_writers_during_its_key_derivation(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pw, admin = password_mode
    hold = _Hold()

    def slow_verify(password: str, encoded: str) -> bool:
        hold.wait()
        return verify_password(password, encoded)

    monkeypatch.setattr(auth_password, "verify_password", slow_verify)

    elapsed, written, slow = _writer_time_beside(
        lambda: client.post("/auth/login", json={"login": "admin", "password": "admin-pw1"}),
        hold,
        _schema_as(admin),
    )

    assert written.status_code == 201, written.text
    assert slow.status_code == 200 and slow.json()["token"]
    assert elapsed < _PROMPT_SECONDS, f"POST /schemas waited {elapsed:.2f} s for a login"


def test_simultaneous_logins_derive_their_keys_in_parallel(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two logins meet inside the key derivation -- impossible one after another."""

    met = threading.Barrier(2, timeout=_HOLD_SECONDS)
    outcomes: list[bool] = []

    def meeting_verify(password: str, encoded: str) -> bool:
        try:
            met.wait()
            outcomes.append(True)
        except threading.BrokenBarrierError:
            outcomes.append(False)
        return verify_password(password, encoded)

    monkeypatch.setattr(auth_password, "verify_password", meeting_verify)

    def login() -> httpx.Response:
        return client.post("/auth/login", json={"login": "admin", "password": "admin-pw1"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = [f.result(timeout=10) for f in [pool.submit(login), pool.submit(login)]]

    assert [a.status_code for a in answers] == [200, 200]
    assert outcomes == [True, True], "the logins derived their keys one after another"


@pytest.mark.parametrize(
    "call",
    [
        lambda h: client.post("/users", json={"login": "neu", "roles": ["operator"]}, headers=h),
        lambda h: client.post("/users/admin/reset-password", headers=h),
        lambda h: client.post(
            "/auth/change-password",
            json={"current_password": "admin-pw1", "new_password": "admin-pw2-lang"},
            headers=h,
        ),
    ],
    ids=["create-user", "reset-password", "change-password"],
)
def test_a_new_password_hash_does_not_hold_up_writers(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[dict[str, str]], httpx.Response],
) -> None:
    pw, admin = password_mode
    second = {"Authorization": f"Bearer {pw.login('admin', 'admin-pw1').token}"}
    hold = _Hold()

    def slow_hash(password: str) -> str:
        hold.wait()
        return hash_password(password)

    monkeypatch.setattr(auth_password, "hash_password", slow_hash)

    elapsed, written, slow = _writer_time_beside(lambda: call(admin), hold, _schema_as(second))

    assert written.status_code == 201, written.text
    assert slow.status_code in (200, 201, 204), slow.text
    assert elapsed < _PROMPT_SECONDS, f"POST /schemas waited {elapsed:.2f} s for a hash"


# --- the store part re-checks what the slow part relied on ---------------------


def _ready_user(pw: PasswordAuthBackend, login: str, password: str) -> None:
    pw.create_user(subject=login, roles=["operator"], login=login)
    user = pw.store.get_user(login)
    assert user is not None
    pw.store.put_user(
        user.model_copy(update={"password_hash": hash_password(password), "must_change": False})
    )


def test_a_session_is_not_opened_for_a_password_changed_meanwhile() -> None:
    pw = PasswordAuthBackend(InMemoryCredentialStore())
    _ready_user(pw, "ute", "altes-passwort")
    verified = pw.verify_login("ute", "altes-passwort")

    pw.reset_password("ute")  # an admin resets the password in between

    with pytest.raises(AuthError) as refused:
        pw.open_session(verified)
    assert refused.value.message == "invalid credentials"


def test_a_session_is_not_opened_for_a_login_deleted_meanwhile() -> None:
    pw = PasswordAuthBackend(InMemoryCredentialStore())
    _ready_user(pw, "ute", "altes-passwort")
    verified = pw.verify_login("ute", "altes-passwort")

    pw.store.delete_user("ute")

    with pytest.raises(AuthError) as refused:
        pw.open_session(verified)
    assert refused.value.message == "invalid credentials"


def test_verify_then_open_is_a_login() -> None:
    pw = PasswordAuthBackend(InMemoryCredentialStore())
    _ready_user(pw, "ute", "altes-passwort")

    result = pw.open_session(pw.verify_login("ute", "altes-passwort"))

    assert pw.authenticate(f"Bearer {result.token}").subject == "ute"


def test_an_unknown_login_costs_exactly_one_key_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Still no timing oracle: unknown and known logins verify once each."""

    pw = PasswordAuthBackend(InMemoryCredentialStore())
    _ready_user(pw, "ute", "altes-passwort")
    calls: list[str] = []

    def counting(password: str, encoded: str) -> bool:
        calls.append(password)
        return verify_password(password, encoded)

    monkeypatch.setattr(auth_password, "verify_password", counting)
    for login in ("ute", "niemand"):
        with pytest.raises(AuthError) as refused:
            pw.verify_login(login, "falsch")
        assert refused.value.message == "invalid credentials"

    assert calls == ["falsch", "falsch"]


def test_a_password_change_is_refused_when_the_password_changed_meanwhile() -> None:
    pw = PasswordAuthBackend(InMemoryCredentialStore())
    _ready_user(pw, "ute", "altes-passwort")
    change = pw.prepare_password_change("ute", "altes-passwort", "neues-passwort")

    pw.reset_password("ute")

    with pytest.raises(AuthError) as refused:
        pw.apply_password_change(change)
    assert refused.value.message == "invalid credentials"
    user = pw.store.get_user("ute")
    assert user is not None and user.must_change  # the reset stands


def test_a_password_change_does_not_bring_back_a_deleted_login() -> None:
    pw = PasswordAuthBackend(InMemoryCredentialStore())
    _ready_user(pw, "ute", "altes-passwort")
    change = pw.prepare_password_change("ute", "altes-passwort", "neues-passwort")

    pw.store.delete_user("ute")

    with pytest.raises(AuthError):
        pw.apply_password_change(change)
    assert pw.store.get_user("ute") is None


def test_a_reset_does_not_bring_back_a_deleted_login() -> None:
    pw = PasswordAuthBackend(InMemoryCredentialStore())
    _ready_user(pw, "ute", "altes-passwort")
    initial = new_initial_password()  # derived before the unit, as the API does

    pw.store.delete_user("ute")

    with pytest.raises(KeyError):
        pw.reset_password("ute", initial=initial)
    assert pw.store.get_user("ute") is None


def test_a_prepared_initial_password_is_the_one_stored() -> None:
    pw = PasswordAuthBackend(InMemoryCredentialStore())
    initial = new_initial_password()

    user, clear = pw.create_user(subject="neu", roles=["operator"], login="neu", initial=initial)

    assert clear == initial.clear
    assert verify_password(clear, user.password_hash)
    assert user.must_change


def test_the_api_still_answers_a_wrong_current_password_with_its_code(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]],
) -> None:
    _pw, admin = password_mode
    resp = client.post(
        "/auth/change-password",
        json={"current_password": "falsch", "new_password": "admin-pw2-lang"},
        headers=admin,
    )

    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "AUTH.wrong-current-password"


def test_the_api_login_still_counts_failures_and_refuses_uniformly(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]],
) -> None:
    resp = client.post("/auth/login", json={"login": "admin", "password": "falsch"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid credentials"
    api_module._login_throttle.success("admin")



# --- a request refused anyway costs no key derivation --------------------------


@pytest.fixture
def hashes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every key derivation of a new password, recorded."""

    made: list[str] = []

    def counting(password: str) -> str:
        made.append(password)
        return hash_password(password)

    monkeypatch.setattr(auth_password, "hash_password", counting)
    return made


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"agent_id": "niemand-bekannt", "roles": ["operator"]}, "USERS.unknown-agent"),
        ({"login": "admin", "roles": ["operator"]}, "USERS.login-taken"),
        ({"login": "ohne-rolle", "roles": []}, "USERS.no-role"),
        ({"login": "falsche-rolle", "roles": ["chef"]}, "USERS.unknown-role"),
    ],
    ids=["unknown-agent", "login-taken", "no-role", "unknown-role"],
)
def test_a_refused_creation_derives_no_key(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]],
    hashes: list[str],
    body: dict[str, object],
    code: str,
) -> None:
    _pw, admin = password_mode

    resp = client.post("/users", json=body, headers=admin)

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == code
    assert hashes == []


def test_a_reset_of_an_unknown_login_derives_no_key(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]], hashes: list[str]
) -> None:
    _pw, admin = password_mode

    resp = client.post("/users/niemand/reset-password", headers=admin)

    assert resp.status_code == 404, resp.text
    assert hashes == []


def test_an_accepted_creation_and_reset_derive_one_key_each(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]], hashes: list[str]
) -> None:
    _pw, admin = password_mode

    made = client.post("/users", json={"login": "neu", "roles": ["operator"]}, headers=admin)
    assert made.status_code == 201, made.text
    assert len(hashes) == 1
    reset = client.post("/users/neu/reset-password", headers=admin)
    assert reset.status_code == 200, reset.text
    assert len(hashes) == 2


def test_a_login_taken_during_the_key_derivation_is_still_refused(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The early check does not replace the one in the store part."""

    pw, admin = password_mode
    original = api_module.new_initial_password

    def meanwhile() -> Any:
        pw.create_user(subject="zugleich", roles=["operator"], login="zugleich")
        return original()

    monkeypatch.setattr(api_module, "new_initial_password", meanwhile)

    resp = client.post("/users", json={"login": "zugleich", "roles": ["operator"]}, headers=admin)

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "USERS.login-taken"


def test_a_login_deleted_during_the_key_derivation_of_its_reset_stays_deleted(
    password_mode: tuple[PasswordAuthBackend, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pw, admin = password_mode
    _ready_user(pw, "geht", "altes-passwort")
    original = api_module.new_initial_password

    def meanwhile() -> Any:
        pw.store.delete_user("geht")
        return original()

    monkeypatch.setattr(api_module, "new_initial_password", meanwhile)

    resp = client.post("/users/geht/reset-password", headers=admin)

    assert resp.status_code == 404, resp.text
    assert pw.store.get_user("geht") is None
