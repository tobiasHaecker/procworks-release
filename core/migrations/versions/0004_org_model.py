# SPDX-License-Identifier: BUSL-1.1
"""org_model table for shared, cross-schema organisation models

Revision ID: 0004_org_model
Revises: 0003_audit_event
Create Date: 2026-06-18

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0004_org_model"
down_revision: str | None = "0003_audit_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "org_model",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("document", _json_document, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("org_model")
