# SPDX-License-Identifier: BUSL-1.1
"""A database that stops answering ends a request with 503 after a known time.

``statement_timeout`` lets the server cancel a statement that waits too long
-- but a *frozen* server (paused container, hung host, a network that keeps
the connection open) cancels nothing. A request on a connection the pool
already held then waited until the database came back; the readiness probe
had its own deadline, ordinary requests did not. TCP keepalives and
``tcp_user_timeout`` do not help either: the frozen host's kernel still
acknowledges every packet (measured against a paused PostgreSQL container).

So the client watches every round trip itself: a connection whose answer is
overdue by the statement timeout plus :data:`procworks.db.STALL_GRACE_SECONDS`
is closed from the client side. The waiting request gets an
``OperationalError`` -- the API's ``503 DB.unavailable`` -- and the pool
replaces the connection. The server-side timeout still comes first in every
case it can handle (a lock wait keeps its connection).
"""

from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from sqlalchemy import event

from procworks import db
from procworks.db import STALL_GRACE_SECONDS, StallWatchdog, stall_limit_seconds

_PG_URL = "postgresql+psycopg://process:process@db.invalid:5432/procworks"


def test_the_limit_follows_the_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROCWORKS_DB_STATEMENT_TIMEOUT", raising=False)
    assert stall_limit_seconds() == 30 + STALL_GRACE_SECONDS
    monkeypatch.setenv("PROCWORKS_DB_STATEMENT_TIMEOUT", "3")
    assert stall_limit_seconds() == 3 + STALL_GRACE_SECONDS


def test_no_statement_timeout_means_no_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grenzfall: ``0`` switches the server limit off -- and the client one with it."""

    monkeypatch.setenv("PROCWORKS_DB_STATEMENT_TIMEOUT", "0")
    assert stall_limit_seconds() is None


def test_an_invalid_timeout_still_aborts_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCWORKS_DB_STATEMENT_TIMEOUT", "1e3")
    with pytest.raises(ValueError, match="PROCWORKS_DB_STATEMENT_TIMEOUT"):
        stall_limit_seconds()


def _blocked_reader() -> tuple[socket.socket, socket.socket, dict[str, object]]:
    """A socket pair whose one end blocks in ``recv`` -- like a frozen server."""

    ours, theirs = socket.socketpair()
    result: dict[str, object] = {}

    def read() -> None:
        started = time.monotonic()
        result["data"] = ours.recv(10)
        result["seconds"] = time.monotonic() - started

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    result["thread"] = reader
    return ours, theirs, result


def test_the_watchdog_closes_an_overdue_connection() -> None:
    ours, theirs, result = _blocked_reader()
    connection = SimpleNamespace(pgconn=SimpleNamespace(socket=ours.fileno()))
    watchdog = StallWatchdog(tick_seconds=0.05)
    try:
        token = watchdog.watch(connection, 0.2)
        reader = result["thread"]
        assert isinstance(reader, threading.Thread)
        reader.join(5)
        assert not reader.is_alive(), "the blocked read was not ended"
        assert result["data"] == b""  # the connection was shut down
        assert 0.15 <= float(str(result["seconds"])) < 2
        watchdog.done(token)
    finally:
        ours.close()
        theirs.close()


def test_the_watchdog_leaves_a_timely_answer_alone() -> None:
    """Negativ: an answer within the limit is never cut off."""

    ours, theirs, result = _blocked_reader()
    connection = SimpleNamespace(pgconn=SimpleNamespace(socket=ours.fileno()))
    watchdog = StallWatchdog(tick_seconds=0.05)
    try:
        token = watchdog.watch(connection, 1.0)
        theirs.sendall(b"ok")
        reader = result["thread"]
        assert isinstance(reader, threading.Thread)
        reader.join(5)
        watchdog.done(token)
        time.sleep(1.2)  # past the deadline: the finished wait is no longer watched
        assert result["data"] == b"ok"
        theirs.sendall(b"still open")
        assert ours.recv(20) == b"still open"
    finally:
        ours.close()
        theirs.close()


def test_the_shared_postgres_engine_connects_through_the_watched_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROCWORKS_DB_STATEMENT_TIMEOUT", raising=False)
    engine = db.build_engine(_PG_URL)
    try:
        assert event.contains(engine, "do_connect", db._connect_watched)
    finally:
        engine.dispose()


def test_without_statement_timeout_the_engine_is_unwatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROCWORKS_DB_STATEMENT_TIMEOUT", "0")
    engine = db.build_engine(_PG_URL)
    try:
        assert not event.contains(engine, "do_connect", db._connect_watched)
    finally:
        engine.dispose()


def test_sqlite_is_never_watched() -> None:
    engine = db.build_engine("sqlite://")
    assert not event.contains(engine, "do_connect", db._connect_watched)


# --- the watched connection class ------------------------------------------------


class _RecordingWatchdog:
    """Stands in for :data:`procworks.db._WATCHDOG` and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def watch(self, connection: object, seconds: float) -> int:
        self.calls.append(("watch", seconds))
        return 7

    def done(self, token: int) -> None:
        self.calls.append(("done", token))


def _bare_connection(
    monkeypatch: pytest.MonkeyPatch, base_wait: object
) -> tuple[Any, _RecordingWatchdog]:
    """A watched connection without a server, whose base ``wait`` is replaced.

    :param base_wait: stands in for ``psycopg.Connection.wait``.
    :returns: ``(connection, watchdog)``: an unconnected instance of the
        watched class (``Any`` -- the tests set ``stall_seconds`` and call
        ``wait`` on it) and the stand-in watchdog that records the calls.
    """

    monkeypatch.setattr(psycopg.Connection, "wait", base_wait)
    watchdog = _RecordingWatchdog()
    monkeypatch.setattr(db, "_WATCHDOG", watchdog)
    cls = db._watched_connection_class()
    return cls.__new__(cls), watchdog


def test_the_watched_class_is_a_psycopg_connection_built_once() -> None:
    cls = db._watched_connection_class()

    assert issubclass(cls, psycopg.Connection)
    assert cls.__name__ == "WatchedConnection"
    assert cls.stall_seconds is None
    assert db._watched_connection_class() is cls


def test_a_watched_round_trip_is_registered_and_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, object]] = []

    def base_wait(self: object, gen: object, *args: object, **kwargs: object) -> str:
        seen.append((self, gen))
        assert watchdog.calls == [("watch", 8.0)]  # watched while waiting
        return "answer"

    connection, watchdog = _bare_connection(monkeypatch, base_wait)
    connection.stall_seconds = 8.0

    assert connection.wait("gen") == "answer"
    assert seen == [(connection, "gen")]
    assert watchdog.calls == [("watch", 8.0), ("done", 7)]


def test_without_a_limit_the_round_trip_is_not_watched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def base_wait(self: object, gen: object, *args: object, **kwargs: object) -> str:
        return "answer"

    connection, watchdog = _bare_connection(monkeypatch, base_wait)

    assert connection.wait("gen") == "answer"
    assert watchdog.calls == []


def test_a_failed_round_trip_is_released_and_the_error_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def base_wait(self: object, gen: object, *args: object, **kwargs: object) -> str:
        raise OSError("connection closed by the watchdog")

    connection, watchdog = _bare_connection(monkeypatch, base_wait)
    connection.stall_seconds = 2.0

    with pytest.raises(OSError, match="closed by the watchdog"):
        connection.wait("gen")
    assert watchdog.calls == [("watch", 2.0), ("done", 7)]


def test_a_new_connection_carries_the_stall_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCWORKS_DB_STATEMENT_TIMEOUT", "3")
    cls = db._watched_connection_class()
    opened: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def connect(klass: type, *args: object, **kwargs: object) -> object:
        opened.append((args, kwargs))
        return klass.__new__(klass)

    monkeypatch.setattr(cls, "connect", classmethod(connect))

    connection = db._connect_watched(None, None, ("dsn",), {"connect_timeout": 5})

    assert isinstance(connection, cls)
    assert opened == [(("dsn",), {"connect_timeout": 5})]
    assert connection.stall_seconds == 3 + STALL_GRACE_SECONDS
