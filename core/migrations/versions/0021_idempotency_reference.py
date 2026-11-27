# SPDX-License-Identifier: BUSL-1.1
"""idempotency keys keep a reference instead of the answer; keyed digests

``idempotency_key.answer`` held the full JSON answer of the first request for
24 hours -- for a start the whole case with its data. Now a row keeps only
``object_id`` and ``status`` of that answer; a replay reads the object anew.
``key_digest`` and ``fingerprint`` become HMACs with a server secret instead
of plain SHA-256 (a copy of the table no longer confirms a guessed key or
body). The secret is ``PROCWORKS_IDEMPOTENCY_SECRET`` when set, otherwise a
random one created here once per installation in the new table
``server_secret`` (shared by all API processes).

**Existing rows are removed** in both directions: they hold the plain answer
(exactly the data this revision stops keeping) under a digest the new code
never computes again. Only replay information of at most 24 hours is lost --
a retry across the update runs once more, as after a restart before the
keys were stored. No process data is touched.

Revision ID: 0021_idempotency_reference
Revises: 0020_login_agent_unique
Create Date: 2026-10-11

"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0021_idempotency_reference"
down_revision: str | None = "0020_login_agent_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM idempotency_key"))
    with op.batch_alter_table("idempotency_key") as batch:
        batch.drop_column("answer")
        batch.add_column(sa.Column("object_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("status", sa.Integer(), nullable=True))
    secret_table = op.create_table(
        "server_secret",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
    )
    op.bulk_insert(secret_table, [{"name": "idempotency", "value": secrets.token_hex(32)}])


def downgrade() -> None:
    op.drop_table("server_secret")
    op.execute(sa.text("DELETE FROM idempotency_key"))
    with op.batch_alter_table("idempotency_key") as batch:
        batch.drop_column("status")
        batch.drop_column("object_id")
        batch.add_column(
            sa.Column("answer", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True)
        )
