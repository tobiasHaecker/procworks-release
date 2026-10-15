# SPDX-License-Identifier: BUSL-1.1
"""version counter for the conditional save of cases and models (lock_version)

Adds ``lock_version`` to ``process_instance`` and ``process_schema``. A store
saves a case or model only if this counter is still the value it was read
with, and raises it by one -- two requests that read the same state can no
longer both save (the later one used to replace the earlier one silently).

Existing rows start at 1 (server default), so a case or model stored before
the upgrade is simply at its first version. The downgrade drops the columns;
the data stays as it is.

Revision ID: 0014_lock_version
Revises: 0013_id_claim
Create Date: 2026-10-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_lock_version"
down_revision: str | None = "0013_id_claim"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("process_instance", "process_schema")


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1")
            )


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("lock_version")
