# SPDX-License-Identifier: BUSL-1.1
"""A connection lost during the commit: the answer says the outcome is unknown.

When the database stops answering, the API gives up on the waiting request
(statement timeout, stall watchdog) and answers ``503 DB.unavailable`` --
"nothing was done, please retry shortly". That is true for everything that
broke off *before* the commit: without a commit the server rolls the
transaction back. But if the connection breaks off while the ``COMMIT``
itself is on its way, the server may still execute it -- measured with a
frozen PostgreSQL, the change was stored after the database came back. A
client that followed "please retry" created the object a second time.

Pinned here:

* a connection lost during the commit answers ``503`` with the code
  ``DB.outcome-unknown``, without ``Retry-After`` and without "retry" in
  the message -- also when the change *was* stored (the case that matters);
* a unit with such a commit is not repeated by the server either;
* a connection lost before the commit stays ``503 DB.unavailable`` with
  ``Retry-After``, and nothing is stored;
* a commit the database refused with a reason of its own (a SQLSTATE such
  as a serialization failure) is no unknown outcome -- the transaction is
  rolled back for sure;
* the client has a German text for the code that says to check first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from web_vm import needs_node, run_app_js

import procworks.api as api_module
from procworks import unit_of_work
from procworks.unit_of_work import CommitOutcomeUnknown

client = TestClient(api_module.app, raise_server_exceptions=False)


def _lost_connection(statement: str) -> OperationalError:
    """The error SQLAlchemy raises when the connection broke off (a disconnect)."""

    return OperationalError(
        statement, {}, Exception("server closed the connection unexpectedly"),
        connection_invalidated=True,
    )


class _ServerError(Exception):
    """A driver error the server reported, with its SQLSTATE (like psycopg)."""

    sqlstate = "40001"


def _names() -> set[str]:
    resp = client.get("/schema-summaries")
    assert resp.status_code == 200, resp.text
    return {s["name"] for s in resp.json()}


def _commit_then_lose_answer(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Let the next commit happen, then report the connection as lost.

    That is the measured case: the ``COMMIT`` reached the server and was
    executed, but the answer never came back.
    """

    original = Session.commit
    calls: list[int] = []

    def commit(self: Session) -> None:
        calls.append(1)
        original(self)
        if len(calls) == 1:
            raise _lost_connection("COMMIT")

    monkeypatch.setattr(Session, "commit", commit)
    return calls


def test_a_connection_lost_during_the_commit_answers_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_sql_backend(monkeypatch, tmp_path)
    calls = _commit_then_lose_answer(monkeypatch)

    resp = client.post("/schemas", json={"name": "Ungewiss gespeichert"})

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert (detail["code"], detail["params"]) == ("DB.outcome-unknown", {})
    assert "retry" not in detail["message"].lower()
    assert "Retry-After" not in resp.headers
    # The reason the answer must not invite a retry: the change is stored.
    assert "Ungewiss gespeichert" in _names()
    assert len(calls) == 1  # not repeated by the server


def test_a_connection_lost_before_the_commit_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Negative: broken off before the commit -- nothing stored, retry is right."""

    use_sql_backend(monkeypatch, tmp_path)
    real_put = api_module._store.put

    def put_then_lose(schema: Any) -> Any:
        real_put(schema)
        raise _lost_connection("UPDATE process_schema")

    monkeypatch.setattr(api_module._store, "put", put_then_lose)

    resp = client.post("/schemas", json={"name": "Vor dem Commit abgebrochen"})

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "DB.unavailable"
    assert resp.headers["Retry-After"] == str(api_module.DB_RETRY_AFTER_SECONDS)
    monkeypatch.setattr(api_module._store, "put", real_put)
    assert "Vor dem Commit abgebrochen" not in _names()


def test_a_flush_that_loses_the_connection_is_before_the_commit() -> None:
    """Edge: the last writes go out before ``COMMIT`` -- a break there is known."""

    class Broken:
        def flush(self) -> None:
            raise _lost_connection("INSERT INTO process_schema")

        def commit(self) -> None:  # pragma: no cover - must not be reached
            raise AssertionError("commit after a failed flush")

    uow = unit_of_work.UnitOfWork()
    uow._sessions[object()] = Broken()  # type: ignore[index]  # stand-in session

    with pytest.raises(OperationalError):
        uow._commit()


def test_a_commit_refused_by_the_server_is_no_unknown_outcome() -> None:
    """Negative: a SQLSTATE answer means the server rolled back -- no doubt."""

    class Refusing:
        def flush(self) -> None:
            pass

        def commit(self) -> None:
            raise OperationalError("COMMIT", {}, _ServerError("could not serialize access"))

    uow = unit_of_work.UnitOfWork()
    uow._sessions[object()] = Refusing()  # type: ignore[index]  # stand-in session

    with pytest.raises(OperationalError) as exc:
        uow._commit()
    assert not isinstance(exc.value, CommitOutcomeUnknown)
    assert unit_of_work.is_transient_collision(exc.value)


def test_a_lost_commit_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server never repeats a unit whose commit may have happened."""

    attempts: list[int] = []

    def commit(self: unit_of_work.UnitOfWork) -> None:
        raise CommitOutcomeUnknown() from _lost_connection("COMMIT")

    monkeypatch.setattr(unit_of_work.UnitOfWork, "_commit", commit)

    with pytest.raises(CommitOutcomeUnknown):
        unit_of_work.run(lambda: attempts.append(1))
    assert attempts == [1]


@needs_node
def test_the_client_says_to_check_before_repeating(tmp_path: Path) -> None:
    title = run_app_js(
        "return describeError({ status: 503, detail: { message: 'x', "
        "code: 'DB.outcome-unknown', params: {} } }).title;",
        tmp_path,
    )
    assert "unklar" in title
    assert "prüfen" in title and "noch einmal versuchen" not in title
