# SPDX-License-Identifier: BUSL-1.1
"""process_instance table for durable instance persistence

Revision ID: 0002_process_instance
Revises: 0001_initial
Create Date: 2026-06-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0002_process_instance"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "process_instance",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("schema_id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("document", _json_document, nullable=False),
    )
    op.create_index(
        "ix_process_instance_schema_id",
        "process_instance",
        ["schema_id"],
    )
    op.create_index(
        "ix_process_instance_state",
        "process_instance",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index("ix_process_instance_state", table_name="process_instance")
    op.drop_index("ix_process_instance_schema_id", table_name="process_instance")
    op.drop_table("process_instance")
