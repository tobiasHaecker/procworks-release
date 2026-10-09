# SPDX-License-Identifier: BUSL-1.1
"""durable mail outbox for modelled e-mail notifications (rule group N)

Revision ID: 0008_mail_outbox
Revises: 0007_webhook_outbox
Create Date: 2026-07-12

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0008_mail_outbox"
down_revision: str | None = "0007_webhook_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "mail_outbox",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("dedup_key", sa.String(), nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("document", _json_document, nullable=False),
    )
    op.create_index("ix_mail_outbox_dedup_key", "mail_outbox", ["dedup_key"])
    op.create_index("ix_mail_outbox_instance_id", "mail_outbox", ["instance_id"])
    op.create_index("ix_mail_outbox_state", "mail_outbox", ["state"])


def downgrade() -> None:
    op.drop_index("ix_mail_outbox_state", table_name="mail_outbox")
    op.drop_index("ix_mail_outbox_instance_id", table_name="mail_outbox")
    op.drop_index("ix_mail_outbox_dedup_key", table_name="mail_outbox")
    op.drop_table("mail_outbox")
