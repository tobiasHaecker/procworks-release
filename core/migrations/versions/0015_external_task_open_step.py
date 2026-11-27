# SPDX-License-Identifier: BUSL-1.1
"""one open external task per step, and a version counter for external tasks

Adds ``lock_version`` to ``external_task`` (the conditional save: a worker can
only lock, report or fail a task nobody changed since it was read) and the
partial unique index ``uq_external_task_open_step`` on
``(instance_id, node_id)`` over the open states (``CREATED``, ``LOCKED``,
``INCIDENT``, ``BPMN_ERROR``): a step holds at most one open task, so two
concurrent fetches can no longer both create the task of the same step.

Stored data that breaks the rule is **not deleted** -- the index could not be
created over it, and deleting would lose the trace of what happened. Before
the index is created, the upgrade closes it:

* **Duplicates** -- a step with several open tasks keeps exactly one: the one
  a worker or tool is holding (``LOCKED``, latest lock expiry first; a push
  without expiry ranks first), else one with an open incident (``INCIDENT``,
  then ``BPMN_ERROR``), else a waiting one (``CREATED``); ties go to the
  smallest id. Every other one becomes ``CANCELLED`` with ``cancel_reason``
  ``"duplicate"``; a worker that still reports it gets ``409 ET.not-locked``.
* **Orphans** -- an open task whose case no longer exists, is no longer
  running, or whose step is no longer active (not ``ACTIVATED``/``RUNNING``)
  becomes ``CANCELLED`` with ``cancel_reason`` ``"step-ended"``.

The unresolved incidents of every closed task are marked resolved (as when a
step is finished by hand). Column and document are changed together; the
document keeps everything else, so the history stays readable.

The downgrade drops index and column. Since older versions do not know the
state ``CANCELLED``, its tasks become ``COMPLETED`` (an end state those
versions never hand out again); ``cancel_reason`` stays in the document as the
note. The tasks are not reopened -- that would bring the duplicates back.

Revision ID: 0015_external_task_open_step
Revises: 0014_lock_version
Create Date: 2026-10-10

"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0015_external_task_open_step"
down_revision: str | None = "0014_lock_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here on purpose: a migration must keep meaning what it meant when it
#: was written, whatever the application code says later.
_OPEN_CONDITION = "state IN ('BPMN_ERROR', 'CREATED', 'INCIDENT', 'LOCKED')"
_ACTIVE_NODE_STATES = ("ACTIVATED", "RUNNING")
_INDEX = "uq_external_task_open_step"
#: JSONB on PostgreSQL, plain JSON elsewhere -- the type of the ``document``
#: columns (see ``0006_external_task``).
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def _document(raw: Any) -> dict[str, Any]:
    """The stored JSON document as a dict (SQLite hands out text, PostgreSQL a dict)."""

    if isinstance(raw, dict):
        return dict(raw)
    loaded = json.loads(raw)
    assert isinstance(loaded, dict)
    return loaded


def _keep_rank(row: dict[str, Any]) -> tuple[int, float, str]:
    """Sort key: the task of a duplicate group that stays open comes first.

    :param row: ``{"id", "state", "document"}`` of one open task.
    :returns: ``(state rank, -lock expiry, id)`` -- held by a worker before an
        open incident before a waiting task; among held ones the latest lock
        (no expiry = a pushed task waiting for its tool = latest); then the
        smallest id, so the choice is reproducible.
    """

    rank = {"LOCKED": 0, "INCIDENT": 1, "BPMN_ERROR": 2, "CREATED": 3}[row["state"]]
    expiry = row["document"].get("lock_expires_at")
    latest = float("inf") if expiry is None else float(expiry)
    return (rank, -latest if rank == 0 else 0.0, row["id"])


def _cases(bind: sa.Connection) -> dict[str, tuple[str, dict[str, Any]]]:
    """Every stored case: id -> (state column, node_states of its document)."""

    cases: dict[str, tuple[str, dict[str, Any]]] = {}
    for case_id, state, raw in bind.execute(
        sa.text("SELECT id, state, document FROM process_instance")
    ):
        node_states = _document(raw).get("node_states") or {}
        cases[case_id] = (state, node_states)
    return cases


def _close(bind: sa.Connection, row: dict[str, Any], reason: str) -> None:
    """Turn one open task into ``CANCELLED`` (column and document) and resolve
    its unresolved incidents."""

    document = dict(row["document"])
    document.update(
        state="CANCELLED",
        cancel_reason=reason,
        worker_id=None,
        lock_expires_at=None,
        available_at=None,
    )
    bind.execute(
        sa.text(
            "UPDATE external_task SET state = 'CANCELLED', available_at = NULL, "
            "document = :doc WHERE id = :id"
        ).bindparams(sa.bindparam("doc", type_=_json_document)),
        {"doc": document, "id": row["id"]},
    )
    incidents = bind.execute(
        sa.text(
            "SELECT id, document FROM incident "
            "WHERE external_task_id = :task AND resolved = :no"
        ),
        {"task": row["id"], "no": False},
    ).all()
    for incident_id, raw in incidents:
        incident = _document(raw)
        incident["resolved"] = True
        bind.execute(
            sa.text(
                "UPDATE incident SET resolved = :yes, document = :doc WHERE id = :id"
            ).bindparams(sa.bindparam("doc", type_=_json_document)),
            {"yes": True, "doc": incident, "id": incident_id},
        )


def _close_duplicates_and_orphans(bind: sa.Connection) -> None:
    """Bring the stored tasks in line with "one open task per active step"."""

    open_rows = [
        {"id": task_id, "instance_id": case_id, "node_id": node_id, "state": state,
         "document": _document(raw)}
        for task_id, case_id, node_id, state, raw in bind.execute(
            sa.text(
                "SELECT id, instance_id, node_id, state, document FROM external_task "
                f"WHERE {_OPEN_CONDITION}"
            )
        )
    ]
    if not open_rows:
        return
    cases = _cases(bind)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in open_rows:
        groups.setdefault((row["instance_id"], row["node_id"]), []).append(row)
    for (case_id, node_id), rows in groups.items():
        case = cases.get(case_id)
        step_active = (
            case is not None
            and case[0] == "RUNNING"
            and case[1].get(node_id) in _ACTIVE_NODE_STATES
        )
        if not step_active:
            for row in rows:
                _close(bind, row, "step-ended")
            continue
        for row in sorted(rows, key=_keep_rank)[1:]:
            _close(bind, row, "duplicate")


def upgrade() -> None:
    with op.batch_alter_table("external_task") as batch:
        batch.add_column(
            sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1")
        )
    _close_duplicates_and_orphans(op.get_bind())
    op.create_index(
        _INDEX,
        "external_task",
        ["instance_id", "node_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN_CONDITION),
        sqlite_where=sa.text(_OPEN_CONDITION),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="external_task")
    bind = op.get_bind()
    cancelled = bind.execute(
        sa.text("SELECT id, document FROM external_task WHERE state = 'CANCELLED'")
    ).all()
    for task_id, raw in cancelled:
        document = _document(raw)
        document["state"] = "COMPLETED"
        bind.execute(
            sa.text(
                "UPDATE external_task SET state = 'COMPLETED', document = :doc WHERE id = :id"
            ).bindparams(sa.bindparam("doc", type_=_json_document)),
            {"doc": document, "id": task_id},
        )
    with op.batch_alter_table("external_task") as batch:
        batch.drop_column("lock_version")
