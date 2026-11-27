# SPDX-License-Identifier: BUSL-1.1
"""reserved Idempotency-Key values, shared by all API processes

Adds ``idempotency_key``. Before, every API process kept the keys in its own
memory: two first requests with the same key, sent to two processes, both
ran the operation. Now the key is reserved by inserting its row inside the
transaction of the request, and the answer is written into the same row in
the same transaction -- a concurrent request in any process waits at its
insert and then replays the committed answer.

``key_digest`` (primary key) is SHA-256 over identity and key, so neither
the login nor the key is stored and any key length fits the index.
``expires_at`` (indexed) ends the replay after 24 hours.

The table starts empty: keys held in memory by the previous version are not
carried over (a retry across the update runs once more -- the same as a
restart before). The downgrade drops the table; only replay information is
lost, no process data.

Revision ID: 0019_idempotency_key
Revises: 0018_org_lock_version
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0019_idempotency_key"
down_revision: str | None = "0018_org_lock_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_key",
        sa.Column("key_digest", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("answer", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_idempotency_key_expires_at", "idempotency_key", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_key_expires_at", table_name="idempotency_key")
    op.drop_table("idempotency_key")
