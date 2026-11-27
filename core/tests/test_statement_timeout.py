# SPDX-License-Identifier: BUSL-1.1
"""A database that does not answer ends a request after a bounded time.

A statement that waits -- for a lock another session holds, or on an
overloaded server -- used to wait without limit: the request hung, and with
it the worker thread. ``/ready`` hung as well instead of reporting the
database. Now:

* every connection of the API's pool runs with ``statement_timeout``
  (``PROCWORKS_DB_STATEMENT_TIMEOUT`` seconds, default 30, ``0`` = off,
  parsed strictly); a statement over it is cancelled by the server, and the
  request answers ``503 DB.unavailable`` without the driver's text;
* ``/ready`` answers within :data:`procworks.api.READY_TIMEOUT_SECONDS` even
  when the ping does not return (a frozen server, which no server-side limit
  can stop), and does not pile up waiting threads.

Migrations (Alembic's own engine) and the backup/restore scripts (their own
``psql``/``pg_restore`` connections) do not use this pool and keep running
long statements.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import procworks.api as api_module
from procworks.db import DEFAULT_STATEMENT_TIMEOUT_SECONDS, engine_options

client = TestClient(api_module.app, raise_server_exceptions=False)

_PG_URL = "postgresql+psycopg://process:process@postgres:5432/procworks"


def test_postgres_connections_get_the_default_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROCWORKS_DB_STATEMENT_TIMEOUT", raising=False)

    options = engine_options(_PG_URL)

    assert DEFAULT_STATEMENT_TIMEOUT_SECONDS == 30
    assert options["connect_args"] == {
        "connect_timeout": 5,
        "options": "-c statement_timeout=30000",
    }


@pytest.mark.parametrize(("value", "millis"), [("5", 5000), ("2.5", 2500), (" 3600 ", 3600000)])
def test_the_statement_timeout_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, value: str, millis: int
) -> None:
    monkeypatch.setenv("PROCWORKS_DB_STATEMENT_TIMEOUT", value)

    options = engine_options(_PG_URL)

    assert options["connect_args"]["options"] == f"-c statement_timeout={millis}"


def test_zero_switches_the_statement_timeout_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCWORKS_DB_STATEMENT_TIMEOUT", "0")

    assert engine_options(_PG_URL)["connect_args"] == {"connect_timeout": 5}


@pytest.mark.parametrize(
    "value", ["1_0", "1e3", "-1", "nan", "inf", "0.5", "3601", "drei", "1,5", "0x10", "+5"]
)
def test_an_invalid_statement_timeout_aborts_with_the_variable_named(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PROCWORKS_DB_STATEMENT_TIMEOUT", value)

    with pytest.raises(ValueError, match="PROCWORKS_DB_STATEMENT_TIMEOUT") as exc:
        engine_options(_PG_URL)
    assert repr(value) in str(exc.value)
    assert "0 (off) or a number of seconds from 1 to 3600" in str(exc.value)


def test_sqlite_gets_no_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCWORKS_DB_STATEMENT_TIMEOUT", "5")

    assert engine_options("sqlite:///x.db") == {"pool_pre_ping": True}


class _CancelledByServer(Exception):
    """The driver error of a statement the server cancelled, with its SQLSTATE.

    Shaped like the driver's own error (``sqlstate`` ``57014``,
    ``query_canceled``) without needing the driver: the API decides by the
    SQLAlchemy error class and the SQLSTATE, never by the driver's type.
    """

    sqlstate = "57014"


def test_a_cancelled_statement_answers_503_without_the_driver_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cancelled(*_args: object, **_kwargs: object) -> Any:
        raise OperationalError(
            "UPDATE audit_chain SET head_seq=head_seq",
            {},
            _CancelledByServer("canceling statement due to statement timeout"),
        )

    monkeypatch.setattr(api_module._store, "put", cancelled)

    resp = client.post("/schemas", json={"name": "Zeitlimit"})

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "DB.unavailable"
    assert "statement timeout" not in resp.text and "audit_chain" not in resp.text


class _HangingEngine:
    """An engine whose connection never answers (a frozen database server)."""

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.calls = 0

    def connect(self) -> Any:
        self.calls += 1
        self._release.wait(10)
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))


def test_ready_answers_503_in_time_when_the_ping_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    engine = _HangingEngine(release)
    monkeypatch.setattr(api_module, "_database_engines", lambda: [engine])
    monkeypatch.setattr(api_module, "READY_TIMEOUT_SECONDS", 0.3)
    try:
        started = time.monotonic()
        first = client.get("/ready")
        second = client.get("/ready")  # the first ping still hangs
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert (first.status_code, second.status_code) == (503, 503)
    assert first.json()["detail"]["code"] == "DB.unavailable"
    assert elapsed < 2.0
    assert engine.calls == 1, "a second ping was started while the first still hangs"


def test_ready_is_ready_again_once_the_ping_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    engine = _HangingEngine(release)
    monkeypatch.setattr(api_module, "_database_engines", lambda: [engine])
    monkeypatch.setattr(api_module, "READY_TIMEOUT_SECONDS", 0.2)
    assert client.get("/ready").status_code == 503
    release.set()
    time.sleep(0.2)  # the hanging ping ends (with its error)
    monkeypatch.setattr(api_module, "_database_engines", lambda: [])

    assert client.get("/ready").status_code == 200


def test_compose_passes_the_statement_timeout_through_empty_by_default() -> None:
    compose = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.full.yml"
    if not compose.is_file():  # pragma: no cover - layout guard
        pytest.skip("no deploy/")
    text = compose.read_text(encoding="utf-8")

    assert 'PROCWORKS_DB_STATEMENT_TIMEOUT: "${PROCWORKS_DB_STATEMENT_TIMEOUT:-}"' in text
