# SPDX-License-Identifier: BUSL-1.1
"""Two admins deleting each other at the same moment never leave no admin.

``DELETE /users/{login}`` refuses to delete the last admin (409
``USERS.last-admin``). The check and the deletion ran under a process lock --
but on a database the deletion only becomes visible with the commit of the
request, and that came after the lock was released. A second request read
the deleted admin as still present and deleted the other one: both answered
204, no admin was left, and nobody could manage logins through the
application any more. The wipe of ``POST /admin/reset`` had the same gap.

Now the store's admin-set lock (``lock_admin_changes``) is held by the
database until the request commits; the second request waits, then sees the
first one's deletion. Pinned on SQLite (a real database with transactions) by
a pause between the deletion and the commit -- exactly the window the race
needs -- and on the in-memory stores (unchanged).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend

import procworks.api as api_module
from procworks import db, unit_of_work
from procworks.api import app
from procworks.auth import Principal
from procworks.auth_password import InMemoryCredentialStore, PasswordAuthBackend

client = TestClient(app)

#: How long a request pauses after its deletion, before its commit. Long enough
#: that the other request certainly reads within the window.
_COMMIT_DELAY = 0.3


def _acting_admin(request: Request) -> Principal:
    """The caller is the admin named in the ``X-Test-As`` header."""

    return Principal(subject=request.headers["x-test-as"], roles=frozenset({"admin"}))


@pytest.fixture(params=["memory", "sqlite"])
def admins(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[PasswordAuthBackend]:
    """Password mode with exactly two admins, ``adm-a`` and ``adm-b``.

    Each request pauses between its deletion and the end of its unit (the
    session end that follows the deletion), so a second request can read in
    between -- the window in which the race lost the last admin.
    """

    if request.param == "sqlite":
        url = use_sql_backend(monkeypatch, tmp_path)
        store: Any = db.SqlAlchemyCredentialStore(url, create_tables=True)
    else:
        store = InMemoryCredentialStore()
    backend = PasswordAuthBackend(store)
    for login in ("adm-a", "adm-b"):
        backend.create_user(subject=login, roles=["admin"], login=login)
    original_revoke = backend.revoke_sessions

    def slow_revoke(login: str) -> None:
        original_revoke(login)
        time.sleep(_COMMIT_DELAY)  # after the deletion, before the commit

    monkeypatch.setattr(backend, "revoke_sessions", slow_revoke)
    monkeypatch.setattr(api_module, "_auth_backend", backend)
    app.dependency_overrides[api_module.get_principal] = _acting_admin
    try:
        yield backend
    finally:
        app.dependency_overrides.pop(api_module.get_principal, None)


def _admins_left(backend: PasswordAuthBackend) -> list[str]:
    return sorted(u.login for u in backend.store.list_users() if "admin" in u.roles)


def _delete(actor: str, login: str) -> Callable[[], httpx.Response]:
    return lambda: client.delete(f"/users/{login}", headers={"X-Test-As": actor})


def _race(*calls: Callable[[], Any]) -> list[Any]:
    with ThreadPoolExecutor(len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result() for future in futures]


def test_two_admins_deleting_each_other_leave_one(admins: PasswordAuthBackend) -> None:
    first, second = _race(_delete("adm-a", "adm-b"), _delete("adm-b", "adm-a"))

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [204, 409], (first.text, second.text)
    refused = first if first.status_code == 409 else second
    assert refused.json()["detail"]["code"] == "USERS.last-admin"
    assert len(_admins_left(admins)) == 1


def test_a_wipe_and_the_deletion_of_the_acting_admin_leave_an_admin(
    admins: PasswordAuthBackend,
) -> None:
    """The reset keeps its acting admin; deleting that admin meanwhile must not
    let the wipe remove the only other one."""

    def wipe_then_seed() -> None:
        api_module._wipe_users({"adm-a"})
        time.sleep(_COMMIT_DELAY)  # the reset goes on (seeding) before it commits

    def wipe() -> None:
        unit_of_work.run(wipe_then_seed, serialize=api_module._write_serialization())

    def delete_after_the_wipe_read() -> httpx.Response:
        time.sleep(_COMMIT_DELAY / 3)
        return _delete("adm-b", "adm-a")()

    _wiped, deleted = _race(wipe, delete_after_the_wipe_read)

    assert deleted.status_code in (204, 409), deleted.text
    assert _admins_left(admins) != []


def test_deleting_one_of_two_admins_alone_still_succeeds(admins: PasswordAuthBackend) -> None:
    """Positive case: the lock changes nothing for a single deletion."""

    resp = _delete("adm-a", "adm-b")()

    assert resp.status_code == 204, resp.text
    assert _admins_left(admins) == ["adm-a"]


def test_the_last_admin_is_refused_with_its_reason(admins: PasswordAuthBackend) -> None:
    assert _delete("adm-a", "adm-b")().status_code == 204

    resp = _delete("adm-b", "adm-a")()

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "USERS.last-admin"
    assert _admins_left(admins) == ["adm-a"]


def test_postgresql_takes_a_transaction_scoped_advisory_lock() -> None:
    """Across API processes only the database can exclude: the lock is
    ``pg_advisory_xact_lock`` (released by commit/rollback), never a session
    lock that would outlive the request."""

    from sqlalchemy.dialects import postgresql

    statement = db._admin_lock_statement("postgresql")
    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]  # Executable is compilable
    assert "pg_advisory_xact_lock" in sql
    assert "pg_advisory_lock(" not in sql


def test_the_lock_waits_for_the_holder_on_sqlite(tmp_path: Path) -> None:
    """Boundary: a second transaction waits on the lock until the first ends."""

    url = f"sqlite:///{tmp_path / 'locks.db'}"
    store = db.SqlAlchemyCredentialStore(url, create_tables=True)
    held = threading.Event()
    release = threading.Event()
    waited: list[float] = []

    def holder() -> None:
        def change() -> None:
            store.lock_admin_changes()
            held.set()
            release.wait(5)

        unit_of_work.run(change)

    def waiter() -> None:
        held.wait(5)
        started = time.monotonic()
        unit_of_work.run(store.lock_admin_changes)
        waited.append(time.monotonic() - started)

    first = threading.Thread(target=holder)
    second = threading.Thread(target=waiter)
    first.start()
    second.start()
    held.wait(5)
    time.sleep(0.3)
    release.set()
    first.join(10)
    second.join(10)

    assert waited and waited[0] >= 0.25
