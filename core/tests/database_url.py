# SPDX-License-Identifier: BUSL-1.1
"""Test helper: the database a SQL-backed test runs against.

By default every SQL-backed test uses its own SQLite file below ``tmp_path``.
With the environment variable :data:`ENV` set to a PostgreSQL URL, the same
tests run against PostgreSQL instead: each call creates a **fresh database**
on that server, so tests never see each other's rows and need no cleanup
between them.

Why PostgreSQL at all: SQLite hides exactly the behaviour that broke releases.
Under PostgreSQL with psycopg 3, ``rowcount`` of an ``INSERT ... ON CONFLICT``
was ``-1`` and every creation hung (2026-10-09); row locks (``FOR UPDATE``),
``RETURNING`` and real concurrency only exist there. The CI job "Tests against
PostgreSQL" sets :data:`ENV` and runs the SQL-backed tests a second time.

After each test the suite drops the databases it created for that test
(:func:`drop_created_databases`, called by a fixture in ``conftest.py``). Each
one costs several megabytes; while they were kept, a local server with its
data in memory (``--tmpfs``) crashed after a few hundred tests, most likely
from running out of memory. Nothing else on the server is ever
changed -- only databases named ``pw_test_*`` that this run created -- but the
server is still meant to be disposable (the CI service container, or a
throwaway local container).

Not a test module (no ``test_`` prefix), imported flat -- ``tests/`` is
deliberately not a package.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

#: Environment variable with the PostgreSQL server URL, e.g.
#: ``postgresql+psycopg://process:process@localhost:55432/procworks``. The
#: database named there is only used to connect; tests run in new databases.
ENV = "PROCWORKS_TEST_DATABASE_URL"

#: Prefix of every database this helper creates (easy to spot and remove).
DATABASE_PREFIX = "pw_test_"

#: ``(tmp_path, name)`` -> PostgreSQL URL already created for it. A test that
#: builds "the same file" twice must get the same database, exactly as two
#: SQLite URLs on one file reach the same data; ``tmp_path`` is unique per
#: test, so entries never leak into another test.
_created: dict[tuple[Path, str], str] = {}


def postgres_requested() -> bool:
    """``True`` when :data:`ENV` is set (to anything but blanks)."""

    return bool(os.environ.get(ENV, "").strip())


def fresh_database_url(tmp_path: Path, name: str = "procworks") -> str:
    """Return the URL of an empty database for one test.

    :param tmp_path: the test's directory; holds the SQLite file in the
        default mode, unused with PostgreSQL.
    :param name: base name of the SQLite file. Different names give
        independent databases; the same ``(tmp_path, name)`` gives the same
        database again (with PostgreSQL too), like two URLs on one file.
    :returns: a SQLAlchemy URL with the password in clear text (the stores
        need it to connect).
    :raises ValueError: if :data:`ENV` is set but is not a PostgreSQL URL --
        a typo must not silently fall back to SQLite and report a green
        PostgreSQL run that never happened.
    """

    base = os.environ.get(ENV, "").strip()
    if not base:
        return f"sqlite:///{tmp_path / f'{name}.db'}"
    known = _created.get((tmp_path, name))
    if known is not None:
        return known
    server = make_url(base)
    if server.get_backend_name() != "postgresql":
        raise ValueError(f"{ENV} must be a postgresql URL, got {server.get_backend_name()!r}")
    database = f"{DATABASE_PREFIX}{uuid.uuid4().hex[:20]}"
    # CREATE DATABASE cannot run inside a transaction; NullPool leaves no
    # idle connection behind on the server.
    admin = create_engine(server, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        admin.dispose()
    url = server.set(database=database).render_as_string(hide_password=False)
    _created[(tmp_path, name)] = url
    return url


def drop_created_databases() -> None:
    """Drop every database :func:`fresh_database_url` created so far.

    ``WITH (FORCE)`` (PostgreSQL 13+) ends connections a test left open --
    an engine the test built itself, or a pool not yet disposed. Only names
    this process created are touched, never another database on the server.
    A no-op without :data:`ENV` or when nothing was created.
    """

    if not _created:
        return
    names = [make_url(url).database for url in _created.values()]
    _created.clear()
    server = make_url(os.environ[ENV].strip())
    admin = create_engine(server, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as connection:
            for name in names:
                assert name and name.startswith(DATABASE_PREFIX), name
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()
