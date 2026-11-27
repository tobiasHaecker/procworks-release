# SPDX-License-Identifier: BUSL-1.1
"""version counter for the conditional save of shared organisations

Adds ``lock_version`` to ``org_model``, as ``0014`` did for cases and
models. A store saves a shared organisation only if this counter is still
the value it was read with, and raises it by one. Before, two requests that
changed the same organisation both saved, and the later one replaced the
earlier one -- visible wherever no referencing schema was saved alongside
(every organisation before its first use).

Existing rows start at 1 (server default). The downgrade drops the column;
the data stays as it is.

Revision ID: 0018_org_lock_version
Revises: 0017_audit_history_record
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_org_lock_version"
down_revision: str | None = "0017_audit_history_record"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("org_model") as batch:
        batch.add_column(
            sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1")
        )


def downgrade() -> None:
    with op.batch_alter_table("org_model") as batch:
        batch.drop_column("lock_version")
