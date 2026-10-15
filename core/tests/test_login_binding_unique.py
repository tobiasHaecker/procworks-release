# SPDX-License-Identifier: BUSL-1.1
"""One login per person -- also when two logins are created at the same moment.

The rule "a person has at most one login" was checked by reading all logins
and then storing the new one. Two creations at the same moment both read
"no login yet" and both stored -- the person had two logins (reproduced with
two and three parallel requests). Now:

* the database holds the rule itself: a unique index on the person binding
  of ``auth_user`` (Alembic ``0020``); the store adds a new login only
  through :meth:`add_user`, which refuses a taken person or login name
  (:class:`procworks.auth_password.LoginConflict`), in memory as in the
  database;
* the backend turns that into the usual answer ``USERS.agent-taken`` naming
  the login that won;
* bindings that are already doubled stay as they are -- nothing is deleted.
  The migration keeps the alphabetically first login of such a person under
  the index and marks the others (``legacy_duplicate``), which leaves them
  out of it -- so no further login can be added for that person either. The
  mark is a property of the row, never of a name: a login created later is
  never marked, even under a deleted legacy login's name, and no login name
  is written into the schema. Deleting the indexed login of such a person
  moves the protection onto one of the remaining ones. The API names those
  persons at start.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import procworks.api as api_module
from procworks.auth_password import (
    InMemoryCredentialStore,
    LoginConflict,
    PasswordAuthBackend,
    PasswordPolicyError,
    User,
    hash_password,
)
from procworks.db import SqlAlchemyCredentialStore

_HASH = hash_password("irrelevant-1")


def _user(login: str, agent_id: str | None) -> User:
    return User(login=login, password_hash=_HASH, subject=login, agent_id=agent_id,
                roles=frozenset({"operator"}))


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        return InMemoryCredentialStore()
    return SqlAlchemyCredentialStore(f"sqlite:///{tmp_path / 'users.db'}", create_tables=True)


# --- the store contract -------------------------------------------------------------


def test_adding_a_second_login_for_a_person_is_refused(store: Any) -> None:
    store.add_user(_user("paul.klein", "p-paul"))

    with pytest.raises(LoginConflict):
        store.add_user(_user("paul.zwei", "p-paul"))

    assert [u.login for u in store.list_users()] == ["paul.klein"]


def test_adding_a_taken_login_name_is_refused_and_keeps_the_stored_one(store: Any) -> None:
    store.add_user(_user("paul.klein", "p-paul"))

    with pytest.raises(LoginConflict):
        store.add_user(_user("paul.klein", None))

    assert store.get_user("paul.klein").agent_id == "p-paul"


def test_logins_without_a_person_are_not_limited(store: Any) -> None:
    store.add_user(_user("admin.eins", None))
    store.add_user(_user("admin.zwei", None))

    assert len(store.list_users()) == 2


def test_updating_a_login_keeps_working(store: Any) -> None:
    store.add_user(_user("paul.klein", "p-paul"))

    store.put_user(_user("paul.klein", "p-paul").model_copy(update={"must_change": False}))

    assert store.get_user("paul.klein").must_change is False


# --- two creations at once (two processes on one database) -----------------------------


def test_two_processes_creating_a_login_for_one_person_create_exactly_one(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'race.db'}"
    backends = [PasswordAuthBackend(SqlAlchemyCredentialStore(url, create_tables=True))
                for _ in range(2)]
    barrier = threading.Barrier(2, timeout=2)
    for backend in backends:  # both read "no login yet" before either stores
        reading = backend.store.list_users

        def gated(reading: Any = reading) -> Any:
            found = reading()
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return found

        backend.store.list_users = gated  # type: ignore[method-assign]
    results: list[Any] = []

    def create(backend: PasswordAuthBackend, login: str) -> None:
        try:
            results.append(backend.create_user(subject=login, roles=["operator"],
                                               agent_id="p-paul", login=login)[0].login)
        except PasswordPolicyError as exc:
            results.append(exc)

    threads = [threading.Thread(target=create, args=(b, f"paul.{n}"))
               for n, b in enumerate(backends)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    created = [r for r in results if isinstance(r, str)]
    refused = [r for r in results if isinstance(r, PasswordPolicyError)]
    assert len(created) == 1 and len(refused) == 1, results
    assert refused[0].code == "USERS.agent-taken"
    assert refused[0].params == {"agent": "p-paul", "login": created[0]}
    stored = SqlAlchemyCredentialStore(url).list_users()
    assert [u.login for u in stored] == created


# --- the migration ---------------------------------------------------------------------


def _alembic(url: str, *args: str) -> None:
    core = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(core / "src")}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=core, env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr


def _insert(url: str, login: str, agent: str | None) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO auth_user (login, subject, password_hash, agent_id, roles, "
                     "display_name, must_change) VALUES (:l, :l, 'x', :a, '[]', NULL, 1)"),
                {"l": login, "a": agent},
            )
    finally:
        engine.dispose()


def _logins(url: str) -> list[tuple[str, str | None]]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT login, agent_id FROM auth_user ORDER BY login"))
            return [(r[0], r[1]) for r in rows]
    finally:
        engine.dispose()


def _index_definition(url: str) -> str:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return str(conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE name = 'ux_auth_user_agent_id'"
            )).scalar_one())
    finally:
        engine.dispose()


def _migrated_legacy(tmp_path: Path) -> tuple[str, list[tuple[str, str | None]]]:
    """A 0019 database with doubled bindings, upgraded to the head."""

    pytest.importorskip("alembic")
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    _alembic(url, "upgrade", "0019_idempotency_key")
    for login, agent in [("p2.a", "p2"), ("p2.b", "p2"), ("p0.c", "p0"), ("p0.a", "p0"),
                         ("p0.b", "p0"), ("ein'zeln", "p1"), ("admin", None), ("mara", None)]:
        _insert(url, login, agent)
    before = _logins(url)
    _alembic(url, "upgrade", "0020_login_agent_unique")
    return url, before


def test_the_migration_keeps_doubled_bindings_and_protects_every_person(tmp_path: Path) -> None:
    url, before = _migrated_legacy(tmp_path)

    assert _logins(url) == before, "the migration changed or removed a login"
    for login, agent in [("p2.neu", "p2"), ("p0.neu", "p0"), ("p1.neu", "p1")]:
        with pytest.raises(IntegrityError):
            _insert(url, login, agent)
    _insert(url, "p3.eins", "p3")
    with pytest.raises(IntegrityError):
        _insert(url, "p3.zwei", "p3")
    _insert(url, "ohne.person", None)
    store = SqlAlchemyCredentialStore(url)
    assert store.duplicate_bindings() == {"p0": ["p0.a", "p0.b", "p0.c"], "p2": ["p2.a", "p2.b"]}

    _alembic(url, "downgrade", "0019_idempotency_key")

    engine = create_engine(url)
    try:
        names = {ix["name"] for ix in inspect(engine).get_indexes("auth_user")}
        columns = {c["name"] for c in inspect(engine).get_columns("auth_user")}
    finally:
        engine.dispose()
    assert "ux_auth_user_agent_id" not in names
    assert "legacy_duplicate" not in columns
    _insert(url, "p3.zwei", "p3")  # the older version had no such rule
    assert len(_logins(url)) == len(before) + 3


def test_no_login_name_is_written_into_the_schema(tmp_path: Path) -> None:
    url, _ = _migrated_legacy(tmp_path)
    definition = _index_definition(url)
    assert "legacy_duplicate" in definition
    for name in ("p0.b", "p0.c", "p2.b"):
        assert name not in definition


def test_a_new_login_under_a_deleted_legacy_name_is_protected(tmp_path: Path) -> None:
    """Delete an exempted login and create one with the same name: it is not exempt."""

    url, _ = _migrated_legacy(tmp_path)
    store = SqlAlchemyCredentialStore(url)
    store.delete_user("p0.b")

    store.add_user(_user("p0.b", "x"))  # same name, another person
    with pytest.raises(LoginConflict):
        store.add_user(_user("x.zwei", "x"))
    assert [u.login for u in store.list_users() if u.agent_id == "x"] == ["p0.b"]


def test_deleting_the_protected_login_moves_the_protection(tmp_path: Path) -> None:
    """Grenzfall: p0.a carried the index for p0 -- after its deletion p0.b does."""

    url, _ = _migrated_legacy(tmp_path)
    store = SqlAlchemyCredentialStore(url)
    store.delete_user("p0.a")

    with pytest.raises(LoginConflict):
        store.add_user(_user("p0.neu", "p0"))
    assert store.duplicate_bindings()["p0"] == ["p0.b", "p0.c"]


def test_rebinding_a_legacy_login_puts_it_under_the_index(tmp_path: Path) -> None:
    """A legacy login moved to another person is no exemption there."""

    url, _ = _migrated_legacy(tmp_path)
    store = SqlAlchemyCredentialStore(url)
    store.add_user(_user("y.eins", "y"))
    moved = store.get_user("p2.b")
    assert moved is not None
    with pytest.raises(IntegrityError):
        store.put_user(User(login=moved.login, password_hash=moved.password_hash,
                            subject=moved.subject, agent_id="y", roles=moved.roles))


def test_the_start_names_the_persons_with_more_than_one_login(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InMemoryCredentialStore()
    store.put_user(_user("p2.a", "p2"))
    store.put_user(_user("p2.b", "p2"))  # as an older version left it
    store.put_user(_user("p1", "p1"))
    monkeypatch.setattr(api_module, "_auth_backend", PasswordAuthBackend(store))

    with caplog.at_level(logging.WARNING, logger="procworks.api"):
        api_module._warn_duplicate_login_bindings()

    (record,) = [r for r in caplog.records if "more than one login" in r.getMessage()]
    assert "p2 (2 logins)" in record.getMessage()
    assert "p1 (" not in record.getMessage()


def test_a_database_with_the_earlier_name_index_downgrades_cleanly(tmp_path: Path) -> None:
    """Grenzfall: a database migrated by the earlier form of the revision (names
    in the index, no mark column) can go back to 0019 and up again."""

    pytest.importorskip("alembic")
    url = f"sqlite:///{tmp_path / 'old.db'}"
    _alembic(url, "upgrade", "0019_idempotency_key")
    for login, agent in [("p0.a", "p0"), ("p0.b", "p0")]:
        _insert(url, login, agent)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX ux_auth_user_agent_id ON auth_user (agent_id) "
                "WHERE agent_id IS NOT NULL AND login NOT IN ('p0.b')"
            ))
            conn.execute(text("UPDATE alembic_version SET version_num = '0020_login_agent_unique'"))
    finally:
        engine.dispose()

    _alembic(url, "downgrade", "0019_idempotency_key")
    _alembic(url, "upgrade", "0020_login_agent_unique")

    assert "p0.b" not in _index_definition(url)
    assert SqlAlchemyCredentialStore(url).duplicate_bindings() == {"p0": ["p0.a", "p0.b"]}
