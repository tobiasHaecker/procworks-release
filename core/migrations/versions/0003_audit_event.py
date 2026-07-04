# SPDX-License-Identifier: BUSL-1.1
"""audit_event table for durable event-log persistence

Revision ID: 0003_audit_event
Revises: 0002_process_instance
Create Date: 2026-06-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0003_audit_event"
down_revision: str | None = "0002_process_instance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "audit_event",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("schema_id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("node_id", sa.String(), nullable=True),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("detail", _json_document, nullable=False),
    )
    op.create_index(
        "ix_audit_event_instance_id",
        "audit_event",
        ["instance_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_event_instance_id", table_name="audit_event")
    op.drop_table("audit_event")
