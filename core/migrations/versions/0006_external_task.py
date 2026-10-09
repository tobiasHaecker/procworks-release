# SPDX-License-Identifier: BUSL-1.1
"""external_task and incident tables for the outbound integration runtime

Revision ID: 0006_external_task
Revises: 0005_user_credential
Create Date: 2026-07-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0006_external_task"
down_revision: str | None = "0005_user_credential"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "external_task",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("available_at", sa.Float(), nullable=True),
        sa.Column("document", _json_document, nullable=False),
    )
    op.create_index("ix_external_task_instance_id", "external_task", ["instance_id"])
    op.create_index("ix_external_task_topic", "external_task", ["topic"])
    op.create_index("ix_external_task_state", "external_task", ["state"])

    op.create_table(
        "incident",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("external_task_id", sa.String(), nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("document", _json_document, nullable=False),
    )
    op.create_index("ix_incident_external_task_id", "incident", ["external_task_id"])
    op.create_index("ix_incident_instance_id", "incident", ["instance_id"])


def downgrade() -> None:
    op.drop_index("ix_incident_instance_id", table_name="incident")
    op.drop_index("ix_incident_external_task_id", table_name="incident")
    op.drop_table("incident")
    op.drop_index("ix_external_task_state", table_name="external_task")
    op.drop_index("ix_external_task_topic", table_name="external_task")
    op.drop_index("ix_external_task_instance_id", table_name="external_task")
    op.drop_table("external_task")
