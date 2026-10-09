# SPDX-License-Identifier: BUSL-1.1
"""atomic reservation of generated ids (id_claim)

Adds the ``id_claim`` table. Every API process reserves a new instance, schema,
template or org-model id here (a unique insert) before handing it out, so two
processes against the same database can never give the same id to two objects
-- the later save would otherwise replace the earlier one. Purely additive; ids
that already exist need no row (the stores themselves are checked as well).

Revision ID: 0013_id_claim
Revises: 0012_auth_session
Create Date: 2026-10-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_id_claim"
down_revision: str | None = "0012_auth_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "id_claim",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("id_claim")
