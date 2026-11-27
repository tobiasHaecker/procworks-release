# SPDX-License-Identifier: BUSL-1.1
"""A password reset ends every access to the login -- also when a login, a
password change or a deletion of the same login runs at the same moment.

The slow part of a login and of a password change (the key derivation) runs
outside the unit of work; only the store part is a unit and re-reads the
login to check that the password it verified is still the stored one. On a
database that re-read used to be a plain read: a reset that had stored its
new password but not yet committed was invisible to it. The login then
opened its session after the reset had already ended the sessions it could
see -- a session opened with the old password outlived the reset (and gained
full rights as soon as the person set their new password). A password change
in the same window either overwrote the reset completely or lost its
forced-change flag, because the database stores only the columns that
changed against the stale read.

Now every store part that decides on a login's password reads the login
under a lock that lasts until the unit commits (``CredentialStore.lock_user``):
the second of two such units waits for the first and then sees its result.
Pinned on SQLite (a real database with transactions) by a pause between the
write and the commit of the first unit -- the window the race needs -- and on
the in-memory stores, whose units run one after another anyway (unchanged).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sql_backed_api import use_sql_backend
from sqlalchemy.dialects import postgresql, sqlite

import procworks.api as api_module
from procworks import db
from procworks.auth import AuthError, Principal
from procworks.auth_password import (
    InMemoryCredentialStore,
    InMemorySessionStore,
    LoginResult,
    PasswordAuthBackend,
    hash_password,
    new_initial_password,
)

#: How long the first unit pauses after its writes, before its commit. Long
#: enough that the second unit certainly runs inside the window.
_HOLD = 0.4

_OLD = "altes-passwort"
_NEW = "neues-passwort"


@pytest.fixture(params=["memory", "sqlite"])
def backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[PasswordAuthBackend]:
    """Password backend with the operator login ``ute`` (password :data:`_OLD`).

    ``sqlite`` moves the API onto SQL stores (units then run in parallel, as on
    PostgreSQL) and keeps logins and sessions in the same database file.
    """

    if request.param == "sqlite":
        url = use_sql_backend(monkeypatch, tmp_path)
        store: Any = db.SqlAlchemyCredentialStore(url, create_tables=True)
        sessions: Any = db.SqlAlchemySessionStore(url, create_tables=True)
    else:
        store = InMemoryCredentialStore()
        sessions = InMemorySessionStore()
    pw = PasswordAuthBackend(store, sessions=sessions)
    pw.create_user(subject="ute", roles=["operator"], login="ute")
    _set_password(pw, _OLD, must_change=False)
    monkeypatch.setattr(api_module, "_auth_backend", pw)
    yield pw


def _set_password(pw: PasswordAuthBackend, password: str, *, must_change: bool) -> None:
    user = pw.store.get_user("ute")
    assert user is not None
    pw.store.put_user(
        user.model_copy(
            update={"password_hash": hash_password(password), "must_change": must_change}
        )
    )


def _unit[T](change: Callable[[], T]) -> T:
    """Run ``change`` as the store part of a request, exactly as the API does."""

    return api_module._run_as_unit(change)


def _first_holds_then_second(
    first: Callable[[], object], second: Callable[[], Any]
) -> Any:
    """Run ``first`` as a unit that pauses before its commit; meanwhile run ``second``.

    :param first: the writes of the first unit (it then holds for :data:`_HOLD`).
    :param second: started once ``first`` has written; runs as its own unit.
    :returns: what ``second`` returned, or the exception it raised.
    """

    written = threading.Event()
    errors: list[BaseException] = []

    def hold() -> None:
        first()
        written.set()
        time.sleep(_HOLD)  # written, not yet committed

    def run_first() -> None:
        try:
            _unit(hold)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
            written.set()

    thread = threading.Thread(target=run_first)
    thread.start()
    assert written.wait(5)
    try:
        outcome: Any = _unit(second)
    except AuthError as exc:
        outcome = exc
    thread.join(10)
    assert not errors, errors
    return outcome


# --- login beside a reset -----------------------------------------------------


def test_a_login_checked_before_a_reset_gets_no_session_after_it(
    backend: PasswordAuthBackend,
) -> None:
    """The reset has stored its new password but not committed; the login,
    whose password was checked against the old one, must not open a session."""

    verified = backend.verify_login("ute", _OLD)  # the slow part, done before
    initial = new_initial_password()

    outcome = _first_holds_then_second(
        lambda: backend.reset_password("ute", initial=initial),
        lambda: backend.open_session(verified),
    )

    assert isinstance(outcome, AuthError), "a session was opened with the old password"
    assert outcome.message == "invalid credentials"
    user = backend.store.get_user("ute")
    assert user is not None and user.must_change
    assert user.password_hash == initial.hashed


def test_a_session_opened_just_before_a_reset_is_ended_by_it(
    backend: PasswordAuthBackend,
) -> None:
    """Boundary: the login wins the race; the reset then ends its session."""

    verified = backend.verify_login("ute", _OLD)
    opened: list[LoginResult] = []

    outcome = _first_holds_then_second(
        lambda: opened.append(backend.open_session(verified)),
        lambda: backend.reset_password("ute"),
    )

    assert isinstance(outcome, str)  # the reset went through
    with pytest.raises(AuthError) as refused:
        backend.authenticate(f"Bearer {opened[0].token}")
    assert refused.value.message == "invalid session"


def test_the_new_password_of_a_reset_logs_in(backend: PasswordAuthBackend) -> None:
    """Positive: after the reset its initial password opens a session that
    must change the password; the lock changes nothing for a lone login."""

    initial = _unit(lambda: backend.reset_password("ute"))

    verified = backend.verify_login("ute", initial)
    result = _unit(lambda: backend.open_session(verified))

    assert result.must_change
    assert backend.authenticate(f"Bearer {result.token}").subject == "ute"


# --- password change beside a reset ---------------------------------------------


def test_a_change_committed_first_is_overwritten_completely_by_the_reset(
    backend: PasswordAuthBackend,
) -> None:
    """The reset reads the login after the change; it stores its password
    *and* the forced-change flag -- never the change's flag with its own hash."""

    _set_password(backend, _OLD, must_change=True)  # the person must change
    change = backend.prepare_password_change("ute", _OLD, _NEW)
    initial = new_initial_password()

    outcome = _first_holds_then_second(
        lambda: backend.apply_password_change(change),
        lambda: backend.reset_password("ute", initial=initial),
    )

    assert outcome == initial.clear
    user = backend.store.get_user("ute")
    assert user is not None
    assert user.password_hash == initial.hashed
    assert user.must_change, "the reset lost its forced-change flag"


def test_a_change_checked_before_a_reset_is_refused_after_it(
    backend: PasswordAuthBackend,
) -> None:
    """The change verified the old password; the reset stored first. The
    change must be refused for its reason and leave the reset standing."""

    change = backend.prepare_password_change("ute", _OLD, _NEW)
    initial = new_initial_password()

    outcome = _first_holds_then_second(
        lambda: backend.reset_password("ute", initial=initial),
        lambda: backend.apply_password_change(change),
    )

    assert isinstance(outcome, AuthError), "the change overwrote the reset"
    assert outcome.message == "invalid credentials"
    user = backend.store.get_user("ute")
    assert user is not None
    assert user.password_hash == initial.hashed and user.must_change


# --- login beside a deletion ------------------------------------------------------


def test_a_login_checked_before_a_deletion_gets_no_session(
    backend: PasswordAuthBackend,
) -> None:
    """A deleted login answers no login, not even with a session that the next
    request would refuse."""

    verified = backend.verify_login("ute", _OLD)

    admin = Principal(subject="adm", roles=frozenset({"admin"}))

    def delete() -> None:  # the unit of DELETE /users/ute
        api_module._delete_user_checked(backend, "ute", admin)
        backend.revoke_sessions("ute")

    outcome = _first_holds_then_second(delete, lambda: backend.open_session(verified))

    assert isinstance(outcome, AuthError), "a deleted login got a session"
    assert outcome.message == "invalid credentials"
    assert backend.store.get_user("ute") is None


# --- the lock itself ----------------------------------------------------------------


def test_postgresql_locks_the_login_row_until_the_transaction_ends() -> None:
    """Across API processes only the database can exclude: on PostgreSQL the
    lock is ``SELECT ... FOR UPDATE`` of the one login row (released by commit
    or rollback), not a lock of the whole table."""

    statements = db._user_lock_statements("postgresql", "ute")

    sql = [str(s.compile(dialect=postgresql.dialect())) for s in statements]  # type: ignore[attr-defined]  # Executable is compilable
    assert len(sql) == 1
    assert "FOR UPDATE" in sql[0] and "auth_user.login = " in sql[0]


def test_sqlite_takes_its_write_lock_before_reading_the_login() -> None:
    """SQLite knows no row lock and ignores ``FOR UPDATE``: an ``UPDATE`` of
    the row comes first and takes the write lock until the transaction ends."""

    statements = db._user_lock_statements("sqlite", "ute")

    sql = [str(s.compile(dialect=sqlite.dialect())) for s in statements]  # type: ignore[attr-defined]  # Executable is compilable
    assert [s.split()[0] for s in sql] == ["UPDATE", "SELECT"]


def test_locking_an_unknown_login_finds_nobody(backend: PasswordAuthBackend) -> None:
    """Boundary: an unknown login is ``None`` under the lock as without it."""

    assert _unit(lambda: backend.store.lock_user("niemand")) is None
    locked = _unit(lambda: backend.store.lock_user("ute"))
    assert locked is not None and locked.login == "ute"
