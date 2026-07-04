# SPDX-License-Identifier: BUSL-1.1
"""webhook subscription, outbox and delivery tables for the event side (E13)

Revision ID: 0007_webhook_outbox
Revises: 0006_external_task
Create Date: 2026-07-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0007_webhook_outbox"
down_revision: str | None = "0006_external_task"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) so the migration is
# portable across the supported backends.
_json_document = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "webhook_subscription",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("document", _json_document, nullable=False),
    )

    op.create_table(
        "webhook_outbox",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("subscription_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("document", _json_document, nullable=False),
    )
    op.create_index(
        "ix_webhook_outbox_subscription_id", "webhook_outbox", ["subscription_id"]
    )
    op.create_index("ix_webhook_outbox_event_type", "webhook_outbox", ["event_type"])
    op.create_index("ix_webhook_outbox_state", "webhook_outbox", ["state"])

    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("subscription_id", sa.String(), nullable=False),
        sa.Column("outbox_id", sa.String(), nullable=False),
        sa.Column("at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("document", _json_document, nullable=False),
    )
    op.create_index(
        "ix_webhook_delivery_subscription_id", "webhook_delivery", ["subscription_id"]
    )
    op.create_index("ix_webhook_delivery_outbox_id", "webhook_delivery", ["outbox_id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_delivery_outbox_id", table_name="webhook_delivery")
    op.drop_index(
        "ix_webhook_delivery_subscription_id", table_name="webhook_delivery"
    )
    op.drop_table("webhook_delivery")
    op.drop_index("ix_webhook_outbox_state", table_name="webhook_outbox")
    op.drop_index("ix_webhook_outbox_event_type", table_name="webhook_outbox")
    op.drop_index(
        "ix_webhook_outbox_subscription_id", table_name="webhook_outbox"
    )
    op.drop_table("webhook_outbox")
    op.drop_table("webhook_subscription")
