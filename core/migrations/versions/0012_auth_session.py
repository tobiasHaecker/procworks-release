# SPDX-License-Identifier: BUSL-1.1
"""login sessions survive a restart (auth_session)

Adds the ``auth_session`` table for password-mode sessions. Before, sessions
lived only in the API process, so every restart or update logged everybody out
(Validierung aus Aussensicht 2026-09-25, VAL-11). Only the SHA-256 digest of a
token is stored, never the token. Purely additive -- no existing table is
touched.

Revision ID: 0012_auth_session
Revises: 0011_process_template
Create Date: 2026-09-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_auth_session"
down_revision: str | None = "0011_process_template"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_session",
        sa.Column("digest", sa.String(), primary_key=True),
        sa.Column("login", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_session_login", "auth_session", ["login"])


def downgrade() -> None:
    op.drop_index("ix_auth_session_login", table_name="auth_session")
    op.drop_table("auth_session")
