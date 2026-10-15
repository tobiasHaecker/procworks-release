# SPDX-License-Identifier: BUSL-1.1
"""A reset of all data runs while no other request writes.

``POST /admin/reset`` deletes every model, case and event and may seed new
ones. On a database, a case started at the same moment read its model before
the reset deleted it and saved itself afterwards: the case stayed behind
without its model. Now every mutating request passes a write gate of the
database first thing in its unit -- shared; the reset passes it exclusively.
The reset waits for the requests under way (its deletions then remove what
they stored), and requests arriving meanwhile wait until the reset is stored.

The gate is a PostgreSQL advisory lock (released by commit or rollback);
SQLite and the in-memory stores have one writer at a time anyway. Pinned
here: which side of the gate each path takes, and the statements themselves.
The concurrency itself is measured against PostgreSQL with two API processes
(see the change log of this fix).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy.dialects import postgresql
from staffing import staff_via_api

import procworks.api as api_module
from procworks import (
    EventType,
    MailBinding,
    MailOutboxState,
    StaffRule,
    StaffRuleKind,
    add_agent,
    add_role,
    assign_staff_rule,
    create_empty_schema,
    db,
    release,
    serial_insert,
    set_mail_binding,
    unit_of_work,
)
from procworks.api import app

client = TestClient(app)


@pytest.fixture
def gate_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[tuple[str, bool]]]:
    """The API on SQLite; every pass of the gate recorded as (first write after it, exclusive)."""

    use_sql_backend(monkeypatch, tmp_path)
    calls: list[tuple[str, bool]] = []
    original = db.SqlAlchemyInstanceStore.hold_write_gate

    def hold(self: db.SqlAlchemyInstanceStore, *, exclusive: bool) -> None:
        assert unit_of_work.current() is not None, "the gate is passed inside the unit"
        calls.append(("gate", exclusive))
        original(self, exclusive=exclusive)

    monkeypatch.setattr(db.SqlAlchemyInstanceStore, "hold_write_gate", hold)
    yield calls


def _released(name: str) -> str:
    sid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return str(sid)


def test_a_start_passes_the_gate_shared_before_it_reads_the_model(
    gate_calls: list[tuple[str, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _released("Start hinter der Schranke")
    gate_calls.clear()
    original_get = api_module._store.get

    def get(schema_id: str) -> Any:
        gate_calls.append(("model read", False))
        return original_get(schema_id)

    monkeypatch.setattr(api_module._store, "get", get)

    assert client.post(f"/schemas/{sid}/instances").status_code == 201

    assert gate_calls[0] == ("gate", False)  # first, before the model is read
    assert ("gate", True) not in gate_calls


def test_every_mutating_request_passes_the_gate_once(gate_calls: list[tuple[str, bool]]) -> None:
    gate_calls.clear()
    assert client.post("/schemas", json={"name": "Nur ein Entwurf"}).status_code in (200, 201)
    assert gate_calls == [("gate", False)]


def test_the_reset_passes_the_gate_exclusively(gate_calls: list[tuple[str, bool]]) -> None:
    _released("Vor dem Zuruecksetzen")
    gate_calls.clear()

    resp = client.post("/admin/reset", json={"load_demo": False})

    assert resp.status_code == 200, resp.text
    assert gate_calls == [("gate", True)]  # exclusive, and not shared before it
    assert resp.json()["schemas"] == 0 and resp.json()["instances"] == 0


def test_a_start_after_the_reset_finds_its_model_gone(gate_calls: list[tuple[str, bool]]) -> None:
    """The order the gate enforces: a start after the reset gets 404, no case."""

    sid = _released("Vom Zuruecksetzen geloescht")
    assert client.post("/admin/reset", json={"load_demo": False}).status_code == 200

    resp = client.post(f"/schemas/{sid}/instances")

    assert resp.status_code == 404, resp.text
    assert api_module._instances.list_ids() == []


def test_the_escalation_of_a_case_passes_the_gate_shared(
    gate_calls: list[tuple[str, bool]],
) -> None:
    gate_calls.clear()
    unit_of_work.run(
        lambda: api_module._escalate_instance("instance_none", datetime.now(UTC), frozenset())
    )
    assert gate_calls == [("gate", False)]


def test_postgresql_statements_are_transaction_scoped_advisory_locks() -> None:
    shared = db._write_gate_statement("postgresql", exclusive=False)
    exclusive = db._write_gate_statement("postgresql", exclusive=True)
    assert shared is not None and exclusive is not None
    dialect = postgresql.dialect()
    shared_sql = str(shared.compile(dialect=dialect))  # type: ignore[attr-defined]  # a Select
    exclusive_sql = str(exclusive.compile(dialect=dialect))  # type: ignore[attr-defined]  # a Select
    assert "pg_advisory_xact_lock_shared" in shared_sql
    assert "pg_advisory_xact_lock(" in exclusive_sql
    assert db._write_gate_statement("sqlite", exclusive=True) is None


def test_outside_a_unit_the_gate_is_not_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock would end with the method's own session at once -- pointless."""

    store = db.SqlAlchemyInstanceStore(f"sqlite:///{tmp_path / 'x.db'}", create_tables=True)
    asked: list[bool] = []

    def statement(dialect: str, *, exclusive: bool) -> None:
        asked.append(exclusive)
        return None

    monkeypatch.setattr(db, "_write_gate_statement", statement)
    store.hold_write_gate(exclusive=True)
    assert asked == []  # not even looked up
    unit_of_work.run(lambda: store.hold_write_gate(exclusive=True))
    assert asked == [True]


def _mail_case() -> tuple[str, str]:
    """A started case whose first step queued a notification that is due now.

    The send at the start fails (transient), so the entry waits in the
    outbox; its retry is then made due at once. Returns ``(case id, entry id)``.
    """

    schema = create_empty_schema("Benachrichtigung neben dem Zuruecksetzen")
    schema = serial_insert(schema, "Pruefen", after_node_id="start")
    step = next(n.id for n in schema.nodes.values() if n.label == "Pruefen")
    schema = add_role(schema, "SB", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1", email="erika@firma.de")
    schema = assign_staff_rule(schema, step, StaffRule(kind=StaffRuleKind.ROLE, ref="sb"))
    schema = set_mail_binding(schema, step, MailBinding(subject="Neue Aufgabe"))
    api_module._store.put(release(schema))
    started = client.post(f"/schemas/{schema.id}/instances")
    assert started.status_code == 201, started.text
    [entry] = api_module._mail_outbox_store.list_entries()
    assert entry.state is MailOutboxState.FAILED  # the first send failed
    entry.next_attempt_at = 0.0
    api_module._mail_outbox_store.put_entry(entry)
    return str(started.json()["id"]), entry.id


class _FailingSender:
    """The first send at the start fails, so the entry waits for its retry."""

    def send(self, message: Any) -> None:
        raise RuntimeError("smtp down")


class _ResetWhileSending:
    """Sends -- and while the mail is on its way, an admin resets all data."""

    def __init__(self) -> None:
        self.sent = 0

    def send(self, message: Any) -> None:
        self.sent += 1
        assert client.post("/admin/reset", json={"load_demo": False}).status_code == 200


def test_a_mail_sent_beside_a_reset_records_no_event_for_the_wiped_case(
    gate_calls: list[tuple[str, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outcome of a send is audited behind the gate, for a case that still exists.

    The dispatch reads its entry before the send and records ``mail.sent``
    after it, outside any unit. A reset between the two wiped the case and
    the log -- and the event was then appended to the new log, for a case
    that no longer existed.
    """

    monkeypatch.setattr(api_module, "_mail_sender", _FailingSender())
    iid, _entry = _mail_case()
    sender = _ResetWhileSending()
    monkeypatch.setattr(api_module, "_mail_sender", sender)
    gate_calls.clear()

    api_module._dispatch_mail_outbox()

    assert sender.sent == 1
    assert api_module._instances.get(iid) is None  # wiped by the reset
    assert api_module._audit.list_all() == []  # no event survives the reset
    assert ("gate", True) in gate_calls  # the reset ran ...
    assert gate_calls[-1] == ("gate", False)  # ... and the outcome passed the gate after it


def test_a_mail_outcome_of_an_existing_case_is_still_audited(
    gate_calls: list[tuple[str, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "_mail_sender", _FailingSender())
    iid, _entry = _mail_case()
    sent: list[Any] = []

    class _Sender:
        def send(self, message: Any) -> None:
            sent.append(message)

    monkeypatch.setattr(api_module, "_mail_sender", _Sender())
    gate_calls.clear()

    api_module._dispatch_mail_outbox()

    assert len(sent) == 1
    events = [e for e in api_module._audit.for_instance(iid) if e.event_type is EventType.MAIL_SENT]
    assert len(events) == 1
    assert gate_calls == [("gate", False)]  # the audit unit passed the gate shared
