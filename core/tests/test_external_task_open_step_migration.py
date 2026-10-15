# SPDX-License-Identifier: BUSL-1.1
"""One open external task per step arrives by migration -- without losing data.

The rule "a step holds at most one open task" is a unique index in the
database. Installations that ran parallel workers before may already hold
several open tasks for one step, and tasks of cases that have ended. The
index cannot be created over them, and deleting them would lose the trace of
what happened. So the upgrade closes them first: of a group of duplicates the
task a worker holds stays open, the others become ``CANCELLED`` with the note
``duplicate``; tasks whose case or step has ended become ``CANCELLED`` with
``step-ended``; their open incidents are resolved. Nothing is deleted.

The downgrade drops index and version column; older versions do not know
``CANCELLED``, so those tasks become ``COMPLETED`` (an end state they never
hand out) and keep their note.

Alembic runs in a subprocess, as in the container (see
``test_lock_version_migration.py``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from procworks.db import SqlAlchemyExternalTaskStore
from procworks.model import ExternalTask, ExternalTaskState

pytest.importorskip("alembic")

_CORE = Path(__file__).resolve().parents[1]
_BEFORE = "0014_lock_version"


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


def _execute(url: str, sql: str, rows: list[dict[str, Any]]) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for row in rows:
                conn.execute(text(sql), row)
    finally:
        engine.dispose()


def _case(case_id: str, state: str, node_state: str | None) -> dict[str, Any]:
    node_states = {} if node_state is None else {"n1": node_state}
    return {
        "id": case_id,
        "state": state,
        "doc": json.dumps({"id": case_id, "schema_id": "s1", "state": state,
                           "node_states": node_states}),
    }


def _task(
    task_id: str, case_id: str, state: str, *, lock_expires_at: float | None = None
) -> dict[str, Any]:
    document = {"id": task_id, "instance_id": case_id, "node_id": "n1", "topic": "t",
                "state": state, "worker_id": "w" if state == "LOCKED" else None,
                "lock_expires_at": lock_expires_at}
    return {"id": task_id, "case": case_id, "state": state, "doc": json.dumps(document)}


def _seed_before_the_rule(url: str) -> None:
    """Tasks as parallel fetches of an earlier version left them behind."""

    _execute(
        url,
        "INSERT INTO process_instance (id, schema_id, schema_version, state, document) "
        "VALUES (:id, 's1', 1, :state, :doc)",
        [
            _case("running", "RUNNING", "ACTIVATED"),  # four open tasks for one step
            _case("single", "RUNNING", "ACTIVATED"),  # one open task: stays as it is
            _case("finished", "COMPLETED", "COMPLETED"),  # case ended
            _case("moved_on", "RUNNING", "COMPLETED"),  # step finished another way
        ],
    )
    _execute(
        url,
        "INSERT INTO external_task (id, instance_id, node_id, topic, state, document) "
        "VALUES (:id, :case, 'n1', 't', :state, :doc)",
        [
            _task("et_waiting", "running", "CREATED"),
            _task("et_held_earlier", "running", "LOCKED", lock_expires_at=500.0),
            _task("et_held_latest", "running", "LOCKED", lock_expires_at=900.0),
            _task("et_incident", "running", "INCIDENT"),
            _task("et_single", "single", "CREATED"),
            _task("et_finished_open", "finished", "CREATED"),
            _task("et_finished_done", "finished", "COMPLETED"),
            _task("et_moved_on", "moved_on", "LOCKED", lock_expires_at=100.0),
            _task("et_orphan", "vanished", "CREATED"),
        ],
    )
    _execute(
        url,
        "INSERT INTO incident (id, external_task_id, instance_id, resolved, document) "
        "VALUES (:id, :task, 'running', :resolved, :doc)",
        [{"id": "inc_1", "task": "et_incident", "resolved": False,
          "doc": json.dumps({"id": "inc_1", "external_task_id": "et_incident",
                             "instance_id": "running", "node_id": "n1",
                             "message": "boom", "created_at": 1.0, "resolved": False})}],
    )


def _rows(url: str) -> dict[str, tuple[str, dict[str, Any]]]:
    """task id -> (state column, document)."""

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, state, document FROM external_task")).all()
    finally:
        engine.dispose()
    return {
        task_id: (state, raw if isinstance(raw, dict) else json.loads(raw))
        for task_id, state, raw in rows
    }


def _incident(url: str) -> tuple[bool, dict[str, Any]]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            resolved, raw = conn.execute(
                text("SELECT resolved, document FROM incident WHERE id = 'inc_1'")
            ).one()
    finally:
        engine.dispose()
    return bool(resolved), json.loads(raw)


def _second_open_task(url: str, case_id: str) -> None:
    row = _task("et_extra", case_id, "CREATED")
    _execute(
        url,
        "INSERT INTO external_task (id, instance_id, node_id, topic, state, document) "
        "VALUES (:id, :case, 'n1', 't', :state, :doc)",
        [row],
    )


def _columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("external_task")}
    finally:
        engine.dispose()


def _states(url: str) -> dict[str, tuple[str, str | None]]:
    """task id -> (state, cancel_reason); column and document must agree."""

    out: dict[str, tuple[str, str | None]] = {}
    for task_id, (column, document) in _rows(url).items():
        assert document["state"] == column, task_id
        out[task_id] = (column, document.get("cancel_reason"))
    return out


def test_upgrade_closes_duplicates_and_orphans_instead_of_deleting_them(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    _alembic(url, "upgrade", _BEFORE)
    _seed_before_the_rule(url)

    _alembic(url, "upgrade", "head")

    assert _states(url) == {
        # The task a worker holds (latest lock) stays; the others are closed.
        "et_held_latest": ("LOCKED", None),
        "et_held_earlier": ("CANCELLED", "duplicate"),
        "et_waiting": ("CANCELLED", "duplicate"),
        "et_incident": ("CANCELLED", "duplicate"),
        "et_single": ("CREATED", None),
        "et_finished_open": ("CANCELLED", "step-ended"),
        "et_finished_done": ("COMPLETED", None),
        "et_moved_on": ("CANCELLED", "step-ended"),
        "et_orphan": ("CANCELLED", "step-ended"),
    }
    # A closed task belongs to no worker any more; its incident is resolved.
    assert _rows(url)["et_moved_on"][1]["worker_id"] is None
    resolved, document = _incident(url)
    assert resolved and document["resolved"] is True
    # Everything still loads, with its version.
    store = SqlAlchemyExternalTaskStore(url)
    loaded = {t.id: t for t in store.list_tasks()}
    assert loaded["et_orphan"].state is ExternalTaskState.CANCELLED
    assert loaded["et_orphan"].cancel_reason == "step-ended"
    assert "lock_version" in _columns(url)


def test_after_the_upgrade_the_database_refuses_a_second_open_task(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'rule.db'}"
    _alembic(url, "upgrade", _BEFORE)
    _seed_before_the_rule(url)
    _alembic(url, "upgrade", "head")

    with pytest.raises(IntegrityError, match="UNIQUE"):
        _second_open_task(url, "single")
    # Through the store: refused as "step already has an open task".
    store = SqlAlchemyExternalTaskStore(url)
    assert store.add(ExternalTask(id="et_new", instance_id="single", node_id="n1",
                                  topic="t")) is False
    # A closed task never blocks the step's next one.
    assert store.add(ExternalTask(id="et_next", instance_id="finished", node_id="n1",
                                  topic="t")) is True


def test_downgrade_keeps_every_task_and_maps_cancelled_to_completed(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    _alembic(url, "upgrade", _BEFORE)
    _seed_before_the_rule(url)
    _alembic(url, "upgrade", "head")

    _alembic(url, "downgrade", _BEFORE)

    states = _states(url)
    assert len(states) == 9
    assert states["et_waiting"] == ("COMPLETED", "duplicate")
    assert states["et_orphan"] == ("COMPLETED", "step-ended")
    assert states["et_held_latest"] == ("LOCKED", None)
    assert "lock_version" not in _columns(url)
    _second_open_task(url, "single")  # the index is gone as well

    # And up again: the duplicate just planted is closed the same way.
    _alembic(url, "upgrade", "head")
    rows = _rows(url)
    open_for_single = [
        task_id for task_id, (state, document) in rows.items()
        if document["instance_id"] == "single" and state in ("CREATED", "LOCKED")
    ]
    assert len(open_for_single) == 1
    closed = ({"et_extra", "et_single"} - set(open_for_single)).pop()
    assert _states(url)[closed] == ("CANCELLED", "duplicate")
