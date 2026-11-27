# SPDX-License-Identifier: BUSL-1.1
"""PostgreSQL persistence via SQLAlchemy (roadmap step 4).

A process schema is stored as a JSON(B) document plus queryable columns
(``name``, ``version``, ``lifecycle_state``). On PostgreSQL the document column
is JSONB; on SQLite (used by the test suite) it falls back to plain JSON. The
store implements the same interface as the in-memory store, so the API does not
care which backend is active.

Process instances are persisted the same way (durable instance persistence):
one ``process_instance`` row per instance, again as a JSON(B) document plus
queryable columns (``schema_id``, ``schema_version``, ``state``).

The append-only audit/event log (roadmap step 15) is persisted durably here as
well (roadmap step 16): one ``audit_event`` row per recorded runtime event, with
a database-assigned monotonic ``seq`` and queryable columns for filtering.
"""

from __future__ import annotations

import functools
import hmac
import logging
import os
import secrets
import socket
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, override

from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Engine,
    Float,
    Index,
    Integer,
    String,
    create_engine,
    delete,
    event,
    func,
    insert,
    make_url,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.sql.expression import Executable

from procworks import unit_of_work
from procworks.audit import (
    AuditEvent,
    ChainState,
    EventType,
    chain_hash,
    historic_fingerprint,
)
from procworks.auth_password import LoginConflict, User, _SessionInfo
from procworks.env_numbers import plain_decimal, plain_whole_number
from procworks.idempotency import Replay, keyed_digest, secret_from_env
from procworks.licensing import AgentBinding, License, PendingClaim, TimeAnchor
from procworks.model import (
    DUE_MAIL_OUTBOX_STATES,
    DUE_OUTBOX_STATES,
    OPEN_EXTERNAL_TASK_STATES,
    AbsenceEntry,
    ExternalTask,
    Incident,
    InstanceState,
    LifecycleState,
    MailOutboxEntry,
    OrgModel,
    OutboxEntry,
    OutboxState,
    ProcessInstance,
    ProcessSchema,
    ProcessTemplate,
    WebhookDelivery,
    WebhookSubscription,
)
from procworks.store import SchemaSummary, remember_version, stored_key, stored_version
from procworks.unit_of_work import ConcurrentModification

logger = logging.getLogger("procworks.db")

#: JSONB on PostgreSQL, generic JSON elsewhere (e.g. SQLite in tests).
JsonDocument = JSON().with_variant(JSONB(), "postgresql")



class Base(DeclarativeBase):
    """Declarative base for the persistence models."""


class _DocumentRow(Protocol):
    """Strukturtyp jeder Zeilenklasse, die ein Pydantic-Dokument trägt.

    Fast alle Tabellen dieses Moduls speichern das vollständige Objekt als
    JSON(B)-Spalte ``document`` (daneben nur abfragbare Kopien einzelner
    Felder). Der Protokolltyp erlaubt :meth:`_SqlAlchemyStore._load`, für all
    diese Zeilenklassen typsicher auf ``document`` zuzugreifen, ohne ihnen eine
    gemeinsame gemappte Basisklasse zu geben — das würde die Spaltenreihenfolge
    der Tabellen berühren, die hier bewusst unverändert bleibt.
    """

    @property
    def document(self) -> dict[str, object]: ...


def _get_or_add[RowT: Base](
    session: Session, row_type: type[RowT], key: str, *, key_column: str = "id"
) -> RowT:
    """Liefere die Zeile mit Primärschlüssel ``key`` oder lege sie neu an.

    Das Upsert-Muster aller ``put``-Methoden: Existiert die Zeile, wird sie
    zurückgegeben und vom Aufrufer überschrieben; sonst entsteht eine neue
    Zeile, die nur den Primärschlüssel trägt und bereits der Session
    hinzugefügt ist. Die übrigen Spalten setzt der Aufrufer, **bevor** er
    ``self._commit(session)`` ruft — erst dann wird geschrieben, sodass Pflicht-
    spalten zum Zeitpunkt des INSERT gefüllt sind.

    Parameters
    ----------
    session:
        Die offene Session, in der der Aufrufer auch committet.
    row_type:
        Die gemappte Zeilenklasse (z. B. ``SchemaRow``).
    key:
        Wert des Primärschlüssels.
    key_column:
        Name der Primärschlüsselspalte für den Konstruktor einer neuen Zeile;
        meist ``id``, bei einzelnen Tabellen abweichend (``login``,
        ``license_id``, ``agent_id``, ``key``).

    Returns
    -------
    Die vorhandene oder die neu angelegte (noch nicht geschriebene) Zeile.
    """

    row = session.get(row_type, key)
    if row is None:
        row = row_type(**{key_column: key})
        session.add(row)
    return row


#: Seconds a PostgreSQL connection attempt may take before it gives up. Without
#: a limit a request waits for the operating system's TCP timeout (minutes)
#: while the database host is unreachable; with it, the request ends as 503.
PG_CONNECT_TIMEOUT_SECONDS = 5

#: Seconds one SQL statement of the API may run on PostgreSQL before the
#: server cancels it (``statement_timeout``). A statement that waits for a
#: lock held by another session, or on an overloaded server, otherwise waited
#: without limit and held a worker thread -- the request hung. 30 s is far
#: above any statement of a request or a background run (single rows, the
#: whole audit log in well under a second per 100 000 events).
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 30
#: Largest accepted ``PROCWORKS_DB_STATEMENT_TIMEOUT`` (one hour).
MAX_STATEMENT_TIMEOUT_SECONDS = 3600

def statement_timeout_millis() -> int:
    """``PROCWORKS_DB_STATEMENT_TIMEOUT`` in milliseconds (0 = no limit).

    Read when the engine is built (the first store at start-up), so an invalid
    value aborts the start with the variable named.

    :returns: the limit in milliseconds; :data:`DEFAULT_STATEMENT_TIMEOUT_SECONDS`
        when the variable is unset or blank (Compose hands an unset
        ``${VAR:-}`` over as the empty string).
    :raises ValueError: for anything but ``0`` or a plain decimal number of
        seconds from 1 to :data:`MAX_STATEMENT_TIMEOUT_SECONDS` (``2.5`` is
        fine; ``1_0``, ``1e3``, ``-1``, ``nan``, ``0.5``, ``3601`` are not).
    """

    name = "PROCWORKS_DB_STATEMENT_TIMEOUT"
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return DEFAULT_STATEMENT_TIMEOUT_SECONDS * 1000
    seconds = plain_decimal(raw)
    if seconds is not None:
        if seconds == 0:
            return 0
        if 1 <= seconds <= MAX_STATEMENT_TIMEOUT_SECONDS:
            return round(seconds * 1000)
    raise ValueError(
        f"Invalid {name}={raw!r}: expected 0 (off) or a number of seconds "
        f"from 1 to {MAX_STATEMENT_TIMEOUT_SECONDS}."
    )


#: Seconds beyond the statement timeout after which the API stops waiting for
#: a database that does not answer at all (:class:`StallWatchdog`). The
#: server's own ``statement_timeout`` comes first in every case it can handle;
#: this margin only ends the waits it cannot -- a frozen server cancels
#: nothing.
STALL_GRACE_SECONDS = 5


def stall_limit_seconds() -> float | None:
    """How long one round trip to PostgreSQL may take before it is given up.

    Derived from ``PROCWORKS_DB_STATEMENT_TIMEOUT`` (:func:`statement_timeout_millis`)
    plus :data:`STALL_GRACE_SECONDS`, so there is one setting for the
    operator: the server cancels a slow statement at the statement timeout,
    and only if even that answer does not come is the connection closed from
    this side.

    :returns: the limit in seconds, or ``None`` when the statement timeout is
        switched off (``0``) -- then nothing is limited, as before.
    :raises ValueError: for an invalid ``PROCWORKS_DB_STATEMENT_TIMEOUT``
        (the start aborts, see :func:`statement_timeout_millis`).
    """

    millis = statement_timeout_millis()
    if not millis:
        return None
    return millis / 1000 + STALL_GRACE_SECONDS


class StallWatchdog:
    """Closes database connections whose answer is overdue.

    A request waits for PostgreSQL's answer in a blocking read. When the
    server is frozen (a paused container, a hung host) that read never
    returns: ``statement_timeout`` needs a server that runs, and TCP
    keepalives or ``tcp_user_timeout`` do not fire either, because the
    frozen host's kernel still acknowledges every packet. So every round trip
    registers here (:meth:`watch`/:meth:`done`, from the connection class of
    :func:`_watched_connection_class`), and one background thread shuts the
    socket of an overdue connection down. The blocked read then returns,
    the driver raises ``OperationalError`` -- for the API a ``503
    DB.unavailable`` -- and the pool discards the connection.

    One instance serves all connections of the process; its thread starts
    with the first watched round trip and is a daemon (it never delays the
    shutdown).

    :param tick_seconds: how often the thread looks for overdue connections;
        the cut-off is at most this late.
    """

    def __init__(self, tick_seconds: float = 0.5) -> None:
        self._tick = tick_seconds
        self._lock = threading.Lock()
        self._watched: dict[int, tuple[float, Any]] = {}
        self._next_token = 0
        self._thread: threading.Thread | None = None

    def watch(self, connection: Any, seconds: float) -> int:
        """Start watching one round trip of ``connection``.

        :param connection: a psycopg connection (anything with
            ``pgconn.socket``, the socket's file descriptor).
        :param seconds: how long the answer may take.
        :returns: the token to hand to :meth:`done`.
        """

        with self._lock:
            self._next_token += 1
            token = self._next_token
            self._watched[token] = (time.monotonic() + seconds, connection)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="db-stall-watchdog", daemon=True
                )
                self._thread.start()
        return token

    def done(self, token: int) -> None:
        """Stop watching the round trip of ``token`` (it returned in any way)."""

        with self._lock:
            self._watched.pop(token, None)

    def _run(self) -> None:
        """The thread: close every connection past its deadline, forever."""

        while True:
            time.sleep(self._tick)
            now = time.monotonic()
            with self._lock:
                overdue = [
                    (token, connection)
                    for token, (deadline, connection) in self._watched.items()
                    if deadline <= now
                ]
                for token, _ in overdue:
                    del self._watched[token]
            for _, connection in overdue:
                self._sever(connection)

    @staticmethod
    def _sever(connection: Any) -> None:
        """Shut the socket of ``connection`` down so its blocked read returns.

        Works on a duplicate of the descriptor: ``shutdown`` acts on the
        socket itself, and closing the duplicate leaves the driver's own
        descriptor for the driver to close. Errors (the connection closed in
        the meantime) are ignored.
        """

        try:
            fd = int(connection.pgconn.socket)
            duplicate = socket.socket(fileno=os.dup(fd))
        except (AttributeError, OSError, ValueError, TypeError):
            return
        try:
            duplicate.shutdown(socket.SHUT_RDWR)
            logging.getLogger("procworks.db").warning(
                "database did not answer in time; connection closed "
                "(the request answers 503)"
            )
        except OSError:
            pass
        finally:
            duplicate.close()


_WATCHDOG = StallWatchdog()

_watched_class: type | None = None
_watched_class_lock = threading.Lock()


def _watched_connection_class() -> type:
    """The psycopg connection class whose every round trip is watched.

    ``psycopg.Connection.wait`` is the one place every statement, fetch,
    commit and rollback waits for the server; the subclass registers each
    such wait with :data:`_WATCHDOG` for :func:`stall_limit_seconds`. Built
    on first use, so psycopg stays an optional dependency (SQLite needs it
    not). ``wait`` is marked ``@override``: should the driver ever rename or
    drop the method, the type check says so instead of the watch silently
    no longer applying.

    :returns: the subclass (one per process); its ``stall_seconds`` class
        attribute is ``None`` (unwatched) until a connection sets its own.
    """

    global _watched_class
    with _watched_class_lock:
        if _watched_class is None:
            import psycopg

            class WatchedConnection(psycopg.Connection[Any]):
                """A psycopg connection that gives up on a frozen server."""

                #: Limit of one round trip in seconds; ``None`` waits unwatched.
                stall_seconds: float | None = None

                @override
                def wait(self, gen: Any, *args: Any, **kwargs: Any) -> Any:
                    """Wait for the server like psycopg, watched by :data:`_WATCHDOG`.

                    :param gen: psycopg's wait generator, passed through.
                    :param args: further arguments of ``psycopg.Connection.wait``.
                    :param kwargs: likewise.
                    :returns: what ``psycopg.Connection.wait`` returns.
                    :raises Exception: whatever psycopg raises -- after the
                        watchdog shut the socket, the ``OperationalError`` of
                        the broken read. The registration is released in
                        every case.
                    """

                    limit = self.stall_seconds
                    if limit is None:
                        return super().wait(gen, *args, **kwargs)
                    token = _WATCHDOG.watch(self, limit)
                    try:
                        return super().wait(gen, *args, **kwargs)
                    finally:
                        _WATCHDOG.done(token)

            _watched_class = WatchedConnection
        return _watched_class


def _connect_watched(
    dialect: Any, connection_record: Any, cargs: tuple[Any, ...], cparams: dict[str, Any]
) -> Any:
    """``do_connect`` hook: open the connection with the watched class.

    Same arguments as the dialect's own connect; the limit is read now
    (:func:`stall_limit_seconds`), the same value the engine was built with.
    """

    cls = _watched_connection_class()
    connection = cls.connect(*cargs, **cparams)  # type: ignore[attr-defined]  # psycopg classmethod, class built at run time
    connection.stall_seconds = stall_limit_seconds()
    return connection


def build_engine(url: str) -> Engine:
    """Create an engine for ``url`` with :func:`engine_options`.

    For PostgreSQL with a statement timeout, the connections are opened
    through :func:`_connect_watched`, so a frozen server ends a request with
    503 after :func:`stall_limit_seconds` instead of holding it until the
    server returns. SQLite and a switched-off timeout stay as they were.

    :param url: the SQLAlchemy database URL.
    :returns: the new engine.
    :raises ValueError: for an invalid setting (:func:`engine_options`).
    """

    engine = create_engine(url, future=True, **engine_options(url))
    if url.startswith("postgresql") and stall_limit_seconds() is not None:
        event.listen(engine, "do_connect", _connect_watched)
    return engine


def engine_options(url: str) -> dict[str, Any]:
    """Keyword arguments for ``create_engine`` that survive a database outage.

    * ``pool_pre_ping``: the pool tests a connection (a cheap ``SELECT 1``)
      before handing it out and replaces it when it is dead. Without it every
      connection opened before a database restart failed once more -- requests
      kept failing for minutes after the database was back.
    * ``connect_args.connect_timeout`` (PostgreSQL only): a connection attempt
      gives up after :data:`PG_CONNECT_TIMEOUT_SECONDS`.
    * ``connect_args.options`` ``-c statement_timeout=<ms>`` (PostgreSQL only,
      :func:`statement_timeout_millis`; left out for ``0``): the server
      cancels a statement over the limit; the API answers 503
      ``DB.unavailable`` (``QueryCanceled`` is an ``OperationalError``). Only
      this pool is affected -- Alembic migrations build their own engine,
      backup and restore run ``pg_dump``/``pg_restore`` with their own
      connections, external SQL connectors have their own engines. A frozen
      server cannot cancel anything; for that :func:`build_engine` adds the
      client-side :class:`StallWatchdog`.
    * ``pool_size``/``max_overflow``/``pool_timeout`` (server databases, not
      SQLite): the bounded pool of :func:`pool_options`.

    Parameters
    ----------
    url:
        The SQLAlchemy database URL.

    Returns
    -------
    The options; SQLite (tests, local use) gets only ``pool_pre_ping``, since
    its driver knows no connect timeout and an in-memory engine's pool takes
    no size (the pool settings are not even read for it).

    Raises
    ------
    ValueError
        For an invalid pool setting of a server database (:func:`pool_options`)
        or statement timeout (:func:`statement_timeout_millis`).
    """

    options: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        connect_args: dict[str, Any] = {"connect_timeout": PG_CONNECT_TIMEOUT_SECONDS}
        millis = statement_timeout_millis()
        if millis:
            connect_args["options"] = f"-c statement_timeout={millis}"
        options["connect_args"] = connect_args
    if not url.startswith("sqlite"):
        options.update(pool_options())
    return options


#: Connections the shared pool of a server database keeps open (``pool_size``).
DEFAULT_POOL_SIZE = 10
#: Connections it may open beyond that under load (``max_overflow``).
DEFAULT_MAX_OVERFLOW = 10
#: Seconds a request waits for a free connection before it ends as 503.
DEFAULT_POOL_TIMEOUT_SECONDS = 30.0
#: Bounds of the pool settings. 100 connections per process already exhaust
#: PostgreSQL's default ``max_connections``; a larger value is a typo, not a
#: plan. A wait below one second ends every request that competes for a
#: connection with 503 at once; above five minutes a request hangs instead
#: of reporting the shortage.
MAX_POOL_SIZE = 100
MAX_POOL_OVERFLOW = 100
MIN_POOL_TIMEOUT_SECONDS = 1.0
MAX_POOL_TIMEOUT_SECONDS = 300.0


def _env_whole_number(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read the whole number ``name`` from the environment.

    Parameters
    ----------
    name:
        The environment variable, e.g. ``PROCWORKS_DB_POOL_SIZE``.
    default:
        The value when the variable is not set or empty (Compose passes an
        unset ``${VAR:-}`` as the empty string).
    minimum, maximum:
        The accepted range (both included).

    Returns
    -------
    The value; surrounding blanks are ignored (``" 4 "`` is 4).

    Raises
    ------
    ValueError
        When the variable is set but is no plain whole number
        (:func:`procworks.env_numbers.plain_whole_number`: ``2.5``, ``1_0``,
        ``+5``, ``1e1`` are not) or lies outside the range. The message names
        the variable, the value and the range, so the start aborts with a
        hint an operator can act on instead of a pool that silently behaves
        differently.
    """

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = plain_whole_number(raw)
    if value is None or not minimum <= value <= maximum:
        raise ValueError(
            f"Invalid {name}={raw!r}: expected a whole number from {minimum} to {maximum}."
        )
    return value


def _env_seconds(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a number of seconds ``name`` from the environment.

    Parameters
    ----------
    name:
        The environment variable, e.g. ``PROCWORKS_DB_POOL_TIMEOUT``.
    default:
        The value when the variable is not set or empty.
    minimum, maximum:
        The accepted range (both included).

    Returns
    -------
    The seconds as ``float`` (``"7.5"`` is allowed).

    Raises
    ------
    ValueError
        When the value is no plain decimal number
        (:func:`procworks.env_numbers.plain_decimal`: ``1e-9``, ``nan``,
        ``inf``, ``3_0`` are not) or lies outside the range -- a wait of a
        nanosecond or without end is exactly what the limit prevents.
    """

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = plain_decimal(raw)
    if value is None or not minimum <= value <= maximum:
        raise ValueError(
            f"Invalid {name}={raw!r}: expected a number of seconds "
            f"from {minimum:g} to {maximum:g}."
        )
    return value


def pool_options() -> dict[str, Any]:
    """Size of the connection pool of a server database, from the environment.

    * ``PROCWORKS_DB_POOL_SIZE`` (default :data:`DEFAULT_POOL_SIZE`, 1 to
      :data:`MAX_POOL_SIZE`): connections kept open. ``0`` is rejected on
      purpose -- SQLAlchemy reads a pool size of 0 as *no limit*.
    * ``PROCWORKS_DB_MAX_OVERFLOW`` (default :data:`DEFAULT_MAX_OVERFLOW`, 0 to
      :data:`MAX_POOL_OVERFLOW`): extra connections under load, closed again
      when returned.
    * ``PROCWORKS_DB_POOL_TIMEOUT`` (default
      :data:`DEFAULT_POOL_TIMEOUT_SECONDS`, 1 to 300 seconds): how long a
      request waits for a free connection; then it fails with the pool's
      timeout error, which the API answers with 503 ``DB.unavailable``.

    Only plain decimal numbers count (:mod:`procworks.env_numbers`).

    Since all stores of a process share one engine (:func:`shared_engine`),
    ``pool size + overflow`` is the most connections one API process opens
    to its database. Several processes against one database need
    ``processes x (pool size + overflow)`` below the server's
    ``max_connections`` (PostgreSQL default 100), with room left for backups
    and administration.

    Returns
    -------
    ``pool_size``, ``max_overflow`` and ``pool_timeout`` for ``create_engine``.

    Raises
    ------
    ValueError
        For an invalid value (see :func:`_env_whole_number`,
        :func:`_env_seconds`); read when the engine is built, i.e. when the
        first store is created at start-up, so the start aborts.
    """

    return {
        "pool_size": _env_whole_number(
            "PROCWORKS_DB_POOL_SIZE", DEFAULT_POOL_SIZE, 1, MAX_POOL_SIZE
        ),
        "max_overflow": _env_whole_number(
            "PROCWORKS_DB_MAX_OVERFLOW", DEFAULT_MAX_OVERFLOW, 0, MAX_POOL_OVERFLOW
        ),
        "pool_timeout": _env_seconds(
            "PROCWORKS_DB_POOL_TIMEOUT",
            DEFAULT_POOL_TIMEOUT_SECONDS,
            MIN_POOL_TIMEOUT_SECONDS,
            MAX_POOL_TIMEOUT_SECONDS,
        ),
    }


#: The engines of this process, one per database URL. Weak: an engine lives as
#: long as a store uses it, so short-lived stores (tests, tools) do not keep
#: their connections open for the life of the process.
_shared_engines: weakref.WeakValueDictionary[str, Engine] = weakref.WeakValueDictionary()
_shared_engines_lock = threading.Lock()


def _is_in_memory_sqlite(url: str) -> bool:
    """Whether ``url`` names an in-memory SQLite database.

    Such a URL names no database that exists apart from its engine: every
    engine built from ``sqlite://`` is a database of its own. Sharing it would
    merge stores that are separate today, so :func:`shared_engine` never does.
    """

    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return False
    database = parsed.database or ""
    return database in ("", ":memory:") or parsed.query.get("mode") == "memory"


def shared_engine(url: str) -> Engine:
    """The one engine (and connection pool) of this process for ``url``.

    Every SQLAlchemy store of this module gets its engine here. Before, each
    store built its own engine with its own pool; thirteen stores per API
    process could hold close to 200 database connections, and parallel
    requests or a second API process exhausted PostgreSQL's
    ``max_connections``. Now a process opens at most
    ``pool size + overflow`` connections per database (:func:`pool_options`).

    Built on first use with :func:`engine_options` (pre-ping, connect timeout,
    pool size) and thread-safe: concurrent first calls build exactly one
    engine. Transaction boundaries do not change -- each store method still
    opens its own session on the shared engine.

    Parameters
    ----------
    url:
        SQLAlchemy database URL; the exact string is the key.

    Returns
    -------
    The shared engine. For an in-memory SQLite URL a new, unshared engine on
    every call (see :func:`_is_in_memory_sqlite`).

    Raises
    ------
    ValueError
        For an invalid pool setting in the environment (:func:`pool_options`).
    """

    if _is_in_memory_sqlite(url):
        return build_engine(url)
    with _shared_engines_lock:
        engine = _shared_engines.get(url)
        if engine is None:
            engine = build_engine(url)
            _shared_engines[url] = engine
        return engine


def dispose_shared_engines() -> None:
    """Close the idle pooled connections of every shared engine.

    Called when the API shuts down (lifespan), so the database sees the
    process's connections end instead of waiting for them to time out. The
    engines stay usable: a later request opens new connections. In-memory
    SQLite engines are not shared and therefore never disposed here -- that
    would drop their data.
    """

    with _shared_engines_lock:
        engines = list(_shared_engines.values())
    for engine in engines:
        engine.dispose()


class _SqlAlchemyStore:
    """Gemeinsame Basis aller SQLAlchemy-Stores dieses Moduls.

    Bündelt, was jeder Store bisher wörtlich wiederholte: den Bezug der Engine
    zur Datenbank-URL, das optionale Anlegen der Tabellen und die drei
    wiederkehrenden Einzelzeilen-Zugriffe (Dokument laden, Zeile löschen,
    Tabellen leeren). Jeder Zugriff öffnet eine eigene Session und committet
    selbst — die Transaktionsgrenzen sind damit dieselben wie zuvor in den
    einzelnen Methoden.

    Parameters
    ----------
    url:
        SQLAlchemy database URL (e.g. ``postgresql+psycopg://user:pw@host/db``
        or ``sqlite:///schemas.db``).
    create_tables:
        If True, create the tables from the ORM metadata. Useful for SQLite and
        local development; in production prefer Alembic migrations and pass
        ``create_tables=False``.
    """

    def __init__(self, url: str, *, create_tables: bool = False) -> None:
        # Eine Engine je Datenbank und Prozess für alle Stores (gemeinsamer,
        # begrenzter Pool) -- nie eine eigene je Store, siehe shared_engine.
        self._engine = shared_engine(url)
        if create_tables:
            Base.metadata.create_all(self._engine)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Die Session, in der eine Store-Methode arbeitet.

        Innerhalb einer Arbeitseinheit (:mod:`procworks.unit_of_work`) ist das
        die eine Session der Einheit -- alle Stores schreiben dann in dieselbe
        Transaktion, die erst am Ende der Anfrage festgeschrieben oder
        zurückgerollt wird, und eine Anfrage belegt genau eine Verbindung.
        Außerhalb einer Einheit (Start, Seeds, Hintergrund nach dem Commit,
        Tests) eine eigene, kurze Session wie bisher. Schreibende Methoden
        beenden mit :meth:`_commit`, nie mit ``session.commit()``.
        """

        with unit_of_work.store_session(self._engine) as session:
            yield session

    @staticmethod
    def _commit(session: Session) -> None:
        """Schreibvorgang beenden: in einer Arbeitseinheit nur ``flush``
        (festgeschrieben wird am Ende der Einheit), sonst ``commit``."""

        unit_of_work.finish_write(session)

    def _load[RowT: _DocumentRow, ModelT: BaseModel](
        self, row_type: type[RowT], key: str, model: type[ModelT]
    ) -> ModelT | None:
        """Lade das Dokument der Zeile ``key`` als Pydantic-Modell ``model``.

        Returns
        -------
        Das aus der Spalte ``document`` validierte Modell, oder ``None``, wenn
        es keine Zeile mit diesem Primärschlüssel gibt.
        """

        with self._session() as session:
            row = session.get(row_type, key)
            if row is None:
                return None
            return model.model_validate(row.document)

    def _delete_row(self, row_type: type[Base], key: str) -> bool:
        """Lösche die Zeile mit Primärschlüssel ``key``, falls vorhanden.

        Idempotent: Fehlt die Zeile, geschieht nichts (auch kein Commit).

        Returns
        -------
        ``True``, wenn eine Zeile gelöscht wurde, sonst ``False`` — Aufrufer,
        deren Schnittstelle nichts zurückgibt, ignorieren den Wert.
        """

        with self._session() as session:
            row = session.get(row_type, key)
            if row is None:
                return False
            session.delete(row)
            self._commit(session)
            return True

    def _delete_all(self, *row_types: type[Base]) -> None:
        """Leere die Tabellen ``row_types`` in genau dieser Reihenfolge.

        Alle Löschungen laufen in einer Session und werden gemeinsam
        committet (eine Transaktion). Die Reihenfolge ist Sache des Aufrufers:
        abhängige Tabellen zuerst.
        """

        with self._session() as session:
            for row_type in row_types:
                session.execute(delete(row_type))
            self._commit(session)


class SchemaRow(Base):
    """One row per process schema (keyed by schema id).

    ``version`` is the model's own version (1, 2, ... per revision);
    ``lock_version`` counts the saves of this row for the conditional save
    (:func:`_save_versioned`). Rows stored before the column existed start at
    1 (Alembic ``0014``).
    """

    __tablename__ = "process_schema"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class SqlAlchemySchemaStore(_SqlAlchemyStore):
    """A schema store backed by a SQLAlchemy engine.

    Constructor parameters (``url``, ``create_tables``) as in
    :class:`_SqlAlchemyStore`.
    """

    def put(self, schema: ProcessSchema) -> ProcessSchema:
        """Save ``schema`` -- only if nobody saved it since it was loaded.

        :param schema: a schema loaded from this store (or a copy of it), or a
            new one that was never stored.
        :returns: ``schema``, now carrying the new stored version.
        :raises ConcurrentModification: the stored row changed since
            ``schema`` was loaded, or a new schema uses a stored id
            (see :func:`_save_versioned`).
        """

        _save_versioned(
            self,
            SchemaRow,
            "schema",
            schema,
            {
                "version": schema.version,
                "name": schema.name,
                "lifecycle_state": schema.lifecycle_state.value,
                "document": schema.model_dump(mode="json"),
            },
        )
        return schema

    def get(self, schema_id: str) -> ProcessSchema | None:
        """Load a schema together with its stored version (``None`` if absent)."""

        return _load_versioned(self, SchemaRow, schema_id, ProcessSchema)

    def list_ids(self) -> list[str]:
        """All stored schema ids, ascending (``ORDER BY id``).

        The order is explicit: without it the database returns whatever its
        plan yields -- the primary key index for this query, the storage
        order for a scan of the table (:meth:`summaries`), and the storage
        order changes with every update of a row.
        """

        with self._session() as session:
            return list(session.scalars(select(SchemaRow.id).order_by(SchemaRow.id)))

    def summaries(self) -> list[SchemaSummary]:
        """One :class:`~procworks.store.SchemaSummary` per stored schema.

        One query over the columns the row keeps anyway (name, version,
        state) plus ``revision_of`` read inside the JSON document -- the
        documents themselves are not loaded, so a list of hundreds of models
        stays one cheap request. Same order as :meth:`list_ids` (ascending
        id, ``ORDER BY`` in both -- a table scan alone returns the storage
        order, which differed from the index order of the id list).
        """

        lineage = SchemaRow.document["revision_of"].as_string()
        with self._session() as session:
            rows = session.execute(
                select(
                    SchemaRow.id,
                    SchemaRow.name,
                    SchemaRow.version,
                    SchemaRow.lifecycle_state,
                    lineage,
                ).order_by(SchemaRow.id)
            ).all()
        return [
            SchemaSummary(
                id=row[0],
                name=row[1],
                version=row[2],
                lifecycle_state=LifecycleState(row[3]),
                revision_of=row[4] or None,
            )
            for row in rows
        ]

    def clear(self) -> None:
        self._delete_all(SchemaRow)


class OrgRow(Base):
    """One row per shared, standalone org model (keyed by org id).

    ``lock_version`` counts the saves of this row for the conditional save
    (:func:`_save_versioned`), as for schemas and cases; rows stored before
    the column existed start at 1 (Alembic ``0018``).
    """

    __tablename__ = "org_model"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class SqlAlchemyOrgStore(_SqlAlchemyStore):
    """A shared-org-model store backed by a SQLAlchemy engine.

    Mirrors ``SqlAlchemySchemaStore``: same engine/URL conventions and the same
    ``put``/``get``/``list_ids`` interface as the in-memory org store, so the
    API is agnostic of the backend. Saved conditionally like a schema
    (:func:`_save_versioned`, kind ``"org"``): two requests that read the same
    org no longer both save -- before, the later one replaced the earlier one
    whenever no referencing schema was saved alongside.
    """

    def put(self, org: OrgModel) -> OrgModel:
        """Save ``org`` -- only if nobody saved it since it was loaded.

        :param org: a shared org model (``id`` set), loaded from this store
            or new.
        :returns: ``org``, now carrying the new stored version.
        :raises ValueError: ``org`` has no id (an embedded model).
        :raises ConcurrentModification: the stored row changed since ``org``
            was loaded, or a new org uses a stored id.
        """

        if org.id is None:
            raise ValueError("a shared org model must have an id before it is stored")
        _save_versioned(
            self,
            OrgRow,
            "org",
            org,
            {"name": org.name, "document": org.model_dump(mode="json")},
        )
        return org

    def get(self, org_id: str) -> OrgModel | None:
        """Load a shared org together with its stored version (``None`` if absent)."""

        return _load_versioned(self, OrgRow, org_id, OrgModel)

    def list_ids(self) -> list[str]:
        with self._session() as session:
            return list(session.scalars(select(OrgRow.id)))

    def clear(self) -> None:
        self._delete_all(OrgRow)


class InstanceRow(Base):
    """One row per process instance (keyed by instance id).

    ``lock_version`` counts the saves of this row for the conditional save
    (:func:`_save_versioned`); rows stored before the column existed start at
    1 (Alembic ``0014``).
    """

    __tablename__ = "process_instance"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_id: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


def _save_versioned(
    store: _SqlAlchemyStore,
    row_type: type[SchemaRow] | type[InstanceRow] | type[ExternalTaskRow] | type[OrgRow],
    kind: str,
    obj: ProcessSchema | ProcessInstance | ExternalTask | OrgModel,
    values: dict[str, object],
) -> None:
    """The conditional save of a case, model or external task (optimistic version check).

    Raising form of :func:`_write_versioned` (same rules): a refused save
    raises :class:`ConcurrentModification`, recorded on the running unit of
    work first (:func:`procworks.unit_of_work.note_conflict`), so the unit is
    repeated on fresh data.

    :param store: the store (for its session and dialect).
    :param row_type: :class:`SchemaRow`, :class:`InstanceRow`,
        :class:`ExternalTaskRow` or :class:`OrgRow`.
    :param kind: ``"schema"``, ``"instance"``, ``"external_task"`` or
        ``"org"``, named in the conflict.
    :param obj: the object being saved.
    :param values: the column values besides ``id`` and ``lock_version``.
    :raises ConcurrentModification: see :func:`_write_versioned`.
    """

    if not _write_versioned(store, row_type, obj, values):
        raise unit_of_work.note_conflict(ConcurrentModification(kind, stored_key(obj)))


def _write_versioned(
    store: _SqlAlchemyStore,
    row_type: type[SchemaRow] | type[InstanceRow] | type[ExternalTaskRow] | type[OrgRow],
    obj: ProcessSchema | ProcessInstance | ExternalTask | OrgModel,
    values: dict[str, object],
) -> bool:
    """The conditional save of a case, model or external task (optimistic version check).

    * **Loaded object** (it carries the version it was read with,
      :func:`procworks.store.stored_version`): ``UPDATE ... SET ...,
      lock_version = v + 1 WHERE id = :id AND lock_version = v``. No row
      updated means somebody else saved it in between (or deleted it): the
      save is refused. On PostgreSQL the second of two concurrent updates
      waits for the first transaction and then finds the version changed.
    * **New object** (never loaded, no version): ``INSERT`` with version 1.
      If the id is already stored, that is a conflict too -- creating never
      replaces (two requests creating under one explicit id used to
      overwrite each other). On PostgreSQL/SQLite it is ``INSERT ... ON
      CONFLICT DO NOTHING``, so the expected collision is no database error.
      Without a conflict target this also covers every other unique index
      of the table -- for ``external_task`` the one open task per step: a
      second creator of the same step's task gets ``False``, not an error.

    Whether a row was written is read from ``RETURNING``, never from the
    driver's ``rowcount`` (unreliable with SQLAlchemy 2.1 and psycopg 3, see
    :class:`SqlAlchemyIdClaimStore`). On success the new version is set on
    ``obj``, so the same request may save it again.

    Records nothing on the unit of work: callers that skip a lost candidate
    (the external-task queue) use the result directly, :func:`_save_versioned`
    turns ``False`` into a conflict.

    :param store: the store (for its session and dialect).
    :param row_type: :class:`SchemaRow`, :class:`InstanceRow` or
        :class:`ExternalTaskRow`.
    :param obj: the object being saved.
    :param values: the column values besides ``id`` and ``lock_version``.
    :returns: whether the row was written (see above for when it is not).
    """

    key = obj.id
    base = stored_version(obj)
    dialect = store._engine.dialect.name
    with store._session() as session:
        if base is None:
            new = 1
            row = {"id": key, "lock_version": new, **values}
            if dialect in ("postgresql", "sqlite"):
                dialect_insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
                written = session.execute(
                    dialect_insert(row_type)
                    .values(**row)
                    .on_conflict_do_nothing()
                    .returning(row_type.id)
                ).first()
            elif session.get(row_type, key) is None:
                written = session.execute(
                    insert(row_type).values(**row).returning(row_type.id)
                ).first()
            else:
                written = None
        else:
            new = base + 1
            written = session.execute(
                update(row_type)
                .where(row_type.id == key, row_type.lock_version == base)
                .values(lock_version=new, **values)
                .returning(row_type.id)
                .execution_options(synchronize_session=False)
            ).first()
        if written is None:
            return False
        store._commit(session)
    remember_version(obj, new)
    return True


def _load_versioned[ModelT: (ProcessSchema, ProcessInstance, OrgModel)](
    store: _SqlAlchemyStore,
    row_type: type[SchemaRow] | type[InstanceRow] | type[OrgRow],
    key: str,
    model: type[ModelT],
) -> ModelT | None:
    """Load a case or model and attach the version it was read with.

    Reads the columns directly (no ORM object in the session's identity map),
    so a later read in the same unit of work sees the unit's own updates.

    :returns: the validated model carrying its stored version, or ``None``.
    """

    with store._session() as session:
        row = session.execute(
            select(row_type.document, row_type.lock_version).where(row_type.id == key)
        ).first()
    if row is None:
        return None
    loaded = model.model_validate(row.document)
    remember_version(loaded, int(row.lock_version))
    return loaded


class SqlAlchemyInstanceStore(_SqlAlchemyStore):
    """A process-instance store backed by a SQLAlchemy engine.

    Mirrors ``SqlAlchemySchemaStore``: same engine/URL conventions and the same
    ``put``/``get``/``list_ids`` interface as the in-memory instance store, so
    the execution engine and API are agnostic of the backend.
    """

    def hold_write_gate(self, *, exclusive: bool) -> None:
        """Pass the write gate of the database for the running unit of work.

        Every mutating request passes it shared, first thing in its unit; the
        reset of all data (``POST /admin/reset``) passes it exclusively. So a
        reset waits until the requests already under way have committed --
        its deletions then see and remove what they stored -- and requests
        arriving meanwhile wait until the reset has committed, then work on
        the new state (a case started on a wiped model gets 404). Without it
        a case started during a reset was saved after the reset had deleted
        its model, and stayed behind without one. Held until the unit commits
        or rolls back (:func:`_write_gate_statement`); a no-op on databases
        without such a lock and outside a unit of work (the lock would end
        with this method's own session at once).

        :param exclusive: the reset's side of the gate.
        """

        if unit_of_work.current() is None:
            return
        statement = _write_gate_statement(self._engine.dialect.name, exclusive=exclusive)
        if statement is None:
            return
        with self._session() as session:
            session.connection().execute(statement)

    def put(self, instance: ProcessInstance) -> ProcessInstance:
        """Save ``instance`` -- only if nobody saved it since it was loaded.

        :param instance: an instance loaded from this store (or the engine's
            copy of it), or a new one that was never stored.
        :returns: ``instance``, now carrying the new stored version.
        :raises ConcurrentModification: see :func:`_save_versioned`.
        """

        _save_versioned(
            self,
            InstanceRow,
            "instance",
            instance,
            {
                "schema_id": instance.schema_id,
                "schema_version": instance.schema_version,
                "state": instance.state.value,
                "document": instance.model_dump(mode="json"),
            },
        )
        return instance

    def get(self, instance_id: str) -> ProcessInstance | None:
        """Load an instance together with its stored version (``None`` if absent)."""

        return _load_versioned(self, InstanceRow, instance_id, ProcessInstance)

    def list_ids(self) -> list[str]:
        with self._session() as session:
            return list(session.scalars(select(InstanceRow.id)))

    def running_schema_ids(self) -> set[str]:
        """The schema ids of the RUNNING cases, in one query.

        Reads the ``state`` and ``schema_id`` columns only (no document),
        so a pass that concerns some schemas decides on them before it
        loads any case. See :meth:`procworks.store.InMemoryInstanceStore.running_schema_ids`.
        """

        with self._session() as session:
            return set(
                session.scalars(
                    select(InstanceRow.schema_id)
                    .where(InstanceRow.state == InstanceState.RUNNING.value)
                    .distinct()
                )
            )

    def list_instances(
        self,
        *,
        running_only: bool = False,
        schema_ids: Collection[str] | None = None,
    ) -> list[ProcessInstance]:
        """Many cases in **one** query, each carrying its stored version.

        The overview paths (worklists, monitoring, titles, escalation sweep,
        push drive) used to load case after case with :meth:`get` -- one
        round trip per case, so every overview grew linearly with the
        stock. Filters on the ``state`` and ``schema_id`` columns, which
        :meth:`put` keeps in step with the document.

        :param running_only: only cases in state RUNNING.
        :param schema_ids: only cases of these schema ids; ``None`` means
            every schema, an empty collection means none (no query at all).
        :returns: the matching cases (order as :meth:`list_ids`, i.e. not
            guaranteed); each validated from its document and carrying the
            version it was read with, like a case loaded with :meth:`get`.
        """

        if schema_ids is not None and not schema_ids:
            return []
        stmt = select(InstanceRow.document, InstanceRow.lock_version)
        if running_only:
            stmt = stmt.where(InstanceRow.state == InstanceState.RUNNING.value)
        if schema_ids is not None:
            stmt = stmt.where(InstanceRow.schema_id.in_(sorted(set(schema_ids))))
        with self._session() as session:
            rows = session.execute(stmt).all()
        loaded: list[ProcessInstance] = []
        for row in rows:
            instance = ProcessInstance.model_validate(row.document)
            remember_version(instance, int(row.lock_version))
            loaded.append(instance)
        return loaded

    def clear(self) -> None:
        self._delete_all(InstanceRow)


class AuditEventRow(Base):
    """One row per recorded runtime event (append-only event log)."""

    __tablename__ = "audit_event"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    schema_id: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[dict[str, str]] = mapped_column(JsonDocument, nullable=False)
    #: Append-only hash chain (see procworks.audit.AuditEvent). Defaulted to ""
    #: so rows written before the chain existed remain valid.
    prev_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    entry_hash: Mapped[str] = mapped_column(String, nullable=False, default="")


#: Primary key of the one row of ``audit_chain``.
_CHAIN_ROW_ID = 1


class AuditChainRow(Base):
    """The recorded end of the audit hash chain -- one row (``audit_chain``).

    Every append locks this row first (an ``UPDATE`` of it, see
    :meth:`SqlAlchemyAuditLog._lock_chain_end`) and moves it to the new event
    in the same transaction. Two transactions can therefore never link to the
    same end: the second waits until the first committed and then reads the
    new end. Before this row existed, two requests read the newest event at
    the same moment and both linked to it -- the chain branched.

    ``serialized_since`` is the newest sequence number written before the row
    existed (Alembic ``0016`` or the first use of a database without it):
    branches up to it are historic, after it a defect.

    ``historic_events``/``historic_digest`` fix that historic region as it was
    at the update (Alembic ``0017``, or the first use): count and digest of
    its events (:func:`procworks.audit.historic_fingerprint`). The check holds
    the stored events to them, so a removal or rewrite there is found also
    before any anchor. ``NULL`` when there is no historic region
    (``serialized_since`` 0: the log was empty at the update) -- then nothing
    is compared.
    """

    __tablename__ = "audit_chain"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    head_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    head_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    serialized_since: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    historic_events: Mapped[int | None] = mapped_column(Integer, nullable=True)
    historic_digest: Mapped[str | None] = mapped_column(String, nullable=True)


def _event_from_row(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        seq=row.seq,
        timestamp=row.timestamp,
        event_type=EventType(row.event_type),
        instance_id=row.instance_id,
        schema_id=row.schema_id,
        schema_version=row.schema_version,
        node_id=row.node_id,
        label=row.label,
        agent_id=row.agent_id,
        detail=dict(row.detail),
        prev_hash=row.prev_hash or "",
        entry_hash=row.entry_hash or "",
    )


@dataclass
class _PendingAuditEvent:
    """An audit event recorded inside a unit of work, not chained yet.

    Holds what :meth:`SqlAlchemyAuditLog.append` was given; number, stamp and
    hashes are taken when it is chained (:meth:`SqlAlchemyAuditLog._chain_locked`).

    :ivar at: an explicit time stamp (seeds), kept as given; ``None`` -- the
        stamp is taken under the chain lock.
    :ivar noted_at: when it was recorded; the time stamp of its provisional
        view (:meth:`provisional`).
    """

    event_type: EventType
    instance_id: str
    schema_id: str
    schema_version: int
    node_id: str | None
    label: str | None
    agent_id: str | None
    detail: dict[str, str]
    at: datetime | None
    noted_at: datetime

    def provisional(self) -> AuditEvent:
        """The event as the unit sees it before the commit: ``seq`` 0 and no
        hashes (both are only known once it is chained)."""

        return AuditEvent(
            seq=0,
            timestamp=self.at or self.noted_at,
            event_type=self.event_type,
            instance_id=self.instance_id,
            schema_id=self.schema_id,
            schema_version=self.schema_version,
            node_id=self.node_id,
            label=self.label,
            agent_id=self.agent_id,
            detail=dict(self.detail),
        )


@dataclass
class _PendingAudit:
    """What one unit of work recorded for one audit log (see
    :meth:`SqlAlchemyAuditLog.append` and :meth:`~SqlAlchemyAuditLog.clear`).

    :ivar events: collected events in the order they were recorded.
    :ivar cleared: the unit cleared the log; done before the events are chained.
    """

    events: list[_PendingAuditEvent] = field(default_factory=list)
    cleared: bool = False


class SqlAlchemyAuditLog(_SqlAlchemyStore):
    """A durable, append-only event log backed by a SQLAlchemy engine.

    Implements the same ``append``/``list_all``/``for_instance`` interface as
    :class:`procworks.audit.InMemoryAuditLog`, so the API records events the
    same way regardless of the backend. Events are returned ordered by ``seq``.

    Appending is serialised across requests and processes by the chain-end row
    (:class:`AuditChainRow`): ``seq`` and ``prev_hash`` are taken from it under
    its lock, inside the transaction of the unit of work, so the chain stays
    one line and the numbers have no gaps (a rolled-back request gives its
    number back).

    **Inside a unit of work the events are collected and chained only right
    before the commit** (:func:`procworks.unit_of_work.before_commit`): the
    chain-end lock is then the unit's last lock and is held only for the
    commit itself. Taken at the first event, it was held for the whole rest
    of the unit -- a migration of thousands of cases or a reset made every
    other writer of every API process wait for seconds (up to the statement
    timeout, 503), and two units that took the case row and the chain end in
    opposite order deadlocked (a completion against an escalation, a start
    against a reset). Reads of the same unit see its collected events
    (:meth:`list_all`, :meth:`for_instance`, ...); outside a unit an event is
    chained at once, in a short transaction of its own.
    """

    def append(
        self,
        event_type: EventType,
        instance_id: str,
        schema_id: str,
        *,
        schema_version: int = 1,
        node_id: str | None = None,
        label: str | None = None,
        agent_id: str | None = None,
        detail: dict[str, str] | None = None,
        at: datetime | None = None,
        immediate: bool = False,
    ) -> AuditEvent:
        """Record one event at the end of the hash chain.

        Inside a unit of work (and without ``immediate``) the event is
        collected and chained right before the unit's commit, together with
        the unit's other events and in the order they were recorded (see the
        class documentation). The returned event is then **provisional**:
        ``seq`` 0, empty ``prev_hash``/``entry_hash``, and the time of the
        call (the stored stamp is taken under the lock, a moment later, so
        the stamps follow the numbers). A rolled-back unit chains nothing.

        Otherwise -- outside a unit, or with ``immediate`` -- the event is
        chained now: lock the chain end (waits while another transaction
        holds it), take the next number and the time stamp, hash, insert,
        move the chain end. Inside a unit this also chains the unit's earlier
        collected events first (order is kept) and holds the lock until the
        commit; outside a unit it is one short transaction of its own.

        The number is one past the recorded end -- or past the newest stored
        event, should a writer without the lock (an older process during an
        update) have added one: the event then still gets a free number, and
        the check reports that writer (``end-mismatch`` / branch).

        :param immediate: chain now although inside a unit -- for the rare
            caller that needs the stored ``entry_hash`` at once (the licence
            time anchor records it).
        :returns: the stored event with ``seq``, ``prev_hash`` and
            ``entry_hash`` -- or the provisional event described above.
        """

        detail = detail or {}
        record = _PendingAuditEvent(
            event_type=event_type,
            instance_id=instance_id,
            schema_id=schema_id,
            schema_version=schema_version,
            node_id=node_id,
            label=label,
            agent_id=agent_id,
            detail=detail,
            at=at,
            noted_at=datetime.now(UTC),
        )
        uow = unit_of_work.current()
        if uow is not None and not immediate:
            self._pending_for_write(uow).events.append(record)
            return record.provisional()
        if uow is not None:
            self._write_pending(uow)
        with self._session() as session:
            prev_hash, head_seq = self._lock_chain_end(session)
            [event] = self._chain_locked(session, prev_hash, head_seq, [record])
            return event

    def _pending(self, uow: unit_of_work.UnitOfWork) -> _PendingAudit | None:
        """What ``uow`` collected for this log (see :meth:`append`), if anything.

        :param uow: the running unit.
        :returns: the collection, or ``None`` when the unit recorded nothing.
        """

        state = uow.store_state.get(self)
        return state if isinstance(state, _PendingAudit) else None

    def _pending_for_write(self, uow: unit_of_work.UnitOfWork) -> _PendingAudit:
        """The collection of ``uow`` -- created on first use.

        Creating it registers its write (:meth:`_write_pending`) as the
        unit's last step before the commit
        (:func:`procworks.unit_of_work.before_commit`), once per unit and log
        (the step's key carries this log's identity, so two logs on the same
        unit each write their own).

        :param uow: the running unit.
        :returns: the collection, kept in ``uow.store_state`` under this log;
            the same object on every later call of the same unit.
        """

        state = self._pending(uow)
        if state is None:
            state = _PendingAudit()
            uow.store_state[self] = state
            unit_of_work.before_commit(
                f"audit-chain:{id(self)}", functools.partial(self._write_pending, uow)
            )
        return state

    def _write_pending(self, uow: unit_of_work.UnitOfWork) -> None:
        """Chain what ``uow`` collected: a pending clear, then its events.

        Runs as the unit's last step before the commit (or earlier, when
        :meth:`append_checked` or an ``immediate`` append needs the chain
        now). Takes the chain-end lock first -- also for a clear, so even a
        reset takes the locks in the same order as every other unit -- and
        writes all collected events in one batch, in the order they were
        recorded. A clear deletes every stored event and the chain end
        first; the collected events then start the new chain.

        :param uow: the running unit whose collection is written; its
            session is the one the rows go into (committed with the unit).
        :returns: nothing. Edge cases: without a collection, or with an empty
            one and no clear, it does nothing (no lock, no statement). It
            empties the collection before writing, so a second call -- the
            before-commit step after an early write -- writes nothing twice;
            if the write fails, the unit rolls back as a whole.
        """

        state = self._pending(uow)
        if state is None or (not state.events and not state.cleared):
            return
        events, cleared = list(state.events), state.cleared
        state.events.clear()
        state.cleared = False
        with self._session() as session:
            prev_hash, head_seq = self._lock_chain_end(session)
            if cleared:
                session.execute(delete(AuditEventRow))
                session.execute(delete(AuditChainRow))
                session.flush()
                prev_hash, head_seq = self._lock_chain_end(session)
            if events:
                self._chain_locked(session, prev_hash, head_seq, events)
            else:
                self._commit(session)

    def append_checked(
        self,
        decide: Callable[[list[AuditEvent], ChainState], dict[str, str]],
        event_type: EventType,
        instance_id: str,
        schema_id: str,
    ) -> AuditEvent:
        """Lock the chain end, let ``decide`` judge the log, append -- in one
        transaction.

        For an event whose content depends on the whole chain (the anchor of
        the chain check): two such appends at the same moment both judged the
        same state and both appended. Here the chain-end lock is taken
        *before* reading, so the second transaction waits until the first
        committed and then reads the log with the first one's event in it
        (PostgreSQL ``READ COMMITTED`` reads anew after the wait; SQLite has
        one writer at a time). Inside a unit of work the lock is held until
        the unit commits.

        :param decide: gets every event and the chain end (read under the
            lock, in this session), returns the ``detail`` of the new event --
            or raises, and nothing is appended (the unit rolls back or, for a
            deliberate answer, commits nothing of this).
        :param event_type: type of the new event.
        :param instance_id: its instance id (a pseudo id for the anchor).
        :param schema_id: its schema id.
        :returns: the appended event.
        """

        uow = unit_of_work.current()
        if uow is not None:
            # The unit's collected events come first -- in the chain and in
            # what ``decide`` reads. The lock is then held until the commit
            # (an anchor request does nothing after it).
            self._write_pending(uow)
        with self._session() as session:
            prev_hash, head_seq = self._lock_chain_end(session)
            rows = session.scalars(select(AuditEventRow).order_by(AuditEventRow.seq))
            events = [_event_from_row(row) for row in rows]
            detail = decide(events, self._chain_state_in(session))
            record = _PendingAuditEvent(
                event_type=event_type,
                instance_id=instance_id,
                schema_id=schema_id,
                schema_version=1,
                node_id=None,
                label=None,
                agent_id=None,
                detail=detail,
                at=None,
                noted_at=datetime.now(UTC),
            )
            [event] = self._chain_locked(session, prev_hash, head_seq, [record])
            return event

    def _chain_locked(
        self,
        session: Session,
        prev_hash: str,
        head_seq: int,
        records: list[_PendingAuditEvent],
    ) -> list[AuditEvent]:
        """Insert ``records`` after the locked end, in order, and move the end.

        Shared by :meth:`append`, :meth:`append_checked` and the write of a
        unit's collected events; the caller holds the chain-end lock in
        ``session`` (:meth:`_lock_chain_end`). The numbers and time stamps are
        taken here, under the lock, so both follow the chain order (an
        explicit ``at`` -- seeds -- is kept as given). One insert batch and
        one update of the end, however many events.

        :param session: the session holding the lock.
        :param prev_hash: hash of the recorded end (the first event links to it).
        :param head_seq: number of the recorded end.
        :param records: the events to chain, at least one.
        :returns: the stored events, in order.
        """

        newest: int | None = session.scalar(select(func.max(AuditEventRow.seq)))
        seq = max(head_seq, newest or 0)
        rows: list[AuditEventRow] = []
        for record in records:
            seq += 1
            timestamp = record.at or datetime.now(UTC)
            entry_hash = chain_hash(
                prev_hash,
                seq=seq,
                timestamp=timestamp,
                event_type=record.event_type,
                instance_id=record.instance_id,
                schema_id=record.schema_id,
                schema_version=record.schema_version,
                node_id=record.node_id,
                label=record.label,
                agent_id=record.agent_id,
                detail=record.detail,
            )
            rows.append(
                AuditEventRow(
                    seq=seq,
                    timestamp=timestamp,
                    event_type=record.event_type.value,
                    instance_id=record.instance_id,
                    schema_id=record.schema_id,
                    schema_version=record.schema_version,
                    node_id=record.node_id,
                    label=record.label,
                    agent_id=record.agent_id,
                    detail=record.detail,
                    prev_hash=prev_hash,
                    entry_hash=entry_hash,
                )
            )
            prev_hash = entry_hash
        session.add_all(rows)
        session.flush()
        session.connection().execute(
            update(AuditChainRow)
            .where(AuditChainRow.id == _CHAIN_ROW_ID)
            .values(head_hash=prev_hash, head_seq=seq)
        )
        self._commit(session)
        if len(rows) == 1:
            session.refresh(rows[0])  # the stamp as the database returns it
        return [_event_from_row(row) for row in rows]

    def _lock_chain_end(self, session: Session) -> tuple[str, int]:
        """Lock the chain-end row and return ``(head_hash, head_seq)``.

        The lock is an ``UPDATE`` that changes nothing, with ``RETURNING``:
        on PostgreSQL it takes the row lock (a second transaction waits, then
        reads the row as the first committed it); on SQLite it is the first
        write of the transaction and takes the database write lock. Both hold
        it until the transaction ends. A plain ``SELECT`` would lock nothing
        on SQLite and was exactly the unlocked read that let two writers link
        to the same end.

        A database without the row (created by ``create_all`` instead of
        Alembic, or emptied by :meth:`clear`) gets it on first use
        (:meth:`_create_chain_end`).
        """

        statement = (
            update(AuditChainRow)
            .where(AuditChainRow.id == _CHAIN_ROW_ID)
            .values(head_seq=AuditChainRow.head_seq)
            .returning(AuditChainRow.head_hash, AuditChainRow.head_seq)
        )
        found = session.connection().execute(statement).one_or_none()
        if found is None:
            self._create_chain_end(session)
            found = session.connection().execute(statement).one()
        return str(found[0] or ""), int(found[1] or 0)

    def _create_chain_end(self, session: Session) -> None:
        """Insert the chain-end row from the newest stored event, if missing.

        :func:`_derived_chain_state` gives its values: the end is the newest
        stored event, and every event so far counts as written before the
        serialisation. Two processes doing this at once insert one row
        (``ON CONFLICT DO NOTHING`` on PostgreSQL and SQLite).
        """

        state = self._derived_chain_state(session)
        values = {
            "id": _CHAIN_ROW_ID,
            "head_hash": state.head_hash,
            "head_seq": state.head_seq,
            "serialized_since": state.serialized_since,
            "historic_events": state.historic_events,
            "historic_digest": state.historic_digest,
        }
        dialect = self._engine.dialect.name
        if dialect in ("postgresql", "sqlite"):
            dialect_insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
            session.connection().execute(
                dialect_insert(AuditChainRow).values(**values).on_conflict_do_nothing()
            )
        else:
            session.connection().execute(insert(AuditChainRow).values(**values))

    @staticmethod
    def _derived_chain_state(session: Session) -> ChainState:
        """The chain end as the stored events give it (no chain-end row yet).

        :returns: the newest event's hash and number, with ``serialized_since``
            at that number -- nothing written so far was serialised -- and the
            record of that historic region (:func:`historic_fingerprint` over
            every stored event; the same lines Alembic ``0017`` writes). An
            empty log has no historic region and no record.
        """

        newest = session.execute(
            select(AuditEventRow.seq, AuditEventRow.entry_hash)
            .order_by(AuditEventRow.seq.desc())
            .limit(1)
        ).one_or_none()
        if newest is None:
            # Nothing historic: every branch is a defect, no record needed.
            return ChainState(head_hash="", head_seq=0, serialized_since=0)
        seq = int(newest[0])
        pairs = session.execute(
            select(AuditEventRow.seq, AuditEventRow.entry_hash)
            .where(AuditEventRow.seq <= seq)
            .order_by(AuditEventRow.seq)
        )
        count, digest = historic_fingerprint(
            (int(found[0]), str(found[1] or "")) for found in pairs
        )
        return ChainState(
            head_hash=str(newest[1] or ""), head_seq=seq, serialized_since=seq,
            historic_events=count, historic_digest=digest,
        )

    def chain_state(self) -> ChainState:
        """Return where the chain ends (see :class:`procworks.audit.ChainState`).

        Read without a lock: a check that reads the events and this state in
        one session (one unit of work) sees a consistent picture; outside a
        unit an append in between shows up as ``end-mismatch`` of that one
        check, never as a stored defect. Without a chain-end row the values
        are derived as :meth:`_create_chain_end` would store them.
        """

        uow = unit_of_work.current()
        state = self._pending(uow) if uow is not None else None
        if state is not None and state.cleared:
            return ChainState(head_hash="", head_seq=0, serialized_since=0)
        with self._session() as session:
            return self._chain_state_in(session)

    def _chain_state_in(self, session: Session) -> ChainState:
        """:meth:`chain_state`, read in ``session`` (e.g. under the lock)."""

        row = session.get(AuditChainRow, _CHAIN_ROW_ID, populate_existing=True)
        if row is None:
            return self._derived_chain_state(session)
        return ChainState(
            head_hash=row.head_hash or "",
            head_seq=row.head_seq,
            serialized_since=row.serialized_since,
            historic_events=row.historic_events,
            historic_digest=row.historic_digest,
        )

    @staticmethod
    def _head_hash(session: Session) -> str:
        row = session.scalars(
            select(AuditEventRow).order_by(AuditEventRow.seq.desc()).limit(1)
        ).first()
        return (row.entry_hash or "") if row is not None else ""

    def _unit_view(self) -> tuple[bool, list[AuditEvent]]:
        """What the running unit changed that is not stored yet.

        :returns: ``(cleared, provisional events)`` -- whether the unit
            cleared the log (stored events then no longer count for it) and
            its collected events in order (see :meth:`append`); ``(False,
            [])`` outside a unit or without either.
        """

        uow = unit_of_work.current()
        state = self._pending(uow) if uow is not None else None
        if state is None:
            return False, []
        return state.cleared, [record.provisional() for record in state.events]

    def list_all(self) -> list[AuditEvent]:
        """Every event in chain order, then the running unit's collected ones.

        Inside a unit of work the unit sees its own events before they are
        stored (see :meth:`append`). Those come last and are
        **provisional**: ``seq`` 0, empty ``prev_hash``/``entry_hash``, the
        time of their recording -- a caller that checks the chain or relies
        on the numbers must look at stored events only (outside a unit, or
        after the commit). If the unit cleared the log, the stored events no
        longer count and only its own come back.

        :returns: stored events ordered by ``seq``, followed by the unit's
            provisional events in recording order; outside a unit only the
            stored ones.
        """

        cleared, pending = self._unit_view()
        if cleared:
            return pending
        with self._session() as session:
            rows = session.scalars(select(AuditEventRow).order_by(AuditEventRow.seq))
            return [_event_from_row(row) for row in rows] + pending

    def for_instance(self, instance_id: str) -> list[AuditEvent]:
        """The events of one instance in chain order, then the unit's collected ones.

        Same rules as :meth:`list_all`, filtered to ``instance_id``: the
        unit's own events of this instance come last and are provisional
        (``seq`` 0, no hashes); after a clear in the unit only those.

        :param instance_id: the case.
        :returns: its stored events ordered by ``seq``, followed by the
            unit's provisional events of it; empty for an unknown case.
        """

        cleared, pending = self._unit_view()
        own = [event for event in pending if event.instance_id == instance_id]
        if cleared:
            return own
        with self._session() as session:
            stmt = (
                select(AuditEventRow)
                .where(AuditEventRow.instance_id == instance_id)
                .order_by(AuditEventRow.seq)
            )
            return [_event_from_row(row) for row in session.scalars(stmt)] + own

    def instance_ids_for_agent(self, agent_id: str) -> set[str]:
        """The instances in which ``agent_id`` acted, in one query.

        Reads only the ``instance_id`` column of the matching rows instead of
        building every event of the history (which the reading right of a
        limited operator did before, on each list request). See
        :meth:`procworks.audit.InMemoryAuditLog.instance_ids_for_agent`.
        The running unit's collected events count as well.
        """

        cleared, pending = self._unit_view()
        own = {event.instance_id for event in pending if event.agent_id == agent_id}
        if cleared:
            return own
        with self._session() as session:
            return own | set(
                session.scalars(
                    select(AuditEventRow.instance_id)
                    .where(AuditEventRow.agent_id == agent_id)
                    .distinct()
                )
            )

    def revision(self) -> int:
        """Return the highest assigned ``seq`` (0 when the log is empty).

        Mirrors :meth:`procworks.audit.InMemoryAuditLog.revision`: a cheap,
        monotonic counter that clients poll to detect new runtime progress.
        Inside a unit its collected events count as well.
        """

        cleared, pending = self._unit_view()
        if cleared:
            return len(pending)
        with self._session() as session:
            value = session.scalar(select(func.max(AuditEventRow.seq)))
            return int(value or 0) + len(pending)

    def head_hash(self) -> str:
        """Return the newest entry's chain hash ("" when the log is empty)."""

        with self._session() as session:
            return self._head_hash(session)

    def max_event_time(self) -> float:
        """Return the newest recorded timestamp as epoch seconds (0.0 if empty).

        A monotone lower bound on real time for the licensing ratchet.
        The explicit annotation matters: since SQLAlchemy 2.1 ``func.max``
        is typed as ``Any``, which ``mypy --strict`` rejects on return.
        Inside a unit its collected events count as well (with the time they
        were recorded -- a lower bound of their stored stamp).
        """

        cleared, pending = self._unit_view()
        newest = max((event.timestamp.timestamp() for event in pending), default=0.0)
        if cleared:
            return newest
        with self._session() as session:
            value: datetime | None = session.scalar(select(func.max(AuditEventRow.timestamp)))
            if value is None:
                return newest
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return max(value.timestamp(), newest)

    def clear(self) -> None:
        """Delete every event and the chain end; the next append starts a new
        chain (number 1, nothing historic).

        Inside a unit of work the deletion is collected like an event and
        done right before the commit, under the chain-end lock (see the class
        documentation): events the unit collected before are dropped, events
        it records afterwards start the new chain, and its reads see the log
        as cleared. Outside a unit it happens at once.
        """

        uow = unit_of_work.current()
        if uow is None:
            self._delete_all(AuditEventRow, AuditChainRow)
            return
        state = self._pending_for_write(uow)
        state.events.clear()
        state.cleared = True


class UserRow(Base):
    """One row per login user (durable part of the password auth backend).

    The unique partial index ``ux_auth_user_agent_id`` holds "one login per
    person" in the database itself (Alembic ``0020``): of two logins created
    for one person at the same moment, only one can be stored. Logins without
    a person (``agent_id`` NULL) are not limited. A database migrated with
    persons that already had several logins keeps them; the migration marks
    all but one of them ``legacy_duplicate``, which leaves them out of the
    index. The mark belongs to the row: a login created later is never
    marked (:meth:`SqlAlchemyCredentialStore.add_user`), one bound to
    another person loses it (:meth:`~SqlAlchemyCredentialStore.put_user`),
    and deleting the indexed login of such a person moves the protection to
    one of the marked ones (:meth:`~SqlAlchemyCredentialStore.delete_user`).
    """

    __tablename__ = "auth_user"
    __table_args__ = (
        Index(
            "ux_auth_user_agent_id",
            "agent_id",
            unique=True,
            postgresql_where=text("agent_id IS NOT NULL AND NOT legacy_duplicate"),
            sqlite_where=text("agent_id IS NOT NULL AND NOT legacy_duplicate"),
        ),
    )

    login: Mapped[str] = mapped_column(String, primary_key=True)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    roles: Mapped[list[str]] = mapped_column(JsonDocument, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    must_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Set only by the migration that introduced the index, for the extra
    #: logins of a person who already had several; such rows stay outside the
    #: index. Never set by the application.
    legacy_duplicate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


def _user_from_row(row: UserRow) -> User:
    return User(
        login=row.login,
        password_hash=row.password_hash,
        subject=row.subject,
        agent_id=row.agent_id,
        roles=frozenset(row.roles),
        display_name=row.display_name,
        must_change=row.must_change,
    )


#: Key of the PostgreSQL advisory lock that keeps every request out while a
#: reset wipes the data (:meth:`SqlAlchemyInstanceStore.hold_write_gate`).
#: A fixed 64-bit number ("PWRESET" in ASCII), never to be changed (see
#: :data:`_ADMIN_LOCK_KEY`).
_WRITE_GATE_KEY = 0x5057_5245_5345_54


def _write_gate_statement(dialect: str, *, exclusive: bool) -> Executable | None:
    """The statement that passes the write gate for the running transaction.

    PostgreSQL: ``pg_advisory_xact_lock_shared`` (an ordinary request -- any
    number pass at once) or ``pg_advisory_xact_lock`` (a reset -- waits until
    every request that passed has ended, and keeps new ones waiting until it
    commits). Both are released by the commit or rollback. PostgreSQL queues
    a new shared request behind a waiting exclusive one, so a reset is not
    starved by a steady stream of requests.

    Any other database has no such lock: ``None`` -- SQLite has one writer at
    a time anyway and serves a single process.

    :param dialect: SQLAlchemy dialect name of the engine.
    :param exclusive: the reset's side of the gate.
    :returns: the statement, or ``None`` when the database has no gate.
    """

    if dialect != "postgresql":
        return None
    if exclusive:
        return select(func.pg_advisory_xact_lock(_WRITE_GATE_KEY))
    return select(func.pg_advisory_xact_lock_shared(_WRITE_GATE_KEY))


#: Key of the PostgreSQL advisory lock that serialises changes to the set of
#: admins (:meth:`SqlAlchemyCredentialStore.lock_admin_changes`). A fixed
#: 64-bit number, chosen once ("PWADMIN" in ASCII); it must never change, or
#: two versions running side by side during an update would not exclude
#: each other.
_ADMIN_LOCK_KEY = 0x5057_4144_4D49_4E


def _admin_lock_statement(dialect: str) -> Executable:
    """The statement that takes the admin-set lock for the running transaction.

    * PostgreSQL: ``pg_advisory_xact_lock`` with :data:`_ADMIN_LOCK_KEY` -- a
      lock of the database, released by the commit or rollback, shared by
      every process on the same database. It locks no row, so logins and
      other user changes are not held up.
    * Any other database (SQLite in tests and single-file setups): an
      ``UPDATE`` of the user table that matches no row. It is the first write
      of the transaction and takes the database write lock, which SQLite
      holds until the transaction ends -- the same trick as the audit chain
      end, because a ``SELECT`` locks nothing there.

    :param dialect: SQLAlchemy dialect name of the engine.
    :returns: the statement to execute in the transaction's connection.
    """

    if dialect == "postgresql":
        return select(func.pg_advisory_xact_lock(_ADMIN_LOCK_KEY))
    return update(UserRow).where(text("1 = 0")).values(login=UserRow.login)


def _user_lock_statements(dialect: str, login: str) -> list[Executable]:
    """The statements that read one login and hold it until the transaction ends.

    Used by :meth:`SqlAlchemyCredentialStore.lock_user`; the last statement
    returns the row (or nothing for an unknown login).

    * PostgreSQL: ``SELECT ... FOR UPDATE`` of the one row in ``auth_user``.
      The lock is the database's, released by commit or rollback and shared
      by every API process; it locks this login only -- logins of other
      people do not wait. An unknown login has no row, so nothing is locked
      (a creation under that name does not wait). Under ``READ COMMITTED`` a waiting ``FOR UPDATE``
      returns the row as the other transaction committed it, not the version
      from before.
    * Any other database (SQLite in tests and single-file setups): SQLite has
      no row locks and ignores ``FOR UPDATE``. An ``UPDATE`` of the row that
      changes nothing comes first; it takes the database write lock, which
      SQLite holds until the transaction ends (the same trick as the admin-set
      lock), and the ``SELECT`` after it reads the committed state.

    :param dialect: SQLAlchemy dialect name of the engine.
    :param login: the login to lock.
    :returns: the statements, to execute in order in the transaction's
        connection.
    """

    read = select(UserRow).where(UserRow.login == login).with_for_update()
    if dialect == "postgresql":
        return [read]
    touch = update(UserRow).where(UserRow.login == login).values(login=UserRow.login)
    return [touch, read]


class SqlAlchemyCredentialStore(_SqlAlchemyStore):
    """A login-user store backed by a SQLAlchemy engine.

    Mirrors the other stores' engine/URL conventions and implements the
    ``get_user``/``put_user``/``list_users``/``delete_user`` interface, so the
    password backend is agnostic of the backend. Sessions are intentionally
    *not* persisted here -- they are ephemeral in-memory state of the backend.
    """

    def get_user(self, login: str) -> User | None:
        with self._session() as session:
            row = session.get(UserRow, login)
            return _user_from_row(row) if row is not None else None

    def lock_user(self, login: str) -> User | None:
        """Read ``login`` under a lock that lasts until the unit of work ends.

        See :meth:`procworks.auth_password.CredentialStore.lock_user` and
        :func:`_user_lock_statements`. The row is loaded into the session
        afresh (``populate_existing``): a following :meth:`put_user` of the
        same unit then compares against the locked, current values, so the
        columns it writes are exactly those that differ from what is stored
        now -- never from an older read.

        Outside a unit of work the lock ends with this method's own session,
        i.e. at once -- callers must hold a unit (the API runs every store
        part of a login, password change, reset and deletion as one).

        :param login: the login name.
        :returns: the login, or ``None`` when unknown -- then nothing is
            locked on PostgreSQL (``FOR UPDATE`` finds no row; see the
            protocol for why that is enough).
        """

        with self._session() as session:
            *takes, read = _user_lock_statements(self._engine.dialect.name, login)
            for statement in takes:
                session.connection().execute(statement)
            row = session.scalars(
                read.execution_options(populate_existing=True)
            ).first()
            return _user_from_row(row) if row is not None else None

    def add_user(self, user: User) -> User:
        """Insert a *new* login -- unless its name or its person is taken.

        ``INSERT ... ON CONFLICT DO NOTHING ... RETURNING login`` (PostgreSQL
        and SQLite): without a conflict target it covers the primary key and
        the unique person index, and a concurrent creation for the same
        person waits for the other transaction, then finds the person taken.
        The result comes from ``RETURNING``, never from ``rowcount``.

        :raises LoginConflict: the login name or the person is taken.
        """

        values = {
            "login": user.login,
            "subject": user.subject,
            "password_hash": user.password_hash,
            "agent_id": user.agent_id,
            "roles": sorted(user.roles),
            "display_name": user.display_name,
            "must_change": user.must_change,
            # A new login is always under the index -- also under the name of
            # a deleted legacy login.
            "legacy_duplicate": False,
        }
        dialect = self._engine.dialect.name
        with self._session() as session:
            if dialect in ("postgresql", "sqlite"):
                dialect_insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
                added = session.execute(
                    dialect_insert(UserRow)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(UserRow.login)
                ).scalar_one_or_none()
            else:
                taken = session.get(UserRow, user.login) is not None or (
                    user.agent_id is not None
                    and session.scalar(
                        select(UserRow.login).where(UserRow.agent_id == user.agent_id)
                    ) is not None
                )
                added = None if taken else session.execute(
                    insert(UserRow).values(**values).returning(UserRow.login)
                ).scalar_one_or_none()
            if added is None:
                raise LoginConflict(user.login)
            self._commit(session)
        return user

    def duplicate_bindings(self) -> dict[str, list[str]]:
        """Persons with more than one login: ``{agent_id: sorted logins}``.

        Left over from versions before the unique index; nothing removes
        them automatically. The API names them in its log at start.
        """

        with self._session() as session:
            rows = session.execute(
                select(UserRow.agent_id, UserRow.login).where(UserRow.agent_id.is_not(None))
            ).all()
        by_agent: dict[str, list[str]] = {}
        for agent_id, login in rows:
            by_agent.setdefault(str(agent_id), []).append(str(login))
        return {agent: sorted(logins) for agent, logins in sorted(by_agent.items())
                if len(logins) > 1}

    def put_user(self, user: User) -> User:
        """Store ``user`` (create or replace the row of its login).

        A legacy duplicate (see :class:`UserRow`) bound to another person
        loses its mark -- the exemption covered the old binding only, and
        under the new one the index applies (a person who already has a
        login makes this fail with ``IntegrityError``).
        """

        with self._session() as session:
            row = _get_or_add(session, UserRow, user.login, key_column="login")
            if row.agent_id != user.agent_id:
                row.legacy_duplicate = False
            row.subject = user.subject
            row.password_hash = user.password_hash
            row.agent_id = user.agent_id
            row.roles = sorted(user.roles)
            row.display_name = user.display_name
            row.must_change = user.must_change
            self._commit(session)
        return user

    def list_users(self) -> list[User]:
        with self._session() as session:
            return [_user_from_row(row) for row in session.scalars(select(UserRow))]

    def lock_admin_changes(self) -> None:
        """Serialise changes to the set of admins until the transaction ends.

        See :meth:`procworks.auth_password.CredentialStore.lock_admin_changes`.
        Taken in the session of the running unit of work, so the lock lasts
        until the unit commits or rolls back -- across API processes, because
        the database holds it (:func:`_admin_lock_statement`). A second caller
        waits here; its following reads are new statements and see the first
        caller's committed deletion (``READ COMMITTED``).

        Outside a unit of work the lock ends with this method's own session,
        i.e. at once -- callers must hold a unit (every mutating endpoint
        does).
        """

        with self._session() as session:
            session.connection().execute(_admin_lock_statement(self._engine.dialect.name))

    def delete_user(self, login: str) -> None:
        """Delete the login ``login`` (no-op when unknown).

        When it was the one login of its person under the index while
        marked legacy duplicates of that person remain, the alphabetically
        first of them takes over in the same transaction -- otherwise the
        person could be given yet another login.
        """

        with self._session() as session:
            row = session.get(UserRow, login)
            if row is None:
                return
            agent_id, was_indexed = row.agent_id, not row.legacy_duplicate
            session.delete(row)
            if agent_id is not None and was_indexed:
                session.flush()
                successor = session.scalars(
                    select(UserRow)
                    .where(UserRow.agent_id == agent_id, UserRow.legacy_duplicate.is_(True))
                    .order_by(UserRow.login)
                    .limit(1)
                ).first()
                if successor is not None:
                    successor.legacy_duplicate = False
            self._commit(session)


class SessionRow(Base):
    """One login session (password mode); keyed by the token's SHA-256 digest.

    Sessions live in the database, so a restart or update does not log
    everybody out. The clear token is never stored -- only its digest.
    """

    __tablename__ = "auth_session"

    digest: Mapped[str] = mapped_column(String, primary_key=True)
    login: Mapped[str] = mapped_column(String, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemySessionStore(_SqlAlchemyStore):
    """Login sessions in the database (``auth_session``), see ``SessionStore``.

    Same engine/URL conventions as the credential store. ``expires_at`` is
    stored timezone-aware; SQLite returns it naive, so it is re-attached as UTC
    on read.
    """

    def get(self, digest: str) -> _SessionInfo | None:
        with self._session() as session:
            row = session.get(SessionRow, digest)
            if row is None:
                return None
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            return _SessionInfo(login=row.login, expires_at=expires)

    def put(self, digest: str, info: _SessionInfo) -> None:
        with self._session() as session:
            session.merge(SessionRow(digest=digest, login=info.login, expires_at=info.expires_at))
            self._commit(session)

    def delete(self, digest: str) -> None:
        with self._session() as session:
            session.execute(delete(SessionRow).where(SessionRow.digest == digest))
            self._commit(session)

    def delete_for_login(self, login: str) -> None:
        with self._session() as session:
            session.execute(delete(SessionRow).where(SessionRow.login == login))
            self._commit(session)

    def purge_expired(self, now: datetime) -> None:
        with self._session() as session:
            session.execute(delete(SessionRow).where(SessionRow.expires_at <= now))
            self._commit(session)


class IdClaimRow(Base):
    """One row per generated id that an API process has reserved (``id_claim``).

    The primary key makes the reservation atomic across processes: of two
    concurrent inserts of the same id, the database accepts exactly one.
    ``claimed_at`` is informational only (diagnosis), nothing reads it.
    """

    __tablename__ = "id_claim"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyIdClaimStore(_SqlAlchemyStore):
    """Shared id reservations in the database, see ``store.IdClaimStore``.

    :meth:`claim` is an INSERT -- deliberately **not** the upsert pattern of the
    other stores (:func:`_get_or_add`), which would let every caller "win".
    On PostgreSQL and SQLite it is ``INSERT ... ON CONFLICT DO NOTHING``: a
    collision is expected under normal operation (several processes, or a
    restart over reserved ids) and must not show up as an error in the database
    log. Other dialects fall back to a plain INSERT and treat a unique
    violation as "taken". Any other database error propagates and aborts the
    allocation, so a failing database never leads to an unguarded id.

    Whether the INSERT won is read from ``RETURNING id``, never from the
    driver's ``rowcount``: with SQLAlchemy 2.1 and psycopg 3 that count is
    ``-1`` for this statement even when the row was inserted. A claim decided
    by ``rowcount`` then reported every fresh id as "taken", and
    :meth:`procworks.ids.IdSequence.new` stepped through numbers without end
    -- no instance, schema, template or org model could be created any more.
    """

    def claim(self, key: str) -> bool:
        """Insert ``key``; ``True`` if inserted, ``False`` if it already existed.

        :param key: the generated id, e.g. ``instance_26``.
        :returns: whether this call reserved the id. On PostgreSQL/SQLite the
            answer is the row the INSERT returned (``RETURNING id``): a row
            means this call inserted it, no row means ``ON CONFLICT DO
            NOTHING`` skipped it because the id was already reserved. This
            does not depend on ``rowcount``, which some driver/SQLAlchemy
            combinations report as ``-1`` (unknown) for such an INSERT.
        :raises sqlalchemy.exc.SQLAlchemyError: for any failure other than a
            duplicate key (connection lost, table missing ...).
        """

        values = {"id": key, "claimed_at": datetime.now(UTC)}
        dialect = self._engine.dialect.name
        with self._session() as session:
            if dialect in ("postgresql", "sqlite"):
                insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
                statement = (
                    insert(IdClaimRow)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(IdClaimRow.id)
                )
                inserted = session.connection().execute(statement).scalar_one_or_none()
                self._commit(session)
                return inserted is not None
            if session.get(IdClaimRow, key) is not None:
                return False
            session.add(IdClaimRow(**values))
            try:
                self._commit(session)
            except IntegrityError:
                if unit_of_work.current() is not None:
                    # Inside a unit of work a rollback would undo the whole
                    # request; the unit ends with the error instead.
                    raise
                session.rollback()
                return False
            return True

    def list_ids(self) -> list[str]:
        """Return every reserved id, for the start-up lift of the counters.

        A reservation whose object was never saved (failed validation, aborted
        request) is invisible to the stores; without this the counters of a
        restarted process would step through those numbers one failed claim at
        a time.
        """

        with self._session() as session:
            return list(session.scalars(select(IdClaimRow.id)))

    def clear(self) -> None:
        """Delete every reservation. Only for tests -- live data never needs it."""

        self._delete_all(IdClaimRow)


#: SQL condition "the task still occupies its step" -- the open states of
#: :data:`procworks.model.OPEN_EXTERNAL_TASK_STATES`, sorted for a stable DDL.
#: Alembic ``0015`` creates the same index with the same condition.
_OPEN_TASK_CONDITION = "state IN ({})".format(
    ", ".join(f"'{state.value}'" for state in sorted(OPEN_EXTERNAL_TASK_STATES))
)


class IdempotencyKeyRow(Base):
    """One reserved ``Idempotency-Key`` with a reference to its request's result.

    ``key_digest`` is an HMAC over identity and key, ``fingerprint`` an HMAC
    over the request (:func:`procworks.idempotency.keyed_digest`): fixed
    length whatever a client sends, nothing stored in plain, and without the
    server secret a guessed key or body cannot be confirmed. ``object_id``
    and ``status`` name what the first answer showed -- never the answer
    itself (data minimisation); both are ``NULL`` while the key is reserved
    but its request not yet done. ``expires_at`` ends the replay (Alembic
    ``0019``, reshaped by ``0021``).
    """

    __tablename__ = "idempotency_key"

    key_digest: Mapped[str] = mapped_column(String, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    object_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ServerSecretRow(Base):
    """A random secret of this installation, shared by all API processes.

    Today one row, ``idempotency``: the HMAC secret of the idempotency keys
    when ``PROCWORKS_IDEMPOTENCY_SECRET`` is not set. Created by the
    migration (Alembic ``0021``) or, for a store that creates its tables, at
    its construction; never shown by the API, never part of a reset.
    """

    __tablename__ = "server_secret"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


#: Name of the idempotency secret in ``server_secret``.
IDEMPOTENCY_SECRET_NAME = "idempotency"


class _SqlIdempotencyReservation:
    """A key reserved by a row of the request's transaction (see the store)."""

    def __init__(
        self,
        store: SqlAlchemyIdempotencyStore,
        digest: str,
        previous: Replay | None,
    ) -> None:
        self._store = store
        self._digest = digest
        self.previous = previous

    def remember(self, object_id: str, status: int) -> None:
        """Write the reference into the reserved row -- same transaction as the
        operation, so it becomes visible with the operation's commit."""

        with self._store._session() as session:
            session.execute(
                update(IdempotencyKeyRow)
                .where(IdempotencyKeyRow.key_digest == self._digest)
                .values(object_id=object_id, status=status)
                .execution_options(synchronize_session=False)
            )
            self._store._commit(session)

    def abandon(self) -> None:
        """Remove the reservation: the operation failed, the key stays free.

        Needed because a deliberate refusal (an ``HTTPException`` of the
        operation) commits the unit's earlier writes -- the reserved row
        would otherwise stay without a reference. A database error here is
        swallowed: the unit already failed and rolls the row back anyway.
        """

        if self.previous is not None:
            return
        try:
            with self._store._session() as session:
                session.execute(
                    delete(IdempotencyKeyRow).where(
                        IdempotencyKeyRow.key_digest == self._digest
                    )
                )
                self._store._commit(session)
        except SQLAlchemyError:
            logger.debug("idempotency reservation not removed (unit fails anyway)",
                         exc_info=True)

    def close(self, committed: bool) -> None:
        """Nothing to release: the row lives and dies with the transaction."""


class SqlAlchemyIdempotencyStore(_SqlAlchemyStore):
    """Idempotency keys in the database -- effective across API processes.

    :meth:`reserve` inserts the key's row inside the request's unit of work
    (``INSERT ... ON CONFLICT DO NOTHING ... RETURNING``, result from
    ``RETURNING``, never ``rowcount``). A concurrent request with the same
    key waits at its insert until the first transaction ended: after a commit
    it finds the row and replays the reference, after a rollback its insert
    goes through. An expired row is deleted first, so the key is free again
    after :data:`procworks.idempotency.TTL_SECONDS`. The API's housekeeping
    timer removes the expired rows of other keys (:meth:`remove_expired`,
    on PostgreSQL with ``SKIP LOCKED``, so two processes never wait on each
    other) -- also when no new key arrives.

    The HMAC secret (:meth:`_secret`): ``PROCWORKS_IDEMPOTENCY_SECRET`` when
    set, else the installation's random secret in ``server_secret``.

    :param secret: a fixed secret (tests); default as described.
    """

    def __init__(
        self, url: str, *, create_tables: bool = False, secret: bytes | None = None
    ) -> None:
        super().__init__(url, create_tables=create_tables)
        self._fixed_secret = secret or secret_from_env()
        self._stored_secret: bytes | None = None
        if create_tables and self._fixed_secret is None:
            # Outside any unit of work: the row is committed right away, so
            # every process that starts later reads the same secret.
            self._secret()

    def _secret(self) -> bytes:
        """The HMAC secret (see the class documentation).

        The stored secret is read once per process and cached. A database
        without the row (it is created by the migration) gets one: inserted
        without overwriting a concurrent one, then read back -- and not
        cached, because inside a unit of work the insert only counts once
        the unit commits.

        :returns: the secret as bytes.
        """

        if self._fixed_secret is not None:
            return self._fixed_secret
        if self._stored_secret is not None:
            return self._stored_secret
        with self._session() as session:
            value = session.execute(
                select(ServerSecretRow.value).where(
                    ServerSecretRow.name == IDEMPOTENCY_SECRET_NAME
                )
            ).scalar_one_or_none()
            if value is not None:
                self._stored_secret = str(value).encode("utf-8")
                return self._stored_secret
            dialect = self._engine.dialect.name
            fresh = {"name": IDEMPOTENCY_SECRET_NAME, "value": secrets.token_hex(32)}
            if dialect in ("postgresql", "sqlite"):
                dialect_insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
                session.execute(
                    dialect_insert(ServerSecretRow).values(**fresh).on_conflict_do_nothing()
                )
            else:
                session.execute(insert(ServerSecretRow).values(**fresh))
            value = session.execute(
                select(ServerSecretRow.value).where(
                    ServerSecretRow.name == IDEMPOTENCY_SECRET_NAME
                )
            ).scalar_one()
            self._commit(session)
            if unit_of_work.current() is None:
                self._stored_secret = str(value).encode("utf-8")
            return str(value).encode("utf-8")

    def _digest(self, subject: str, key: str) -> str:
        """HMAC of identity and key (the row's primary key)."""

        return keyed_digest(self._secret(), subject, key)

    def reserve(self, subject: str, key: str, fingerprint: str) -> _SqlIdempotencyReservation:
        """Reserve the key in the request's transaction, or find its reference.

        :returns: a fresh reservation (``previous`` ``None``) or the
            :class:`~procworks.idempotency.Replay` of the committed request.
        """

        from procworks.idempotency import TTL_SECONDS

        secret = self._secret()
        digest = keyed_digest(secret, subject, key)
        request_digest = keyed_digest(secret, fingerprint)
        now = datetime.now(UTC)
        dialect = self._engine.dialect.name
        with self._session() as session:
            session.execute(
                delete(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.key_digest == digest,
                    IdempotencyKeyRow.expires_at <= now,
                )
            )
            values = {
                "key_digest": digest,
                "fingerprint": request_digest,
                "object_id": None,
                "status": None,
                "expires_at": now + timedelta(seconds=TTL_SECONDS),
            }
            for _ in range(3):
                if dialect in ("postgresql", "sqlite"):
                    dialect_insert = (
                        postgresql_insert if dialect == "postgresql" else sqlite_insert
                    )
                    inserted = session.execute(
                        dialect_insert(IdempotencyKeyRow)
                        .values(**values)
                        .on_conflict_do_nothing()
                        .returning(IdempotencyKeyRow.key_digest)
                    ).scalar_one_or_none()
                elif session.get(IdempotencyKeyRow, digest) is None:
                    inserted = session.execute(
                        insert(IdempotencyKeyRow)
                        .values(**values)
                        .returning(IdempotencyKeyRow.key_digest)
                    ).scalar_one_or_none()
                else:
                    inserted = None
                if inserted is not None:
                    self._commit(session)
                    return _SqlIdempotencyReservation(self, digest, None)
                found = session.execute(
                    select(
                        IdempotencyKeyRow.fingerprint,
                        IdempotencyKeyRow.object_id,
                        IdempotencyKeyRow.status,
                    ).where(IdempotencyKeyRow.key_digest == digest)
                ).first()
                if found is None:
                    continue  # removed in between (expired): reserve again
                if found.object_id is None or found.status is None:
                    # Committed without a reference -- never written so; treat
                    # the key as free rather than blocking it for a day.
                    session.execute(
                        delete(IdempotencyKeyRow).where(IdempotencyKeyRow.key_digest == digest)
                    )
                    continue
                return _SqlIdempotencyReservation(
                    self,
                    digest,
                    Replay(
                        hmac.compare_digest(str(found.fingerprint), request_digest),
                        str(found.object_id),
                        int(found.status),
                    ),
                )
        raise RuntimeError("idempotency key could neither be reserved nor read")

    def remove_expired(self) -> int:
        """Delete expired rows (a batch on PostgreSQL, skipping locked ones).

        :returns: how many rows were deleted.
        """

        now = datetime.now(UTC)
        with self._session() as session:
            if self._engine.dialect.name == "postgresql":
                victims = (
                    select(IdempotencyKeyRow.key_digest)
                    .where(IdempotencyKeyRow.expires_at <= now)
                    .limit(5000)
                    .with_for_update(skip_locked=True)
                    .scalar_subquery()
                )
                statement = delete(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.key_digest.in_(victims)
                )
            else:
                statement = delete(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.expires_at <= now
                )
            removed = session.execute(
                statement.returning(IdempotencyKeyRow.key_digest)
            ).all()
            self._commit(session)
            return len(removed)

    def clear(self) -> None:
        """Forget every key (``/admin/reset``); the secret stays."""

        self._delete_all(IdempotencyKeyRow)


class ExternalTaskRow(Base):
    """One row per external task (outbound integration queue, roadmap E11).

    ``lock_version`` counts the saves for the conditional save
    (:func:`_write_versioned`); the partial unique index
    ``uq_external_task_open_step`` lets a step hold at most one open task
    (Alembic ``0015``) -- two transactions creating the task of the same step
    can no longer both succeed.
    """

    __tablename__ = "external_task"
    __table_args__ = (
        Index(
            "uq_external_task_open_step",
            "instance_id",
            "node_id",
            unique=True,
            postgresql_where=text(_OPEN_TASK_CONDITION),
            sqlite_where=text(_OPEN_TASK_CONDITION),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    topic: Mapped[str] = mapped_column(String, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String, nullable=False, index=True)
    available_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class IncidentRow(Base):
    """One row per raised incident (dead-letter of an exhausted task)."""

    __tablename__ = "incident"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    external_task_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyExternalTaskStore(_SqlAlchemyStore):
    """An external-task store backed by a SQLAlchemy engine.

    Mirrors the other stores: each task/incident is a JSON document plus a few
    queryable columns, and the runtime is agnostic of the backend. Tasks are
    saved conditionally and a step holds at most one open task (see
    :class:`procworks.store.ExternalTaskStore`); on PostgreSQL the second of
    two concurrent saves of one task waits for the first transaction and
    then finds the version moved on.
    """

    @staticmethod
    def _values(task: ExternalTask) -> dict[str, object]:
        """The columns of ``task`` besides ``id`` and ``lock_version``."""

        return {
            "instance_id": task.instance_id,
            "node_id": task.node_id,
            "topic": task.topic,
            "state": task.state.value,
            "available_at": task.available_at,
            "document": task.model_dump(mode="json"),
        }

    def add(self, task: ExternalTask) -> bool:
        """See :meth:`procworks.store.ExternalTaskStore.add`."""

        if stored_version(task) is not None:
            return False
        return _write_versioned(self, ExternalTaskRow, task, self._values(task))

    def put(self, task: ExternalTask) -> ExternalTask:
        """See :meth:`procworks.store.ExternalTaskStore.put`."""

        _save_versioned(self, ExternalTaskRow, "external_task", task, self._values(task))
        return task

    def put_if_unchanged(self, task: ExternalTask) -> bool:
        """See :meth:`procworks.store.ExternalTaskStore.put_if_unchanged`."""

        return _write_versioned(self, ExternalTaskRow, task, self._values(task))

    @staticmethod
    def _loaded(document: dict[str, object], version: int) -> ExternalTask:
        task = ExternalTask.model_validate(document)
        remember_version(task, int(version))
        return task

    def get(self, task_id: str) -> ExternalTask | None:
        """Load a task together with its stored version (``None`` if absent).

        Reads the columns directly (no ORM object in the identity map), so a
        later read in the same unit of work sees the unit's own updates.
        """

        with self._session() as session:
            row = session.execute(
                select(ExternalTaskRow.document, ExternalTaskRow.lock_version).where(
                    ExternalTaskRow.id == task_id
                )
            ).first()
        return self._loaded(row.document, row.lock_version) if row is not None else None

    def list_tasks(self) -> list[ExternalTask]:
        """All tasks, each with its stored version (see :meth:`get`)."""

        with self._session() as session:
            rows = session.execute(
                select(ExternalTaskRow.document, ExternalTaskRow.lock_version)
            ).all()
        return [self._loaded(row.document, row.lock_version) for row in rows]

    def put_incident(self, incident: Incident) -> Incident:
        payload = incident.model_dump(mode="json")
        with self._session() as session:
            row = _get_or_add(session, IncidentRow, incident.id)
            row.external_task_id = incident.external_task_id
            row.instance_id = incident.instance_id
            row.resolved = incident.resolved
            row.document = payload
            self._commit(session)
        return incident

    def get_incident(self, incident_id: str) -> Incident | None:
        return self._load(IncidentRow, incident_id, Incident)

    def list_incidents(self) -> list[Incident]:
        with self._session() as session:
            rows = session.scalars(select(IncidentRow))
            return [Incident.model_validate(row.document) for row in rows]

    def clear(self) -> None:
        self._delete_all(ExternalTaskRow, IncidentRow)


class WebhookSubscriptionRow(Base):
    """One row per webhook subscription (event side of the open API, E13)."""

    __tablename__ = "webhook_subscription"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class OutboxEntryRow(Base):
    """One row per queued webhook delivery (transactional outbox, E13)."""

    __tablename__ = "webhook_outbox"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String, nullable=False, index=True)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class WebhookDeliveryRow(Base):
    """One row per delivery attempt (append-only delivery log, E13)."""

    __tablename__ = "webhook_delivery"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    outbox_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


def _claim_outbox_row[EntryT: (OutboxEntry, MailOutboxEntry)](
    store: _SqlAlchemyStore,
    row_type: type[OutboxEntryRow] | type[MailOutboxEntryRow],
    model: type[EntryT],
    due_states: frozenset[str],
    entry_id: str,
    *,
    now: float,
    lease_until: float,
) -> EntryT | None:
    """Reserve a due webhook or mail outbox entry for exactly one caller.

    Reads the row's ``state`` and ``next_attempt_at`` columns; if it is due
    (a state in ``due_states`` and ``next_attempt_at <= now``), it counts the
    attempt and moves ``next_attempt_at`` to ``lease_until`` with ``UPDATE ...
    WHERE id = :id AND state = :seen_state AND next_attempt_at = :seen_next
    RETURNING id``. On PostgreSQL the second of two such updates waits for
    the first transaction and then finds the row changed, on SQLite the
    writes are serialised -- either way only one caller gets a row back.
    Every change of a reservable row changes ``state`` or moves
    ``next_attempt_at`` (a new lease always ends later than the expired one),
    so the pair is a reliable version. Whether the row was written is read
    from ``RETURNING``, never from the driver's ``rowcount``.

    No schema change was needed: a reservation is "not due before the end of
    the lease", which also older versions of the dispatcher respect.

    :param store: the store (for its session).
    :param row_type: :class:`OutboxEntryRow` or :class:`MailOutboxEntryRow`.
    :param model: the matching entry model.
    :param due_states: state values in which an entry may still be delivered.
    :param entry_id: the entry to reserve.
    :param now: the caller's clock.
    :param lease_until: end of the reservation.
    :returns: the reserved entry, or ``None`` (unknown, not due, or lost).
    """

    with store._session() as session:
        seen = session.execute(
            select(row_type.state, row_type.next_attempt_at, row_type.document).where(
                row_type.id == entry_id
            )
        ).first()
        if seen is None or seen.state not in due_states or seen.next_attempt_at > now:
            return None
        # The columns decide; the document follows them.
        claimed = model.model_validate({**seen.document, "state": seen.state})
        claimed.next_attempt_at = lease_until
        claimed.attempts += 1
        written = session.execute(
            update(row_type)
            .where(
                row_type.id == entry_id,
                row_type.state == seen.state,
                row_type.next_attempt_at == seen.next_attempt_at,
            )
            .values(
                next_attempt_at=lease_until,
                document=claimed.model_dump(mode="json"),
            )
            .returning(row_type.id)
            .execution_options(synchronize_session=False)
        ).first()
        if written is None:
            return None
        store._commit(session)
    return claimed


def _finish_outbox_row(
    store: _SqlAlchemyStore,
    row_type: type[OutboxEntryRow] | type[MailOutboxEntryRow],
    due_states: frozenset[str],
    entry: OutboxEntry | MailOutboxEntry,
    *,
    lease_until: float,
) -> bool:
    """Store the outcome of a reserved delivery if the caller still holds the lease.

    ``UPDATE ... WHERE id = :id AND state IN due_states AND next_attempt_at =
    :lease_until RETURNING id``: a holder whose lease ran out and was taken
    over finds ``next_attempt_at`` moved and is refused.

    :param store: the store (for its session).
    :param row_type: :class:`OutboxEntryRow` or :class:`MailOutboxEntryRow`.
    :param due_states: state values of a still reserved entry.
    :param entry: the entry carrying its outcome.
    :param lease_until: the lease the caller reserved it with.
    :returns: whether the outcome was stored.
    """

    with store._session() as session:
        written = session.execute(
            update(row_type)
            .where(
                row_type.id == entry.id,
                row_type.state.in_(due_states),
                row_type.next_attempt_at == lease_until,
            )
            .values(
                state=entry.state.value,
                next_attempt_at=entry.next_attempt_at,
                document=entry.model_dump(mode="json"),
            )
            .returning(row_type.id)
            .execution_options(synchronize_session=False)
        ).first()
        if written is None:
            return False
        store._commit(session)
    return True


_DUE_OUTBOX_VALUES = frozenset(state.value for state in DUE_OUTBOX_STATES)
_DUE_MAIL_OUTBOX_VALUES = frozenset(state.value for state in DUE_MAIL_OUTBOX_STATES)


class SqlAlchemyWebhookStore(_SqlAlchemyStore):
    """A webhook store backed by a SQLAlchemy engine.

    Mirrors the other stores: subscriptions, outbox entries and deliveries are
    each a JSON document plus a few queryable columns, and the dispatcher is
    agnostic of the backend.
    """

    def put_subscription(self, sub: WebhookSubscription) -> WebhookSubscription:
        payload = sub.model_dump(mode="json")
        with self._session() as session:
            row = _get_or_add(session, WebhookSubscriptionRow, sub.id)
            row.url = sub.url
            row.active = sub.active
            row.document = payload
            self._commit(session)
        return sub

    def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        return self._load(WebhookSubscriptionRow, subscription_id, WebhookSubscription)

    def list_subscriptions(self) -> list[WebhookSubscription]:
        with self._session() as session:
            rows = session.scalars(select(WebhookSubscriptionRow))
            return [WebhookSubscription.model_validate(row.document) for row in rows]

    def delete_subscription(self, subscription_id: str) -> None:
        self._delete_row(WebhookSubscriptionRow, subscription_id)

    def put_entry(self, entry: OutboxEntry) -> OutboxEntry:
        payload = entry.model_dump(mode="json")
        with self._session() as session:
            row = _get_or_add(session, OutboxEntryRow, entry.id)
            row.subscription_id = entry.subscription_id
            row.event_type = entry.event_type
            row.state = entry.state.value
            row.next_attempt_at = entry.next_attempt_at
            row.document = payload
            self._commit(session)
        return entry

    def get_entry(self, entry_id: str) -> OutboxEntry | None:
        return self._load(OutboxEntryRow, entry_id, OutboxEntry)

    def list_entries(self) -> list[OutboxEntry]:
        with self._session() as session:
            rows = session.scalars(select(OutboxEntryRow))
            return [OutboxEntry.model_validate(row.document) for row in rows]

    def claim_entry(
        self, entry_id: str, *, now: float, lease_until: float
    ) -> OutboxEntry | None:
        """See :meth:`procworks.store.WebhookStore.claim_entry` and
        :func:`_claim_outbox_row`."""

        return _claim_outbox_row(
            self, OutboxEntryRow, OutboxEntry, _DUE_OUTBOX_VALUES, entry_id,
            now=now, lease_until=lease_until,
        )

    def finish_entry(self, entry: OutboxEntry, *, lease_until: float) -> bool:
        """See :meth:`procworks.store.WebhookStore.finish_entry` and
        :func:`_finish_outbox_row`."""

        return _finish_outbox_row(
            self, OutboxEntryRow, _DUE_OUTBOX_VALUES, entry, lease_until=lease_until
        )

    def discard_entry(self, entry_id: str, *, now: float, reason: str) -> bool:
        """See :meth:`procworks.store.WebhookStore.discard_entry`.

        ``UPDATE ... WHERE id = :id AND state = :seen_state AND
        next_attempt_at = :seen_next RETURNING id`` on the row as just read
        (the same version check as a reservation), repeated a few times if a
        dispatcher changed the row in between. A reserved row is discarded
        too: its holder's later :func:`_finish_outbox_row` then finds the
        state changed and is refused.
        """

        for _ in range(5):
            with self._session() as session:
                seen = session.execute(
                    select(
                        OutboxEntryRow.state,
                        OutboxEntryRow.next_attempt_at,
                        OutboxEntryRow.document,
                    ).where(OutboxEntryRow.id == entry_id)
                ).first()
                if seen is None or seen.state not in _DUE_OUTBOX_VALUES:
                    return False
                closed = OutboxEntry.model_validate({**seen.document, "state": seen.state})
                closed.state = OutboxState.DISCARDED
                closed.next_attempt_at = now
                closed.last_error = reason
                written = session.execute(
                    update(OutboxEntryRow)
                    .where(
                        OutboxEntryRow.id == entry_id,
                        OutboxEntryRow.state == seen.state,
                        OutboxEntryRow.next_attempt_at == seen.next_attempt_at,
                    )
                    .values(
                        state=closed.state.value,
                        next_attempt_at=now,
                        document=closed.model_dump(mode="json"),
                    )
                    .returning(OutboxEntryRow.id)
                    .execution_options(synchronize_session=False)
                ).first()
                if written is not None:
                    self._commit(session)
                    return True
        return False

    def put_delivery(self, delivery: WebhookDelivery) -> WebhookDelivery:
        payload = delivery.model_dump(mode="json")
        with self._session() as session:
            row = _get_or_add(session, WebhookDeliveryRow, delivery.id)
            row.subscription_id = delivery.subscription_id
            row.outbox_id = delivery.outbox_id
            row.at = delivery.at
            row.document = payload
            self._commit(session)
        return delivery

    def list_deliveries(
        self, subscription_id: str | None = None
    ) -> list[WebhookDelivery]:
        with self._session() as session:
            stmt = select(WebhookDeliveryRow).order_by(WebhookDeliveryRow.at)
            if subscription_id is not None:
                stmt = stmt.where(
                    WebhookDeliveryRow.subscription_id == subscription_id
                )
            rows = session.scalars(stmt)
            return [WebhookDelivery.model_validate(row.document) for row in rows]

    def clear(self) -> None:
        self._delete_all(WebhookDeliveryRow, OutboxEntryRow, WebhookSubscriptionRow)


class MailOutboxEntryRow(Base):
    """One row per queued modelled e-mail notification (mail outbox, rule N)."""

    __tablename__ = "mail_outbox"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String, nullable=False, index=True)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyMailOutboxStore(_SqlAlchemyStore):
    """A durable mail outbox backed by a SQLAlchemy engine.

    Mirrors the webhook outbox: each queued notification is a JSON document plus
    a few queryable columns (``dedup_key`` for idempotency lookups, ``state`` and
    ``next_attempt_at`` for the dispatcher). The dispatcher stays backend-agnostic.
    """

    def put_entry(self, entry: MailOutboxEntry) -> MailOutboxEntry:
        payload = entry.model_dump(mode="json")
        with self._session() as session:
            row = _get_or_add(session, MailOutboxEntryRow, entry.id)
            row.dedup_key = entry.dedup_key
            row.instance_id = entry.instance_id
            row.state = entry.state.value
            row.next_attempt_at = entry.next_attempt_at
            row.document = payload
            self._commit(session)
        return entry

    def get_entry(self, entry_id: str) -> MailOutboxEntry | None:
        return self._load(MailOutboxEntryRow, entry_id, MailOutboxEntry)

    def find_by_dedup_key(self, dedup_key: str) -> MailOutboxEntry | None:
        with self._session() as session:
            row = session.scalars(
                select(MailOutboxEntryRow).where(
                    MailOutboxEntryRow.dedup_key == dedup_key
                )
            ).first()
            if row is None:
                return None
            return MailOutboxEntry.model_validate(row.document)

    def list_entries(self) -> list[MailOutboxEntry]:
        with self._session() as session:
            rows = session.scalars(
                select(MailOutboxEntryRow).order_by(MailOutboxEntryRow.next_attempt_at)
            )
            return [MailOutboxEntry.model_validate(row.document) for row in rows]

    def claim_entry(
        self, entry_id: str, *, now: float, lease_until: float
    ) -> MailOutboxEntry | None:
        """See :meth:`procworks.store.MailOutboxStore.claim_entry` and
        :func:`_claim_outbox_row`."""

        return _claim_outbox_row(
            self, MailOutboxEntryRow, MailOutboxEntry, _DUE_MAIL_OUTBOX_VALUES, entry_id,
            now=now, lease_until=lease_until,
        )

    def finish_entry(self, entry: MailOutboxEntry, *, lease_until: float) -> bool:
        """See :meth:`procworks.store.MailOutboxStore.finish_entry` and
        :func:`_finish_outbox_row`."""

        return _finish_outbox_row(
            self, MailOutboxEntryRow, _DUE_MAIL_OUTBOX_VALUES, entry, lease_until=lease_until
        )

    def clear(self) -> None:
        self._delete_all(MailOutboxEntryRow)


class AbsenceEntryRow(Base):
    """One row per recorded agent absence (deputy substitution window)."""

    __tablename__ = "absence_entry"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyAbsenceStore(_SqlAlchemyStore):
    """A durable absence store backed by a SQLAlchemy engine.

    Each entry is a JSON document plus an ``agent_id`` column for lookups. Like
    every other store the API stays backend-agnostic; the boundary lists the
    (small) set of entries and resolves the currently-absent agents in Python.
    """

    def put_entry(self, entry: AbsenceEntry) -> AbsenceEntry:
        payload = entry.model_dump(mode="json")
        with self._session() as session:
            row = _get_or_add(session, AbsenceEntryRow, entry.id)
            row.agent_id = entry.agent_id
            row.document = payload
            self._commit(session)
        return entry

    def get_entry(self, entry_id: str) -> AbsenceEntry | None:
        return self._load(AbsenceEntryRow, entry_id, AbsenceEntry)

    def list_entries(self) -> list[AbsenceEntry]:
        with self._session() as session:
            rows = session.scalars(select(AbsenceEntryRow).order_by(AbsenceEntryRow.id))
            return [AbsenceEntry.model_validate(row.document) for row in rows]

    def delete_entry(self, entry_id: str) -> bool:
        return self._delete_row(AbsenceEntryRow, entry_id)

    def clear(self) -> None:
        self._delete_all(AbsenceEntryRow)


class TemplateRow(Base):
    """One row per user-created process template (keyed by template id)."""

    __tablename__ = "process_template"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, default="")
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyTemplateStore(_SqlAlchemyStore):
    """A durable user-template store backed by a SQLAlchemy engine.

    Stores only user-created templates as JSON documents; built-in templates
    are provided by :mod:`procworks.templates` and never persisted. Mirrors the
    other stores' ``put``/``get``/``list_ids``/``delete``/``clear`` interface so
    the API stays backend-agnostic.
    """

    def put(self, template: ProcessTemplate) -> ProcessTemplate:
        payload = template.model_dump(mode="json")
        with self._session() as session:
            row = _get_or_add(session, TemplateRow, template.id)
            row.name = template.name
            row.category = template.category
            row.document = payload
            self._commit(session)
        return template

    def get(self, template_id: str) -> ProcessTemplate | None:
        return self._load(TemplateRow, template_id, ProcessTemplate)

    def list_ids(self) -> list[str]:
        with self._session() as session:
            return list(session.scalars(select(TemplateRow.id).order_by(TemplateRow.id)))

    def delete(self, template_id: str) -> bool:
        return self._delete_row(TemplateRow, template_id)

    def clear(self) -> None:
        self._delete_all(TemplateRow)


class LicenseRow(Base):
    """One row per license contingent (free quota / bought pack)."""

    __tablename__ = "license"

    license_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    slots: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    install_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class AgentBindingRow(Base):
    """One row per agent->license assignment (explicit binding)."""

    __tablename__ = "agent_license_binding"

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    license_id: Mapped[str] = mapped_column(String, nullable=False, index=True)


class LicenseMetaRow(Base):
    """Key/value slot for the install id and the persisted time anchor."""

    __tablename__ = "license_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyLicenseStore(_SqlAlchemyStore):
    """A durable license store backed by a SQLAlchemy engine.

    Mirrors the other stores: each license is a JSON document plus a few
    queryable columns; bindings and the small meta records (install id, time
    anchor) are their own tables. The manager stays backend-agnostic.
    """

    _INSTALL_KEY = "install_id"
    _ANCHOR_KEY = "time_anchor"
    #: Open auto-pull claims share the generic meta table under this key prefix
    #: (they are ephemeral, best-effort state that needs no dedicated schema).
    _CLAIM_PREFIX = "claim:"

    def list_licenses(self) -> list[License]:
        with self._session() as session:
            rows = session.scalars(select(LicenseRow))
            return [License.model_validate(row.document) for row in rows]

    def put_license(self, lic: License) -> None:
        payload = lic.model_dump(mode="json")
        with self._session() as session:
            row = _get_or_add(session, LicenseRow, lic.license_id, key_column="license_id")
            row.kind = lic.kind.value
            row.slots = lic.slots
            row.expires_at = lic.expires_at
            row.install_id = lic.install_id
            row.document = payload
            self._commit(session)

    def get_license(self, license_id: str) -> License | None:
        return self._load(LicenseRow, license_id, License)

    def remove_license(self, license_id: str) -> None:
        self._delete_row(LicenseRow, license_id)

    def list_bindings(self) -> list[AgentBinding]:
        with self._session() as session:
            rows = session.scalars(select(AgentBindingRow))
            return [
                AgentBinding(agent_id=row.agent_id, license_id=row.license_id)
                for row in rows
            ]

    def bind(self, agent_id: str, license_id: str) -> None:
        with self._session() as session:
            row = _get_or_add(session, AgentBindingRow, agent_id, key_column="agent_id")
            row.license_id = license_id
            self._commit(session)

    def unbind(self, agent_id: str) -> None:
        self._delete_row(AgentBindingRow, agent_id)

    def get_time_anchor(self) -> TimeAnchor | None:
        return self._load(LicenseMetaRow, self._ANCHOR_KEY, TimeAnchor)

    def put_time_anchor(self, anchor: TimeAnchor) -> None:
        self._put_meta(self._ANCHOR_KEY, anchor.model_dump(mode="json"))

    def list_claims(self) -> list[PendingClaim]:
        with self._session() as session:
            rows = session.scalars(
                select(LicenseMetaRow).where(
                    LicenseMetaRow.key.like(f"{self._CLAIM_PREFIX}%")
                )
            )
            return [PendingClaim.model_validate(row.document) for row in rows]

    def put_claim(self, claim: PendingClaim) -> None:
        key = self._CLAIM_PREFIX + claim.claim_token
        self._put_meta(key, claim.model_dump(mode="json"))

    def _put_meta(self, key: str, payload: dict[str, object]) -> None:
        """Schreibe ``payload`` als Dokument des Meta-Eintrags ``key`` (Upsert).

        Gemeinsamer Schreibweg für Zeitanker und offene Claims, die sich die
        generische Tabelle ``license_meta`` teilen. Ein fehlender Eintrag wird
        angelegt, ein vorhandener überschrieben; ein Commit je Aufruf.

        Parameters
        ----------
        key:
            Primärschlüssel in ``license_meta`` (``time_anchor`` oder
            ``claim:<token>``).
        payload:
            Das JSON-serialisierte Modell (``model_dump(mode="json")``).
        """

        with self._session() as session:
            row = _get_or_add(session, LicenseMetaRow, key, key_column="key")
            row.document = payload
            self._commit(session)

    def remove_claim(self, claim_token: str) -> None:
        self._delete_row(LicenseMetaRow, self._CLAIM_PREFIX + claim_token)

    def install_id(self) -> str:
        with self._session() as session:
            row = session.get(LicenseMetaRow, self._INSTALL_KEY)
            if row is not None:
                return str(row.document["value"])
            value = uuid.uuid4().hex
            session.add(
                LicenseMetaRow(key=self._INSTALL_KEY, document={"value": value})
            )
            self._commit(session)
            return value

    def clear(self) -> None:
        with self._session() as session:
            session.execute(delete(LicenseRow))
            session.execute(delete(AgentBindingRow))
            # The install id survives a data reset (it identifies the install,
            # and bought packs are bound to it); the anchor and any open
            # auto-pull claims are dropped.
            session.execute(
                delete(LicenseMetaRow).where(
                    (LicenseMetaRow.key == self._ANCHOR_KEY)
                    | (LicenseMetaRow.key.like(f"{self._CLAIM_PREFIX}%"))
                )
            )
            self._commit(session)



