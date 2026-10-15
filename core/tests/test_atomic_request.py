# SPDX-License-Identifier: BUSL-1.1
"""A request on a database is all or nothing: case, audit and outbox together.

Starting a case writes the case, its ``INSTANCE_CREATED`` event, the
``instance.started`` outbox entry and the activation stamps. Each of these used
to be its own transaction: a failure in the middle (database gone for a
moment, an audit write that fails) left a case behind that the caller saw as
failed -- without creation event, without activation stamp (no due date, no
escalation) -- and a retry created a second one.

On a database every mutating request is now one transaction: the case is only
there if everything that belongs to it is there. Pinned with a failure
injected after the case was saved -- once an unexpected error (500), once a
database error (503) -- for the start and for the completion of a step, plus
the unchanged normal case. The in-memory stores keep their behaviour (no
transactions there).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy.exc import OperationalError
from staffing import staff_via_api

import procworks.api as api_module
from procworks import db
from procworks.api import app
from procworks.audit import EventType
from procworks.outbox import OutboxDispatcher

client = TestClient(app)
#: Unexpected errors as the HTTP answer they become (500), not as a raise.
lenient = TestClient(app, raise_server_exceptions=False)


class _Recorder:
    """Webhook transport that records instead of sending (no network)."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        del url, headers, timeout
        self.sent.append(body)
        return 200


@pytest.fixture(autouse=True)
def recorder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Recorder:
    """The API on SQLite, with a subscriber of ``instance.started``.

    The subscription makes every start write an outbox entry, so a test can
    see whether that entry stayed behind.
    """

    url = use_sql_backend(monkeypatch, tmp_path)
    sent = _Recorder()
    outbox = OutboxDispatcher(db.SqlAlchemyWebhookStore(url, create_tables=True), transport=sent)
    outbox.subscribe("https://hooks.example.org/procworks", ["instance.started"], "")
    monkeypatch.setattr(api_module, "_outbox", outbox)
    return sent


def _outbox_entries_of(sid: str) -> list[object]:
    return [
        e
        for e in api_module._outbox._store.list_entries()
        if e.payload.get("schema_id") == sid
    ]


def _released(name: str) -> tuple[str, str]:
    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, act


def _instances_of(sid: str) -> list[str]:
    return [
        i
        for i in api_module._instances.list_ids()
        if (inst := api_module._instances.get(i)) is not None and inst.schema_id == sid
    ]


def _fail_on(
    monkeypatch: pytest.MonkeyPatch, event_type: EventType, error: Exception
) -> Any:
    """Let the audit write of ``event_type`` fail -- after the case was saved.

    :returns: the original ``append``, to end the fault within the test.
    """

    original = api_module._audit.append

    def append(kind: EventType, *args: Any, **kwargs: Any) -> Any:
        if kind is event_type:
            raise error
        return original(kind, *args, **kwargs)

    monkeypatch.setattr(api_module._audit, "append", append)
    return original


def test_a_normal_start_stores_the_case_with_its_event_and_stamp(recorder: _Recorder) -> None:
    sid, act = _released("Anlage ohne Störung")

    resp = client.post(f"/schemas/{sid}/instances")

    assert resp.status_code == 201
    iid = resp.json()["id"]
    assert _instances_of(sid) == [iid]
    assert len(_outbox_entries_of(sid)) == 1
    assert len(recorder.sent) == 1  # delivered once the case was stored
    created = [
        e
        for e in api_module._audit.for_instance(iid)
        if e.event_type is EventType.INSTANCE_CREATED
    ]
    assert len(created) == 1
    assert act in client.get(f"/instances/{iid}").json()["node_activated_at"]


def test_a_failing_audit_write_leaves_no_case_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    sid, _ = _released("Anlage, Audit scheitert")
    _fail_on(monkeypatch, EventType.INSTANCE_CREATED, RuntimeError("audit write failed"))

    resp = lenient.post(f"/schemas/{sid}/instances")

    assert resp.status_code == 500
    assert _instances_of(sid) == []
    assert _outbox_entries_of(sid) == []


def test_a_failure_after_case_event_and_outbox_entry_leaves_none_of_them(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """The strongest case: all three were written before the fault."""

    sid, _ = _released("Anlage, Fehler nach allen dreien")

    def notify(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("mail queue failed")

    monkeypatch.setattr(api_module, "_notify_ready", notify)

    resp = lenient.post(f"/schemas/{sid}/instances")

    assert resp.status_code == 500
    assert _instances_of(sid) == []
    assert not any(
        e.schema_id == sid and e.event_type is EventType.INSTANCE_CREATED
        for e in api_module._audit.list_all()
    )
    assert _outbox_entries_of(sid) == []
    assert recorder.sent == []  # nothing announced for a case that does not exist


def test_a_database_error_during_the_start_answers_503_and_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database going away mid-request: a clean 503, no half-stored case."""

    sid, _ = _released("Anlage, Datenbank weg")
    outage = OperationalError("INSERT INTO audit_event", {}, Exception("server closed"))
    _fail_on(monkeypatch, EventType.INSTANCE_CREATED, outage)

    resp = lenient.post(f"/schemas/{sid}/instances")

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "DB.unavailable"
    assert _instances_of(sid) == []


def test_a_failing_outbox_write_leaves_neither_case_nor_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid, _ = _released("Anlage, Outbox scheitert")

    def emit(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(api_module._outbox, "emit", emit)

    resp = lenient.post(f"/schemas/{sid}/instances")

    assert resp.status_code == 500
    assert _instances_of(sid) == []
    assert not any(
        e.schema_id == sid and e.event_type is EventType.INSTANCE_CREATED
        for e in api_module._audit.list_all()
    )


def test_a_failing_audit_write_leaves_the_step_open(monkeypatch: pytest.MonkeyPatch) -> None:
    sid, act = _released("Abschluss, Audit scheitert")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    original = _fail_on(
        monkeypatch, EventType.ACTIVITY_COMPLETED, RuntimeError("audit write failed")
    )

    resp = lenient.post(f"/instances/{iid}/complete", json={"node_id": act})

    assert resp.status_code == 500
    stored = client.get(f"/instances/{iid}").json()
    assert stored["state"] == "RUNNING"
    assert stored["node_states"][act] == "ACTIVATED"
    assert not any(
        e.event_type is EventType.INSTANCE_COMPLETED for e in api_module._audit.for_instance(iid)
    )

    # Once the fault is gone, the same completion goes through -- exactly once.
    monkeypatch.setattr(api_module._audit, "append", original)
    assert client.post(f"/instances/{iid}/complete", json={"node_id": act}).status_code == 200
    completed = [
        e
        for e in api_module._audit.for_instance(iid)
        if e.event_type is EventType.ACTIVITY_COMPLETED
    ]
    assert len(completed) == 1
