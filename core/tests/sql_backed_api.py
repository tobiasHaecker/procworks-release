# SPDX-License-Identifier: BUSL-1.1
"""Test helper: run the API module on SQLAlchemy stores of one database file.

The API module builds its stores once at import, from ``DATABASE_URL`` -- in
the test suite that means in-memory stores. Tests that need the transactional
behaviour of a real database (one transaction per request, the optimistic
version check of the SQL stores, a rollback after a failed write) swap every
store the request paths touch for an SQLAlchemy store on one SQLite file, plus
everything that captured a store at import time (the schema and org
resolvers, the engine context with its spawn observer, the two outbox
dispatchers). ``monkeypatch`` restores the originals after the test.

Not a test module (no ``test_`` prefix), imported flat -- ``tests/`` is
deliberately not a package.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import procworks.api as api_module
from procworks import db, mail_runtime
from procworks import execution as exe
from procworks.outbox import OutboxDispatcher
from procworks.store import make_org_resolver, make_resolver


def use_sql_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Point the API module at SQLAlchemy stores on a fresh SQLite file.

    :param monkeypatch: the test's monkeypatch (restores the module afterwards).
    :param tmp_path: the test's directory; the database file is created there.
    :returns: the database URL, for tests that open a second store on it
        (e.g. to play another API process).
    """

    url = f"sqlite:///{tmp_path / 'procworks.db'}"
    schemas = db.SqlAlchemySchemaStore(url, create_tables=True)
    instances = db.SqlAlchemyInstanceStore(url, create_tables=True)
    orgs = db.SqlAlchemyOrgStore(url, create_tables=True)
    mail_store = db.SqlAlchemyMailOutboxStore(url, create_tables=True)
    resolver = make_resolver(schemas)
    replacements: dict[str, object] = {
        "_store": schemas,
        "_instances": instances,
        "_org_store": orgs,
        "_audit": db.SqlAlchemyAuditLog(url, create_tables=True),
        "_external_tasks": db.SqlAlchemyExternalTaskStore(url, create_tables=True),
        "_outbox": OutboxDispatcher(db.SqlAlchemyWebhookStore(url, create_tables=True)),
        "_mail_outbox_store": mail_store,
        "_mail_outbox": mail_runtime.MailOutboxDispatcher(mail_store),
        "_absence_store": db.SqlAlchemyAbsenceStore(url, create_tables=True),
        "_template_store": db.SqlAlchemyTemplateStore(url, create_tables=True),
        "_idempotency": db.SqlAlchemyIdempotencyStore(url, create_tables=True),
        "_resolver": resolver,
        "_org_resolver": make_org_resolver(orgs),
        "_context": exe.ExecutionContext(
            resolver,
            instances,
            on_spawn=lambda spawned, origin: api_module._record_spawned_instance(
                spawned, origin
            ),
        ),
    }
    for name, value in replacements.items():
        monkeypatch.setattr(api_module, name, value)
    return url
