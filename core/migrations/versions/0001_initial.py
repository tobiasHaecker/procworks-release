# SPDX-License-Identifier: BUSL-1.1
"""initial schema: process_schema table

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "process_schema",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("lifecycle_state", sa.String(), nullable=False),
        sa.Column("document", _json_document, nullable=False),
    )
    op.create_index(
        "ix_process_schema_lifecycle_state",
        "process_schema",
        ["lifecycle_state"],
    )


def downgrade() -> None:
    op.drop_index("ix_process_schema_lifecycle_state", table_name="process_schema")
    op.drop_table("process_schema")
