# SPDX-License-Identifier: BUSL-1.1
"""An unexpected database error answers 500 with a code, nothing saved.

A database error that is no outage (a violated constraint, a trigger, data
the database refuses) answered a bare ``500 Internal Server Error`` without
a code. The request is rolled back as a whole, which is right -- but a
client could not tell this answer from any other failure, nor show a
sensible message. Now it gets ``500`` with ``detail.code``
``DB.unexpected``, a fixed message without the driver's text, and the log
names the error class. Outages keep their ``503 DB.unavailable``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from web_vm import needs_node, run_app_js

import procworks.api as api_module

client = TestClient(api_module.app, raise_server_exceptions=False)

_DRIVER_TEXT = 'duplicate key value violates unique constraint "process_schema_pkey"'


@pytest.mark.parametrize("error", [IntegrityError, DataError])
def test_an_unexpected_database_error_answers_500_with_a_code(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def broken(*_args: object, **_kwargs: object) -> Any:
        raise error("INSERT INTO process_schema", {}, Exception(_DRIVER_TEXT))

    monkeypatch.setattr(api_module._store, "put", broken)

    resp = client.post("/schemas", json={"name": "Fehler"})

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "DB.unexpected"
    assert detail["params"] == {}
    assert "process_schema" not in resp.text and "duplicate key" not in resp.text


def test_an_outage_stays_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*_args: object, **_kwargs: object) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(api_module._store, "list_ids", down)

    resp = client.get("/schemas")

    assert (resp.status_code, resp.json()["detail"]["code"]) == (503, "DB.unavailable")


def test_nothing_of_the_request_is_saved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """On a real database: the schema write fails after the audit append --
    neither stays (one transaction)."""

    use_sql_backend(monkeypatch, tmp_path)
    before_schemas = set(api_module._store.list_ids())
    before_events = len(api_module._audit.list_all())
    real_put = api_module._store.put

    def put_then_fail(schema: Any) -> Any:
        real_put(schema)
        raise IntegrityError("UPDATE process_schema", {}, Exception(_DRIVER_TEXT))

    monkeypatch.setattr(api_module._store, "put", put_then_fail)

    resp = client.post("/schemas", json={"name": "Halb"})

    assert resp.json()["detail"]["code"] == "DB.unexpected"
    assert set(api_module._store.list_ids()) == before_schemas
    assert len(api_module._audit.list_all()) == before_events


@needs_node
def test_the_client_words_the_code(tmp_path: Path) -> None:
    title = run_app_js(
        "return describeError({ status: 500, detail: { message: 'x', code: 'DB.unexpected', "
        "params: {} } }).title;",
        tmp_path,
    )
    assert "gespeichert wurde nichts" in title
