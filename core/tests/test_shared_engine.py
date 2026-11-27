# SPDX-License-Identifier: BUSL-1.1
"""One database engine per process and database, with a configurable pool.

Every SQLAlchemy store (schemas, instances, audit log, logins, sessions, id
reservations, outboxes, licences …) used to build its own engine, each with
SQLAlchemy's default pool of 5 connections plus 10 overflow. Thirteen stores
in one API process could therefore open close to 200 PostgreSQL connections:
under parallel load, or with a second API process, the database's
``max_connections`` (100 by default) ran out and requests failed with 500
("too many clients already").

Now:

* all stores of a process that point at the same database URL share **one**
  engine and thus one pool (:func:`procworks.db.shared_engine`), built once
  and thread-safe;
* the pool of a server database is sized from ``PROCWORKS_DB_POOL_SIZE``,
  ``PROCWORKS_DB_MAX_OVERFLOW`` and ``PROCWORKS_DB_POOL_TIMEOUT``; an invalid
  value aborts the start with a message naming the variable;
* an in-memory SQLite URL names no shared database, so every store keeps its
  own engine there (as before);
* a pool that stays exhausted ends a request with 503 ``DB.unavailable``
  after the configured wait, not with 500.
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

import procworks.api as api_module
import procworks.db as db_module
from procworks.audit import create_audit_log
from procworks.auth_password import create_credential_store, create_session_store
from procworks.db import (
    SqlAlchemySchemaStore,
    SqlAlchemyTemplateStore,
    dispose_shared_engines,
    engine_options,
    shared_engine,
)
from procworks.licensing import create_license_store
from procworks.store import (
    create_absence_store,
    create_external_task_store,
    create_id_claim_store,
    create_instance_store,
    create_mail_outbox_store,
    create_org_store,
    create_store,
    create_template_store,
    create_webhook_store,
)

_PG_URL = "postgresql+psycopg://process:process@postgres:5432/procworks"

_POOL_VARS = (
    "PROCWORKS_DB_POOL_SIZE",
    "PROCWORKS_DB_MAX_OVERFLOW",
    "PROCWORKS_DB_POOL_TIMEOUT",
)


@pytest.fixture(autouse=True)
def _no_pool_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from the defaults, whatever the shell exports."""

    for name in _POOL_VARS:
        monkeypatch.delenv(name, raising=False)


def _count_engines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the URL of every engine :mod:`procworks.db` builds from now on."""

    built: list[str] = []
    original = db_module.create_engine

    def counting(url: str, **kwargs: Any) -> Engine:
        built.append(str(url))
        return original(url, **kwargs)

    monkeypatch.setattr(db_module, "create_engine", counting)
    return built


# --- one engine per database ---------------------------------------------------


def test_all_stores_of_a_process_share_one_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = f"sqlite:///{tmp_path / 'one.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    built = _count_engines(monkeypatch)

    stores = [
        create_store(),
        create_instance_store(),
        create_id_claim_store(),
        create_org_store(),
        create_external_task_store(),
        create_webhook_store(),
        create_mail_outbox_store(),
        create_absence_store(),
        create_template_store(),
        create_audit_log(),
        create_credential_store(),
        create_session_store(),
        create_license_store(),
    ]

    engines = {id(store._engine) for store in stores}
    assert len(stores) == 13
    assert engines == {id(shared_engine(url))}
    assert built == [url]


def test_a_second_database_gets_its_own_engine(tmp_path: Path) -> None:
    first = SqlAlchemySchemaStore(f"sqlite:///{tmp_path / 'a.db'}", create_tables=True)
    second = SqlAlchemySchemaStore(f"sqlite:///{tmp_path / 'b.db'}", create_tables=True)
    again = SqlAlchemyTemplateStore(f"sqlite:///{tmp_path / 'a.db'}", create_tables=True)

    assert first._engine is not second._engine
    assert first._engine is again._engine


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:"])
def test_an_in_memory_database_is_never_shared(url: str) -> None:
    """Each in-memory engine *is* its own database: sharing it would merge
    stores that were separate before."""

    first = SqlAlchemySchemaStore(url, create_tables=True)
    second = SqlAlchemySchemaStore(url, create_tables=True)

    assert first._engine is not second._engine


def test_concurrent_first_use_builds_a_single_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = f"sqlite:///{tmp_path / 'race.db'}"
    built: list[str] = []
    original = db_module.create_engine

    def slow(url_: str, **kwargs: Any) -> Engine:
        built.append(url_)
        time.sleep(0.05)  # widen the window between "not there" and "stored"
        return original(url_, **kwargs)

    monkeypatch.setattr(db_module, "create_engine", slow)
    start = threading.Barrier(8)
    seen: list[Engine] = []

    def worker() -> None:
        start.wait()
        seen.append(shared_engine(url))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert built == [url]
    assert len({id(engine) for engine in seen}) == 1


def test_an_engine_no_store_uses_is_released(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The register holds engines weakly: a test suite or a tool that builds
    stores for many short-lived databases does not keep their pools open."""

    url = f"sqlite:///{tmp_path / 'gone.db'}"
    built = _count_engines(monkeypatch)
    store = SqlAlchemySchemaStore(url, create_tables=True)
    del store
    gc.collect()

    SqlAlchemySchemaStore(url, create_tables=True)

    assert built == [url, url]


def test_dispose_closes_the_pooled_connections(tmp_path: Path) -> None:
    store = SqlAlchemySchemaStore(f"sqlite:///{tmp_path / 'd.db'}", create_tables=True)
    store.list_ids()
    assert store._engine.pool.checkedin() >= 1

    dispose_shared_engines()

    assert store._engine.pool.checkedin() == 0
    assert store.list_ids() == []  # the engine stays usable after a dispose


# --- pool size from the environment --------------------------------------------


def test_server_database_pool_defaults() -> None:
    options = engine_options(_PG_URL)

    assert options["pool_size"] == 10
    assert options["max_overflow"] == 10
    assert options["pool_timeout"] == 30.0
    # the outage settings of the pool stay in place
    assert options["pool_pre_ping"] is True
    assert options["connect_args"]["connect_timeout"] == 5


def test_pool_size_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCWORKS_DB_POOL_SIZE", "3")
    monkeypatch.setenv("PROCWORKS_DB_MAX_OVERFLOW", "2")
    monkeypatch.setenv("PROCWORKS_DB_POOL_TIMEOUT", "7.5")

    engine = shared_engine(_PG_URL)
    try:
        assert engine.pool.size() == 3  # QueuePool API
        assert engine.pool._max_overflow == 2
        assert engine.pool._timeout == 7.5
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("size", "overflow"),
    [("1", "0"), (" 4 ", "0")],
)
def test_smallest_valid_pool_is_accepted(
    monkeypatch: pytest.MonkeyPatch, size: str, overflow: str
) -> None:
    monkeypatch.setenv("PROCWORKS_DB_POOL_SIZE", size)
    monkeypatch.setenv("PROCWORKS_DB_MAX_OVERFLOW", overflow)

    options = engine_options(_PG_URL)

    assert options["pool_size"] == int(size)
    assert options["max_overflow"] == 0


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        ("PROCWORKS_DB_POOL_SIZE", "0", "whole number from 1 to 100"),
        ("PROCWORKS_DB_POOL_SIZE", "-1", "whole number from 1 to 100"),
        ("PROCWORKS_DB_POOL_SIZE", "zehn", "whole number"),
        ("PROCWORKS_DB_POOL_SIZE", "2.5", "whole number"),
        ("PROCWORKS_DB_MAX_OVERFLOW", "-1", "whole number from 0 to 100"),
        ("PROCWORKS_DB_MAX_OVERFLOW", "viel", "whole number"),
        ("PROCWORKS_DB_POOL_TIMEOUT", "0", "seconds from 1 to 300"),
        ("PROCWORKS_DB_POOL_TIMEOUT", "-3", "seconds from 1 to 300"),
        ("PROCWORKS_DB_POOL_TIMEOUT", "nan", "seconds from 1 to 300"),
        ("PROCWORKS_DB_POOL_TIMEOUT", "inf", "seconds from 1 to 300"),
        ("PROCWORKS_DB_POOL_TIMEOUT", "bald", "number of seconds"),
    ],
)
def test_an_invalid_pool_setting_aborts_with_the_variable_named(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, reason: str
) -> None:
    """``0`` is no harmless edge: SQLAlchemy reads a pool size of 0 as
    *unlimited*, which is exactly what the setting is there to prevent."""

    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name) as exc:
        engine_options(_PG_URL)
    assert reason in str(exc.value)
    assert repr(value) in str(exc.value)


@pytest.mark.parametrize("value", ["", "  "])
def test_an_empty_setting_means_the_default(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Compose hands an unset ``${VAR:-}`` over as the empty string."""

    for name in _POOL_VARS:
        monkeypatch.setenv(name, value)

    options = engine_options(_PG_URL)

    assert (options["pool_size"], options["max_overflow"], options["pool_timeout"]) == (
        10,
        10,
        30.0,
    )


def test_sqlite_ignores_the_pool_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite has no connection limit; an in-memory engine's pool would not
    even accept the arguments. So the settings (valid or not) leave it alone."""

    monkeypatch.setenv("PROCWORKS_DB_POOL_SIZE", "0")

    assert engine_options("sqlite:///x.db") == {"pool_pre_ping": True}
    assert engine_options("sqlite://") == {"pool_pre_ping": True}


# --- an exhausted pool ---------------------------------------------------------


def test_an_exhausted_pool_answers_503_after_the_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With every connection taken, a request waits ``pool_timeout`` and then
    ends as 503 ``DB.unavailable`` -- the client may retry, nothing crashed."""

    url = f"sqlite:///{tmp_path / 'full.db'}"
    original = db_module.create_engine

    def tiny_pool(url_: str, **kwargs: Any) -> Engine:
        return original(url_, pool_size=1, max_overflow=0, pool_timeout=0.2, **kwargs)

    monkeypatch.setattr(db_module, "create_engine", tiny_pool)
    store = SqlAlchemySchemaStore(url, create_tables=True)
    monkeypatch.setattr(api_module, "_store", store)
    client = TestClient(api_module.app, raise_server_exceptions=False)

    held = store._engine.connect()
    try:
        started = time.monotonic()
        resp = client.get("/schemas")
        waited = time.monotonic() - started
    finally:
        held.close()

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "DB.unavailable"
    assert waited < 5
    assert client.get("/schemas").status_code == 200  # free again


# --- the real API process ------------------------------------------------------


_PROBE = """
import json, sys
import sqlalchemy
original = sqlalchemy.create_engine
built = []
def counting(url, **kwargs):
    built.append(str(url))
    return original(url, **kwargs)
sqlalchemy.create_engine = counting
import procworks.api as api
print(json.dumps({
    "built": built,
    "engines": [str(e.url) for e in api._database_engines()],
}))
"""


def test_the_api_process_opens_one_engine_for_its_database(tmp_path: Path) -> None:
    """Import the API in a fresh interpreter, as a server process does, with
    password logins (so the login and session stores exist as well)."""

    url = f"sqlite:///{tmp_path / 'api.db'}"
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "PROCWORKS_AUTH": "password",
        "PROCWORKS_ADMIN_PASSWORD": "Start-Passwort-1234",
        "PYTHONPATH": str(Path(db_module.__file__).parents[1]),
    }
    for name in _POOL_VARS:
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["built"] == [url]
    assert report["engines"] == [url]


def test_the_api_closes_its_pools_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(db_module, "dispose_shared_engines", lambda: calls.append("dispose"))

    with TestClient(api_module.app) as client:
        assert client.get("/health").status_code == 200
        assert calls == []

    assert calls == ["dispose"]
