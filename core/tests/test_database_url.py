# SPDX-License-Identifier: BUSL-1.1
"""Guards for the test database switch (``database_url.py``) and its CI job.

The SQL-backed tests run on SQLite by default and on PostgreSQL when
``PROCWORKS_TEST_DATABASE_URL`` is set. The switch is only worth something if
(a) a PostgreSQL run really talks to PostgreSQL, (b) a wrong value fails loudly
instead of quietly running SQLite again, and (c) the CI job that sets it exists
and cannot hang for hours -- the bug it exists for (an id claim that read
``rowcount``, which is ``-1`` under PostgreSQL) shows up as a hang, not as a
failing assertion.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from database_url import (
    DATABASE_PREFIX,
    ENV,
    drop_created_databases,
    fresh_database_url,
    postgres_requested,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

_CI_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def test_default_is_a_sqlite_file_below_tmp_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV, raising=False)

    assert fresh_database_url(tmp_path) == f"sqlite:///{tmp_path / 'procworks.db'}"
    assert fresh_database_url(tmp_path, "audit") == f"sqlite:///{tmp_path / 'audit.db'}"


def test_blank_value_counts_as_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Boundary: an empty variable (as a CI matrix may leave it) means SQLite."""

    monkeypatch.setenv(ENV, "   ")

    assert not postgres_requested()
    assert fresh_database_url(tmp_path).startswith("sqlite:///")


@pytest.mark.parametrize("value", ["sqlite:///elsewhere.db", "mysql+pymysql://u:p@db/x"])
def test_a_value_that_is_not_postgresql_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    """A typo must not turn the PostgreSQL job into a second SQLite run."""

    monkeypatch.setenv(ENV, value)

    with pytest.raises(ValueError, match=f"{ENV} must be a postgresql URL"):
        fresh_database_url(tmp_path)


@pytest.mark.skipif(not postgres_requested(), reason=f"{ENV} is not set (SQLite run)")
def test_a_postgresql_run_really_uses_fresh_postgresql_databases(tmp_path: Path) -> None:
    """Premise of the CI job: the URL points at PostgreSQL, one database per name."""

    first = fresh_database_url(tmp_path)
    again = fresh_database_url(tmp_path)
    other = fresh_database_url(tmp_path, "other")

    assert first == again
    assert first != other
    name = make_url(first).database
    assert make_url(first).get_backend_name() == "postgresql"
    assert name is not None and name.startswith(DATABASE_PREFIX)

    engine = create_engine(first)
    try:
        with engine.begin() as connection:
            assert connection.execute(text("SELECT current_database()")).scalar_one() == name
            connection.execute(text("CREATE TABLE probe (id integer)"))
            connection.execute(text("INSERT INTO probe VALUES (1)"))
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM probe")).scalar_one() == 1
    finally:
        engine.dispose()

    drop_created_databases()

    admin = create_engine(os.environ[ENV].strip())
    try:
        with admin.connect() as connection:
            left = connection.execute(
                text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar_one()
    finally:
        admin.dispose()
    assert left == 0, "drop_created_databases() left the test database behind"


def _job_block(workflow: str, job: str) -> str | None:
    """The text of one job of a workflow, up to the next job (``None`` if absent).

    Jobs are the keys indented by exactly two spaces below ``jobs:``.
    """

    headers = list(re.finditer(r"^  ([A-Za-z0-9_-]+):\s*$", workflow, re.MULTILINE))
    for index, header in enumerate(headers):
        if header.group(1) == job:
            end = headers[index + 1].start() if index + 1 < len(headers) else len(workflow)
            return workflow[header.start():end]
    return None


@pytest.mark.skipif(not _CI_WORKFLOW.exists(), reason="CI workflow exists only internally")
def test_ci_runs_the_suite_against_postgresql_with_a_time_limit() -> None:
    """The job exists, sets the switch, has a database service and a time limit."""

    job = _job_block(_CI_WORKFLOW.read_text(encoding="utf-8"), "test-postgres")
    assert job is not None, "ci.yml has no job 'test-postgres'"
    assert f"{ENV}: postgresql+psycopg://" in job, "the job does not set the switch"
    assert "image: postgres:16-alpine" in job, "the job has no PostgreSQL service"
    assert "timeout-minutes:" in job, (
        "the job has no time limit -- a claim bug hangs instead of failing"
    )
    assert "pytest -q" in job, "the job does not run the test suite"


def test_job_block_ends_at_the_next_job() -> None:
    """Boundary of the helper: a setting of the next job is not attributed."""

    workflow = "jobs:\n  a:\n    runs-on: x\n  b:\n    timeout-minutes: 5\n"

    assert _job_block(workflow, "a") == "  a:\n    runs-on: x\n"
    assert "timeout-minutes" in (_job_block(workflow, "b") or "")
    assert _job_block(workflow, "c") is None
