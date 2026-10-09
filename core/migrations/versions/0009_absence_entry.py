# SPDX-License-Identifier: BUSL-1.1
"""recorded agent absences (deputy substitution windows)

Revision ID: 0009_absence_entry
Revises: 0008_mail_outbox
Create Date: 2026-07-12

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0009_absence_entry"
down_revision: str | None = "0008_mail_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "absence_entry",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("document", _json_document, nullable=False),
    )
    op.create_index("ix_absence_entry_agent_id", "absence_entry", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_absence_entry_agent_id", table_name="absence_entry")
    op.drop_table("absence_entry")
