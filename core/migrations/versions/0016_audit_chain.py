# SPDX-License-Identifier: BUSL-1.1
"""recorded end of the audit hash chain (audit_chain)

Adds the one-row table ``audit_chain``. Every append locks this row, takes
the next number and the predecessor hash from it and moves it to the new
event, all in the transaction of the request -- two requests can no longer
link their events to the same end of the chain.

The row starts at the newest stored event (``head_hash``/``head_seq``), and
``serialized_since`` records that number: every event up to it was written
before appending was serialised, so a branch among them is *historic* (the
check reports it with its time, an administrator closes it with an anchor
event); a branch after it is a defect.

**No audit row is changed** -- the log is append-only. Existing branches stay
as they are.

From now on the application assigns ``audit_event.seq`` itself (one past the
recorded end, gap-free) instead of drawing it from the column's sequence, so
the PostgreSQL sequence stops advancing. The downgrade drops the table and,
on PostgreSQL, moves the sequence past the highest stored number, so an older
version continues numbering without a collision.

Revision ID: 0016_audit_chain
Revises: 0015_external_task_open_step
Create Date: 2026-10-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_audit_chain"
down_revision: str | None = "0015_external_task_open_step"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_chain",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("head_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("head_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("serialized_since", sa.Integer(), nullable=False, server_default="0"),
    )
    bind = op.get_bind()
    newest = bind.execute(
        sa.text("SELECT seq, entry_hash FROM audit_event ORDER BY seq DESC LIMIT 1")
    ).first()
    seq = int(newest[0]) if newest is not None else 0
    head_hash = str(newest[1] or "") if newest is not None else ""
    bind.execute(
        sa.text(
            "INSERT INTO audit_chain (id, head_hash, head_seq, serialized_since) "
            "VALUES (1, :head_hash, :seq, :seq)"
        ),
        {"head_hash": head_hash, "seq": seq},
    )


def downgrade() -> None:
    op.drop_table("audit_chain")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The application numbered events itself; let the column's sequence
        # continue after them (is_called=false on an empty table: next is 1).
        bind.execute(
            sa.text(
                "SELECT setval(pg_get_serial_sequence('audit_event', 'seq'), "
                "COALESCE(MAX(seq), 1), MAX(seq) IS NOT NULL) FROM audit_event"
            )
        )
