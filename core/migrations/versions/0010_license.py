# SPDX-License-Identifier: BUSL-1.1
"""licensing tables and audit hash chain

Adds the (dormant by default) licensing layer: license contingents, explicit
agent->license bindings and a key/value meta table for the install id and the
time anchor. Also adds the append-only hash-chain columns to ``audit_event``
(``prev_hash``/``entry_hash``), defaulted to "" so pre-existing rows stay valid.

Revision ID: 0010_license
Revises: 0009_absence_entry
Create Date: 2026-07-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0010_license"
down_revision: str | None = "0009_absence_entry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "license",
        sa.Column("license_id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("slots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.Column("install_id", sa.String(), nullable=False, server_default=""),
        sa.Column("document", _json_document, nullable=False),
    )
    op.create_table(
        "agent_license_binding",
        sa.Column("agent_id", sa.String(), primary_key=True),
        sa.Column("license_id", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_agent_license_binding_license_id",
        "agent_license_binding",
        ["license_id"],
    )
    op.create_table(
        "license_meta",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("document", _json_document, nullable=False),
    )
    op.add_column(
        "audit_event",
        sa.Column("prev_hash", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "audit_event",
        sa.Column("entry_hash", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("audit_event", "entry_hash")
    op.drop_column("audit_event", "prev_hash")
    op.drop_table("license_meta")
    op.drop_index(
        "ix_agent_license_binding_license_id",
        table_name="agent_license_binding",
    )
    op.drop_table("agent_license_binding")
    op.drop_table("license")
