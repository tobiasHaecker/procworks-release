# SPDX-License-Identifier: BUSL-1.1
"""A database outage answers 503 with a code, and ``/ready`` reports it.

When PostgreSQL went away, reads answered 500 with a stack trace in the log,
writes broke the upstream connection (502 behind the reverse proxy), and
``/health`` kept saying "ok". After the database came back, connections the
pool still held were dead, and requests kept failing for minutes.

Now:

* every request that fails because the database cannot be reached answers
  ``503`` with ``detail.code`` ``DB.unavailable`` and a ``Retry-After``
  header -- never 500, never the driver's text with host and port;
* the pool checks a connection before handing it out (``pool_pre_ping``), so
  the first request after the database is back already succeeds, and a
  PostgreSQL connection attempt gives up after a few seconds;
* ``/ready`` pings every database the API uses (503 ``DB.unavailable``
  otherwise); ``/health`` stays a pure sign of life.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError

import procworks.api as api_module
from procworks.db import SqlAlchemySchemaStore, engine_options

client = TestClient(api_module.app, raise_server_exceptions=False)

_DRIVER_TEXT = 'connection to server at "db.internal" (10.0.0.5), port 5432 failed'


def _down(*_args: object, **_kwargs: object) -> Any:
    raise OperationalError("SELECT 1", {}, Exception(_DRIVER_TEXT))


def _assert_unavailable(resp: Any) -> None:
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "DB.unavailable"
    assert "db.internal" not in resp.text and "5432" not in resp.text
    assert resp.headers["Retry-After"] == "5"


# --- requests during an outage -------------------------------------------------


def test_a_read_during_an_outage_answers_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module._store, "list_ids", _down)

    _assert_unavailable(client.get("/schemas"))


def test_a_write_during_an_outage_answers_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module._store, "put", _down)

    _assert_unavailable(client.post("/schemas", json={"name": "Ausfall"}))


def test_a_lost_connection_answers_503_as_well(monkeypatch: pytest.MonkeyPatch) -> None:
    def lost(*_args: object, **_kwargs: object) -> Any:
        raise InterfaceError("SELECT 1", {}, Exception("connection already closed"))

    monkeypatch.setattr(api_module._store, "list_ids", lost)

    _assert_unavailable(client.get("/schemas"))


def test_other_database_errors_are_not_reported_as_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only "cannot reach the database" is a 503; a constraint violation is a
    defect and stays a 500, so it is not mistaken for a passing outage."""

    def conflict(*_args: object, **_kwargs: object) -> Any:
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    monkeypatch.setattr(api_module._store, "list_ids", conflict)

    assert client.get("/schemas").status_code == 500


# --- readiness ------------------------------------------------------------------


def test_health_stays_a_sign_of_life_and_ready_is_ready_without_a_database() -> None:
    assert client.get("/health").json()["status"] == "ok"
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_ready_pings_the_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SqlAlchemySchemaStore(f"sqlite:///{tmp_path / 'ok.db'}", create_tables=True)
    monkeypatch.setattr(api_module, "_store", store)

    assert client.get("/ready").status_code == 200


def test_ready_reports_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unreachable = SqlAlchemySchemaStore(f"sqlite:///{tmp_path / 'missing' / 'x.db'}")
    monkeypatch.setattr(api_module, "_instances", unreachable)

    resp = _assert_ready_down()
    assert client.get("/health").status_code == 200  # liveness unaffected
    assert resp.json()["detail"]["params"] == {}


def _assert_ready_down() -> Any:
    resp = client.get("/ready")
    _assert_unavailable(resp)
    return resp


# --- the pool ------------------------------------------------------------------


def test_the_pool_checks_connections_before_use(tmp_path: Path) -> None:
    store = SqlAlchemySchemaStore(f"sqlite:///{tmp_path / 'p.db'}", create_tables=True)

    assert store._engine.pool._pre_ping is True


def test_a_postgres_connection_attempt_gives_up_quickly() -> None:
    options = engine_options("postgresql+psycopg://process:process@postgres:5432/procworks")

    assert options["pool_pre_ping"] is True
    assert options["connect_args"]["connect_timeout"] == 5


def test_sqlite_gets_no_postgres_options() -> None:
    assert engine_options("sqlite:///x.db") == {"pool_pre_ping": True}
