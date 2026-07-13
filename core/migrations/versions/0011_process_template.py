# SPDX-License-Identifier: BUSL-1.1
"""user-created process templates (reusable blueprints)

Adds the ``process_template`` table that persists modeller-saved templates.
Built-in templates ship as code (:mod:`procworks.templates`) and are never
stored, so only user templates land here. Purely additive -- no existing table
is touched.

Revision ID: 0011_process_template
Revises: 0010_license
Create Date: 2026-07-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0011_process_template"
down_revision: str | None = "0010_license"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "process_template",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False, server_default=""),
        sa.Column("document", _json_document, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("process_template")
