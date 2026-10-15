# SPDX-License-Identifier: BUSL-1.1
"""The recorded end of the audit chain arrives by migration -- no audit row
is rewritten.

An installation that updates runs ``alembic upgrade head`` (the API container
does it at start). Its history may already contain branches from requests
that recorded events at the same moment. The upgrade only adds the chain-end
row: it starts at the newest stored event and records that number as the
point from which appending is serialised. The stored events stay byte for
byte as they were; the check then reports the old branches as historic, and
an anchor closes them. The downgrade removes the row again.

Alembic runs in a subprocess, as in the container: its logging configuration
would otherwise reconfigure the loggers of the test process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text
from test_audit_chain import _chain, _plant

from procworks.audit import EventType, check_chain
from procworks.db import SqlAlchemyAuditLog

pytest.importorskip("alembic")

_CORE = Path(__file__).resolve().parents[1]


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(_CORE / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_CORE,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _rows(url: str) -> list[tuple[Any, ...]]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM audit_event ORDER BY seq"))
            return [tuple(row) for row in rows]
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_keeps_every_event_and_reports_old_branches_as_historic(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    _alembic(url, "upgrade", "0015_external_task_open_step")
    _plant(url, _chain([1, 2, 3, 4, 5, 6], links={3: 1, 5: 3}))
    before = _rows(url)

    _alembic(url, "upgrade", "head")

    assert _rows(url) == before, "the upgrade rewrote stored audit events"
    log = SqlAlchemyAuditLog(url)
    state = log.chain_state()
    assert (state.head_hash, state.head_seq, state.serialized_since) == (
        before[-1][-1], 6, 6,
    )
    report = check_chain(log)
    assert report.status == "open"
    assert [(b.seq, b.links_to) for b in report.branches] == [(3, 1), (5, 3)]

    event = log.append(EventType.INSTANCE_CREATED, "nach-dem-update", "s1")
    assert (event.seq, event.prev_hash) == (7, before[-1][-1])
    anchor = log.append(
        EventType.CHAIN_ANCHOR, "__audit_chain__", "__audit_chain__",
        detail={"branches": "3,5", "checked_through": "7", "reason": "geprüft", "actor": "admin"},
    )
    closed = check_chain(log)
    assert closed.status == "intact"
    assert [(b.seq, b.anchored_by) for b in closed.branches] == [(3, anchor.seq), (5, anchor.seq)]
    assert _rows(url)[:6] == before


def test_upgrade_of_an_empty_log_starts_a_fresh_chain(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    _alembic(url, "upgrade", "head")

    log = SqlAlchemyAuditLog(url)
    first = log.append(EventType.INSTANCE_CREATED, "i1", "s1")

    assert (first.seq, first.prev_hash) == (1, "")
    assert log.chain_state().serialized_since == 0
    assert check_chain(log).status == "intact"


def test_downgrade_drops_the_chain_end_and_keeps_the_events(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    _alembic(url, "upgrade", "0015_external_task_open_step")
    _plant(url, _chain([1, 2, 3], links={3: 1}))
    _alembic(url, "upgrade", "head")
    SqlAlchemyAuditLog(url).append(EventType.INSTANCE_CREATED, "i4", "s1")
    before = _rows(url)

    _alembic(url, "downgrade", "0015_external_task_open_step")

    assert "audit_chain" not in _tables(url)
    assert _rows(url) == before
    _alembic(url, "upgrade", "head")  # and up again: the end follows the events
    assert SqlAlchemyAuditLog(url).chain_state().head_seq == 4
