# SPDX-License-Identifier: BUSL-1.1
"""Inside a unit of work the audit chain end is locked last, right before the commit.

The database log locked the end of the hash chain at the first event of a
unit and held the lock until the unit committed. A long unit (a mass
migration, a reset with sample data) therefore made every writer of every API
process wait for its whole duration -- up to the statement timeout -- and two
units that took a case row and the chain end in opposite order deadlocked (a
completion: case first, then chain; an escalation: chain first, then case).

Now a unit collects its events and chains them as its last step before the
commit. Pinned here on SQLite (the same code as PostgreSQL):

* the chain end is locked after every other write of the unit, once per unit;
* the chain stays one line, gap-free, with stamps in chain order -- and atomic
  with the data (a rolled-back unit chains nothing; an answer that commits
  chains what came before it);
* the unit reads its own events before the commit (involvement check, seeds);
* a clear inside the unit drops what came before it, the events after it
  start the new chain;
* outside a unit, and with ``immediate``, an event is chained at once.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from staffing import staff_via_api

import procworks.api as api_module
from procworks import db, unit_of_work
from procworks.api import app
from procworks.audit import EventType, check_chain

client = TestClient(app)


@pytest.fixture
def log(tmp_path: Path) -> db.SqlAlchemyAuditLog:
    return db.SqlAlchemyAuditLog(f"sqlite:///{tmp_path / 'audit.db'}", create_tables=True)


@pytest.fixture
def lock_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record every lock of the chain end (``"lock"``) in one timeline."""

    timeline: list[str] = []
    original = db.SqlAlchemyAuditLog._lock_chain_end

    def lock(self: db.SqlAlchemyAuditLog, session: Any) -> tuple[str, int]:
        timeline.append("lock")
        return original(self, session)

    monkeypatch.setattr(db.SqlAlchemyAuditLog, "_lock_chain_end", lock)
    yield timeline


def _append(log: db.SqlAlchemyAuditLog, instance_id: str, **kwargs: Any) -> Any:
    return log.append(EventType.INSTANCE_CREATED, instance_id, "schema_1", **kwargs)


# --- the order of the locks --------------------------------------------------------


def test_the_chain_end_is_locked_after_the_units_other_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lock_calls: list[str]
) -> None:
    """A start: case saved, events recorded -- the chain is locked only at the end."""

    use_sql_backend(monkeypatch, tmp_path)
    original_put = api_module._instances.put

    def put(instance: Any) -> Any:
        lock_calls.append("case saved")
        return original_put(instance)

    monkeypatch.setattr(api_module._instances, "put", put)
    sid = client.post("/schemas", json={"name": "Reihenfolge der Sperren"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    lock_calls.clear()

    resp = client.post(f"/schemas/{sid}/instances")

    assert resp.status_code == 201, resp.text
    assert "case saved" in lock_calls
    assert lock_calls[-1] == "lock"  # last, after every save of the case
    assert lock_calls.count("lock") == 1  # one lock for all events of the unit


def test_many_events_of_one_unit_take_the_lock_once(
    log: db.SqlAlchemyAuditLog, lock_calls: list[str]
) -> None:
    def change() -> None:
        for n in range(50):
            _append(log, f"instance_{n}")
        assert lock_calls == []  # nothing locked while the unit works

    unit_of_work.run(change)

    assert lock_calls == ["lock"]
    events = log.list_all()
    assert [e.seq for e in events] == list(range(1, 51))
    assert check_chain(log).status == "intact"


# --- one line, atomic with the data ---------------------------------------------


def test_units_chain_one_gap_free_line_with_stamps_in_order(log: db.SqlAlchemyAuditLog) -> None:
    for unit in range(5):
        unit_of_work.run(lambda unit=unit: [_append(log, f"i{unit}-{n}") for n in range(3)])
    _append(log, "outside")  # outside a unit: chained at once

    events = log.list_all()
    assert [e.seq for e in events] == list(range(1, 17))
    assert all(a.entry_hash == b.prev_hash for a, b in zip(events, events[1:], strict=False))
    assert all(a.timestamp <= b.timestamp for a, b in zip(events, events[1:], strict=False))
    assert check_chain(log).status == "intact"


def test_a_rolled_back_unit_chains_nothing(log: db.SqlAlchemyAuditLog) -> None:
    _append(log, "before")

    def change() -> None:
        _append(log, "lost")
        raise RuntimeError("unexpected")

    with pytest.raises(RuntimeError, match="unexpected"):
        unit_of_work.run(change)

    assert [e.instance_id for e in log.list_all()] == ["before"]
    assert log.chain_state().head_seq == 1
    assert check_chain(log).status == "intact"


def test_a_deliberate_answer_keeps_the_events_before_it(log: db.SqlAlchemyAuditLog) -> None:
    def change() -> None:
        _append(log, "recorded")
        raise HTTPException(status_code=502, detail="external system failed")

    with pytest.raises(HTTPException):
        unit_of_work.run(change, commit_on=(HTTPException,))

    assert [e.instance_id for e in log.list_all()] == ["recorded"]
    assert check_chain(log).status == "intact"


def test_a_repeated_unit_chains_its_events_once(log: db.SqlAlchemyAuditLog) -> None:
    attempts: list[int] = []

    def change() -> None:
        attempts.append(1)
        _append(log, f"attempt-{len(attempts)}")
        if len(attempts) == 1:
            raise unit_of_work.ConcurrentModification("instance", "instance_1")

    unit_of_work.run(change)

    assert [e.instance_id for e in log.list_all()] == ["attempt-2"]


# --- reading inside the unit ---------------------------------------------------------


def test_the_unit_reads_its_own_events_before_the_commit(log: db.SqlAlchemyAuditLog) -> None:
    _append(log, "stored", agent_id="a-old")
    seen: dict[str, Any] = {}

    def change() -> None:
        provisional = _append(log, "new", agent_id="a-new")
        seen["provisional"] = provisional
        seen["for_instance"] = log.for_instance("new")
        seen["list_all"] = [e.instance_id for e in log.list_all()]
        seen["agents"] = log.instance_ids_for_agent("a-new")
        seen["revision"] = log.revision()

    unit_of_work.run(change)

    assert seen["provisional"].seq == 0 and seen["provisional"].entry_hash == ""
    assert [e.agent_id for e in seen["for_instance"]] == ["a-new"]
    assert seen["list_all"] == ["stored", "new"]
    assert seen["agents"] == {"new"}
    assert seen["revision"] == 2
    stored = log.for_instance("new")
    assert stored[0].seq == 2 and stored[0].entry_hash  # chained with the commit


def test_a_seed_with_explicit_stamps_keeps_them(log: db.SqlAlchemyAuditLog) -> None:
    """Seeds date their history back (``at``); the unit reads and stores it so."""

    back = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    read: list[datetime] = []

    def change() -> None:
        _append(log, "seeded", at=back)
        read.extend(e.timestamp for e in log.for_instance("seeded"))

    unit_of_work.run(change)

    assert read == [back]
    stored = log.for_instance("seeded")[0].timestamp
    assert abs(stored.replace(tzinfo=UTC) - back) < timedelta(seconds=1)


# --- clear, the anchor and the immediate path ---------------------------------------


def test_a_clear_inside_the_unit_drops_what_came_before_and_restarts_the_chain(
    log: db.SqlAlchemyAuditLog,
) -> None:
    _append(log, "old")
    inside: list[list[str]] = []

    def change() -> None:
        _append(log, "dropped")
        log.clear()
        inside.append([e.instance_id for e in log.list_all()])
        _append(log, "fresh")
        inside.append([e.instance_id for e in log.list_all()])

    unit_of_work.run(change)

    assert inside == [[], ["fresh"]]
    events = log.list_all()
    assert [(e.seq, e.instance_id, e.prev_hash) for e in events] == [(1, "fresh", "")]
    assert check_chain(log).status == "intact"


def test_the_anchor_inside_a_unit_comes_after_the_units_events(
    log: db.SqlAlchemyAuditLog,
) -> None:
    seen: list[int] = []

    def decide(events: list[Any], state: Any) -> dict[str, str]:
        seen.append(len(events))
        return {"reason": "test"}

    def change() -> None:
        _append(log, "first")
        log.append_checked(decide, EventType.CHAIN_ANCHOR, "__chain__", "__chain__")
        _append(log, "after")

    unit_of_work.run(change)

    assert seen == [1]  # the unit's earlier event was chained before deciding
    assert [e.instance_id for e in log.list_all()] == ["first", "__chain__", "after"]
    assert check_chain(log).status == "intact"


def test_immediate_returns_the_stored_hash_inside_a_unit(log: db.SqlAlchemyAuditLog) -> None:
    got: list[Any] = []

    def change() -> None:
        _append(log, "collected")
        got.append(_append(log, "anchor", immediate=True))

    unit_of_work.run(change)

    events = log.list_all()
    assert [e.instance_id for e in events] == ["collected", "anchor"]  # order kept
    assert got[0].seq == 2 and got[0].entry_hash == events[1].entry_hash


def test_outside_a_unit_an_event_is_chained_at_once(log: db.SqlAlchemyAuditLog) -> None:
    event = _append(log, "background")

    assert event.seq == 1 and event.entry_hash
    assert log.chain_state().head_hash == event.entry_hash
