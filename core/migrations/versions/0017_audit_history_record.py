# SPDX-License-Identifier: BUSL-1.1
"""record of the historic region of the audit chain (audit_chain)

``0016`` made appending serial and called every event up to
``serialized_since`` *historic*: a branch among them is no defect, an
administrator closes it with an anchor. Until that first anchor, the
historic region was held to nothing -- an event at the end of a historic
branch (no later event names its hash) could be removed or rewritten
together with its own hash without breaking a link, and the anchor then
recorded the forged state as reviewed.

This revision fixes the region as it is at the update: two columns on the
one ``audit_chain`` row, ``historic_events`` (the number of events up to
``serialized_since``) and ``historic_digest`` (SHA-256 over one line
``"<seq>:<entry_hash>\\n"`` per event, in sequence order -- the same lines
as ``procworks.audit.historic_fingerprint``; a test pins that both agree).
The check compares the stored events with them on every run.

**No audit row is changed.** The record describes the region as found: a
change made before this revision ran is part of it (a backup from before the
update is the reference for that).

The downgrade drops the two columns; the events stay. A later upgrade
records the region anew, as it is then.

Revision ID: 0017_audit_history_record
Revises: 0016_audit_chain
Create Date: 2026-10-11

"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_audit_history_record"
down_revision: str | None = "0016_audit_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Rows read per round trip while computing the digest (bounded memory for
#: any size of history).
_BATCH = 5000


def upgrade() -> None:
    with op.batch_alter_table("audit_chain") as batch:
        batch.add_column(sa.Column("historic_events", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("historic_digest", sa.String(), nullable=True))
    bind = op.get_bind()
    row = bind.execute(sa.text("SELECT serialized_since FROM audit_chain WHERE id = 1")).first()
    if row is None:
        # No chain end yet (it is created on first use and records the
        # region itself then).
        return
    serialized_since = int(row[0])
    if serialized_since <= 0:
        # The log was empty at the update: no historic region, no record
        # (every branch is a defect anyway).
        return
    digest = hashlib.sha256()
    count = 0
    last = -(2**62)  # below any number: the first batch starts at the beginning
    while True:
        batch_rows = bind.execute(
            sa.text(
                "SELECT seq, entry_hash FROM audit_event "
                "WHERE seq <= :upto AND seq > :after ORDER BY seq LIMIT :limit"
            ),
            {"upto": serialized_since, "after": last, "limit": _BATCH},
        ).all()
        if not batch_rows:
            break
        for seq, entry_hash in batch_rows:
            digest.update(f"{int(seq)}:{entry_hash or ''}\n".encode())
            count += 1
            last = int(seq)
    bind.execute(
        sa.text(
            "UPDATE audit_chain SET historic_events = :count, historic_digest = :digest "
            "WHERE id = 1"
        ),
        {"count": count, "digest": digest.hexdigest()},
    )


def downgrade() -> None:
    with op.batch_alter_table("audit_chain") as batch:
        batch.drop_column("historic_digest")
        batch.drop_column("historic_events")
