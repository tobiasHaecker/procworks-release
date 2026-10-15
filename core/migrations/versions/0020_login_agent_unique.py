# SPDX-License-Identifier: BUSL-1.1
"""one login per person, held by a unique index (auth_user.agent_id)

"A person has at most one login" was checked by reading all logins and then
storing the new one; two creations at the same moment both read "free" and
both stored. This revision adds the unique partial index
``ux_auth_user_agent_id`` on ``auth_user(agent_id)`` for logins bound to a
person (``agent_id IS NOT NULL``) -- logins without a person stay unlimited.

**Existing double bindings are kept** (deleting a login is the
administrator's decision, and it may be in use). For each person with
several logins the alphabetically first one stays under the index; the
others get the mark ``legacy_duplicate`` (a new column, ``false`` for every
other row), and the index leaves marked rows out (``agent_id IS NOT NULL
AND NOT legacy_duplicate``). So no further login can be added for such a
person either, and the API names these persons in its log at start until
they are cleaned up. The migration prints the same list.

The exemption is a property of the row, never of a name: an earlier form
listed the login names in the index predicate, so a login created later
under a deleted exempted name was exempt as well (two logins for a person
again), and the names stayed in the schema for good. Only this migration
sets the mark; the application never does.

The downgrade drops the index and the column; no login is touched.

Revision ID: 0020_login_agent_unique
Revises: 0019_idempotency_key
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020_login_agent_unique"
down_revision: str | None = "0019_idempotency_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ux_auth_user_agent_id"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT agent_id, login FROM auth_user WHERE agent_id IS NOT NULL "
            "ORDER BY agent_id, login"
        )
    ).all()
    by_agent: dict[str, list[str]] = {}
    for agent_id, login in rows:
        by_agent.setdefault(str(agent_id), []).append(str(login))
    extras = sorted(login for logins in by_agent.values() for login in logins[1:])
    op.add_column(
        "auth_user",
        sa.Column(
            "legacy_duplicate", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    if extras:
        bind.execute(
            sa.text("UPDATE auth_user SET legacy_duplicate = :yes WHERE login IN :logins")
            .bindparams(sa.bindparam("logins", expanding=True)),
            {"yes": True, "logins": extras},
        )
        doubled = {agent: len(logins) for agent, logins in by_agent.items() if len(logins) > 1}
        print(  # noqa: T201 -- the operator reads the migration's output
            f"auth_user: {len(doubled)} person(s) keep more than one login "
            f"({', '.join(f'{a}: {n}' for a, n in doubled.items())}); "
            "delete the extra logins in the user administration."
        )
    predicate = "agent_id IS NOT NULL AND NOT legacy_duplicate"
    op.create_index(
        _INDEX,
        "auth_user",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text(predicate),
        sqlite_where=sa.text(predicate),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="auth_user")
    # A database migrated by the earlier form of this revision (exemptions by
    # name) has no such column; its downgrade only drops the index.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("auth_user")}
    if "legacy_duplicate" in columns:
        with op.batch_alter_table("auth_user") as batch:
            batch.drop_column("legacy_duplicate")
