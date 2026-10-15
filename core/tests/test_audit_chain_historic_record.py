# SPDX-License-Identifier: BUSL-1.1
"""The history from before the update is fixed when the update runs, and
anchors are written one after another.

Before appending was serialised, two requests could link their events to the
same end of the chain; those *historic* branches are reported, and an
administrator closes them with an anchor that records what it saw. Until
that first anchor, though, the history was held to nothing: an event at the
end of a historic branch (no later event names its hash) could be removed or
rewritten together with its own hash without breaking a link -- and the next
anchor then recorded the forged state as reviewed.

Pinned here:

* **The update records the historic region.** The chain end carries the
  number of events up to ``serialized_since`` and a digest over their
  numbers and hashes (:func:`procworks.audit.historic_fingerprint`). The
  migration writes it, and so does a database that creates the chain end on
  first use. The check compares the stored events with it on every run --
  before any anchor as well as after one -- and reports a removal, a
  rewrite with recomputed hash, or an event slipped into a gap as
  ``historic-range-changed``. An anchor is then refused.
* **Anchors are serialised.** The anchor endpoint checks the chain and
  appends its event while holding the lock of the chain end; a second anchor
  started at the same moment waits, then finds nothing open (409).
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy import create_engine, delete, text, update
from test_audit_chain import _chain, _event, _kinds, _plant, _rehashed

import procworks.api_admin as api_admin_module
from procworks.api import app
from procworks.audit import (
    AuditEvent,
    ChainState,
    EventType,
    InMemoryAuditLog,
    check_chain,
    historic_fingerprint,
    verify_chain,
)
from procworks.db import AuditEventRow, SqlAlchemyAuditLog

_CORE = Path(__file__).resolve().parents[1]

client = TestClient(app)


def _recorded_state(events: list[AuditEvent], serialized_since: int) -> ChainState:
    """The chain end as the update records it over ``events``."""

    count, digest = historic_fingerprint(
        (e.seq, e.entry_hash) for e in events if e.seq <= serialized_since
    )
    newest = events[-1]
    return ChainState(
        head_hash=newest.entry_hash,
        head_seq=newest.seq,
        serialized_since=serialized_since,
        historic_events=count,
        historic_digest=digest,
    )


# --- the fingerprint itself ----------------------------------------------------------


def test_the_fingerprint_depends_on_every_number_and_hash() -> None:
    base = historic_fingerprint([(1, "a"), (2, "b")])

    assert base[0] == 2
    assert historic_fingerprint([(1, "a"), (2, "b")]) == base
    assert historic_fingerprint([(1, "a")])[1] != base[1]
    assert historic_fingerprint([(1, "a"), (2, "c")])[1] != base[1]
    assert historic_fingerprint([(1, "a"), (3, "b")])[1] != base[1]
    # no ambiguity between number and hash at the separator
    assert historic_fingerprint([(1, "1:a")]) != historic_fingerprint([(11, ":a")])
    assert historic_fingerprint([]) == (0, historic_fingerprint([])[1])


# --- the check before any anchor -----------------------------------------------------


def _history() -> list[AuditEvent]:
    """1..7 with a historic branch at 4 (to 2): 3 is a dead end. No anchor."""

    return _chain([1, 2, 3, 4, 5, 6, 7], links={4: 2})


def test_the_untouched_history_stays_open_without_a_defect() -> None:
    events = _history()

    report = verify_chain(events, _recorded_state(events, 7))

    assert report.status == "open"
    assert report.defects == []
    assert report.historic_events == 7


def test_removing_a_dead_end_before_any_anchor_is_found() -> None:
    events = _history()
    state = _recorded_state(events, 7)
    del events[2]  # number 3, the dead end: no link breaks

    report = verify_chain(events, state)

    assert report.status == "defective"
    assert _kinds(report) == [("historic-range-changed", 7)]
    assert "7 events" in report.defects[0].message
    assert "now 6" in report.defects[0].message


def test_a_dead_end_rewritten_with_its_hash_before_any_anchor_is_found() -> None:
    events = _history()
    state = _recorded_state(events, 7)
    events[2] = _rehashed(events[2], instance_id="gefälscht")

    report = verify_chain(events, state)

    assert _kinds(report) == [("historic-range-changed", 7)]


def test_an_event_slipped_into_a_gap_of_the_history_is_found() -> None:
    events = _chain([1, 2, 3, 5, 6, 7], links={5: 2})
    state = _recorded_state(events, 7)
    forged = _event(4, events[2].entry_hash)  # continues the dead end 3
    tampered = sorted([*events, forged], key=lambda e: e.seq)

    report = verify_chain(tampered, state)

    assert _kinds(report) == [("historic-range-changed", 7)]


def test_events_after_the_historic_region_do_not_touch_the_record() -> None:
    events = _history()
    state = _recorded_state(events, 7)
    events.append(_event(8, events[-1].entry_hash))
    state = ChainState(
        head_hash=events[-1].entry_hash, head_seq=8, serialized_since=7,
        historic_events=state.historic_events, historic_digest=state.historic_digest,
    )

    assert verify_chain(events, state).defects == []


def test_a_state_without_a_record_checks_nothing_extra() -> None:
    """In memory, and for a chain end the caller built without a record."""

    events = _history()
    del events[2]
    state = ChainState(
        head_hash=events[-1].entry_hash, head_seq=7, serialized_since=7,
    )

    assert "historic-range-changed" not in {kind for kind, _ in _kinds(verify_chain(events, state))}
    assert InMemoryAuditLog().chain_state().historic_digest is None


# --- the record on the database ------------------------------------------------------


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(_CORE / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_CORE, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr


def _columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(audit_chain)"))
            return {row[1] for row in rows}
    finally:
        engine.dispose()


def _rows_of(url: str) -> list[tuple[Any, ...]]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text("SELECT * FROM audit_event ORDER BY seq"))]
    finally:
        engine.dispose()


def _delete_event(url: str, seq: int) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(delete(AuditEventRow.__table__).where(AuditEventRow.__table__.c.seq == seq))
    engine.dispose()


def _replace_event(url: str, event: AuditEvent) -> None:
    table = AuditEventRow.__table__
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            update(table)
            .where(table.c.seq == event.seq)
            .values(instance_id=event.instance_id, entry_hash=event.entry_hash)
        )
    engine.dispose()


def test_the_migration_records_the_history_and_a_later_removal_is_found(
    tmp_path: Path,
) -> None:
    pytest.importorskip("alembic")
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    _alembic(url, "upgrade", "0015_external_task_open_step")
    planted = _history()
    _plant(url, planted)

    _alembic(url, "upgrade", "head")

    log = SqlAlchemyAuditLog(url)
    state = log.chain_state()
    assert (state.serialized_since, state.historic_events) == (7, 7)
    assert state.historic_digest == historic_fingerprint(
        (e.seq, e.entry_hash) for e in planted
    )[1]
    assert check_chain(log).status == "open"
    log.append(EventType.INSTANCE_CREATED, "nach-dem-update", "s1")
    assert check_chain(log).defects == []

    _delete_event(url, 3)

    report = check_chain(log)
    assert report.status == "defective"
    assert _kinds(report) == [("historic-range-changed", 7)]


def test_the_downgrade_drops_only_the_record(tmp_path: Path) -> None:
    pytest.importorskip("alembic")
    url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    _alembic(url, "upgrade", "0015_external_task_open_step")
    _plant(url, _history())
    _alembic(url, "upgrade", "head")
    assert {"historic_events", "historic_digest"} <= _columns(url)

    _alembic(url, "downgrade", "0016_audit_chain")

    columns = _columns(url)
    assert "historic_events" not in columns
    assert {"head_hash", "head_seq", "serialized_since"} <= columns
    engine = create_engine(url)  # read as the older version would: plain columns
    with engine.connect() as conn:
        end = conn.execute(text("SELECT head_seq, serialized_since FROM audit_chain")).one()
    engine.dispose()
    assert tuple(end) == (7, 7)
    assert len(_rows_of(url)) == 7
    _alembic(url, "upgrade", "head")
    assert SqlAlchemyAuditLog(url).chain_state().historic_events == 7


def test_a_chain_end_created_on_first_use_records_the_history(tmp_path: Path) -> None:
    """Without Alembic (``create_all``), the first append creates the chain
    end -- with the same record as the migration."""

    url = f"sqlite:///{tmp_path / 'audit.db'}"
    log = SqlAlchemyAuditLog(url, create_tables=True)
    planted = _history()
    _plant(url, planted)
    log.append(EventType.INSTANCE_CREATED, "erstes", "s1")

    state = log.chain_state()
    assert (state.serialized_since, state.historic_events) == (7, 7)
    _replace_event(url, _rehashed(planted[2], instance_id="gefälscht"))

    assert _kinds(check_chain(log)) == [("historic-range-changed", 7)]


# --- the endpoint ----------------------------------------------------------------------


@pytest.fixture
def sql_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    return use_sql_backend(monkeypatch, tmp_path)


def _anchors() -> list[AuditEvent]:
    import procworks.api as api_module

    return [e for e in api_module._audit.list_all() if e.event_type is EventType.CHAIN_ANCHOR]


def _start_chain_end(url: str) -> None:
    """One ordinary event after the planted history: the chain end now exists
    and records the history, as after an update."""

    SqlAlchemyAuditLog(url).append(EventType.INSTANCE_CREATED, "nach-dem-update", "s1")


def test_an_anchor_over_a_dead_end_rewritten_before_it_is_refused(sql_api: str) -> None:
    """The probe of the follow-up test: a dead end rewritten together with its
    hash before the first anchor -- the anchor would record the forgery."""

    planted = _history()
    _plant(sql_api, planted)
    _start_chain_end(sql_api)
    _replace_event(sql_api, _rehashed(planted[2], instance_id="gefälscht"))

    report = client.get("/admin/audit-chain").json()
    assert report["status"] == "defective"
    assert [(d["kind"], d["seq"]) for d in report["defects"]] == [("historic-range-changed", 7)]

    resp = client.post("/admin/audit-chain/anchor", json={"reason": "geprüft"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "AUD.chain-defective"
    assert _anchors() == []


def test_the_untouched_history_is_anchored_as_before(sql_api: str) -> None:
    _plant(sql_api, _history())
    _start_chain_end(sql_api)

    resp = client.post("/admin/audit-chain/anchor", json={"reason": "geprüft"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "intact"
    assert resp.json()["defects"] == []


def _gate_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the first two chain checks of the anchor endpoint until both
    arrived -- the moment at which two anchors both saw the branches open."""

    original: Callable[..., Any] = api_admin_module.verify_chain
    barrier = threading.Barrier(2, timeout=1.0)
    calls = [0]
    lock = threading.Lock()

    def gated(*args: Any, **kwargs: Any) -> Any:
        with lock:
            calls[0] += 1
            call = calls[0]
        if call <= 2:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return original(*args, **kwargs)

    monkeypatch.setattr(api_admin_module, "verify_chain", gated)


def test_two_anchors_at_once_write_one(sql_api: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _plant(sql_api, _history())
    _start_chain_end(sql_api)
    _gate_check(monkeypatch)
    answers: list[Any] = []

    def anchor(reason: str) -> None:
        answers.append(client.post("/admin/audit-chain/anchor", json={"reason": reason}))

    threads = [threading.Thread(target=anchor, args=(f"Prüfung {n}",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(a.status_code for a in answers) == [200, 409]
    (refused,) = [a for a in answers if a.status_code == 409]
    assert refused.json()["detail"]["code"] == "AUD.chain-nothing-open"
    assert len(_anchors()) == 1
    assert client.get("/admin/audit-chain").json()["status"] == "intact"


# --- the record itself: shown, and pinned by the anchor --------------------------------
#
# The record lives in the same database as the events. Before the first anchor
# whoever can rewrite an event can recompute the record as well -- the check
# alone cannot tell (documented limit). So the check shows the record (count
# and digest) for the operator to keep outside the database, and every anchor
# records it in its own hashed detail: from then on a changed record is a
# defect of its own.


def _anchored_history() -> tuple[list[AuditEvent], ChainState]:
    """The history plus an anchor written by :func:`anchor_record`."""

    from test_audit_chain import _event as event_of

    from procworks.audit import anchor_record

    events = _history()
    state = _recorded_state(events, 7)
    record = anchor_record(events, verify_chain(events, state))
    anchor = event_of(8, events[-1].entry_hash, event_type=EventType.CHAIN_ANCHOR,
                      detail={**record, "checked_through": "7", "reason": "geprüft",
                              "actor": "admin"})
    events.append(anchor)
    return events, dataclasses.replace(state, head_hash=anchor.entry_hash, head_seq=8)


def test_the_check_shows_count_and_digest_of_the_record() -> None:
    events = _history()
    state = _recorded_state(events, 7)

    report = verify_chain(events, state)

    assert (report.historic_events, report.historic_digest) == (
        state.historic_events, state.historic_digest
    )


def test_the_anchor_records_the_historic_record() -> None:
    from procworks.audit import anchor_record

    events = _history()
    state = _recorded_state(events, 7)

    record = anchor_record(events, verify_chain(events, state))

    assert record["historic_events"] == "7"
    assert record["historic_digest"] == state.historic_digest


def test_an_anchor_without_a_record_records_none() -> None:
    """Grenzfall: in memory there is no record -- the anchor carries no such keys."""

    from procworks.audit import anchor_record

    events = _history()
    state = ChainState(head_hash=events[-1].entry_hash, head_seq=7, serialized_since=7)

    record = anchor_record(events, verify_chain(events, state))

    assert "historic_digest" not in record and "historic_events" not in record


def test_the_anchored_history_is_intact() -> None:
    events, state = _anchored_history()
    report = verify_chain(events, state)
    assert report.status == "intact", report.defects


def test_a_record_changed_after_the_anchor_is_a_defect() -> None:
    """Event *and* record rewritten after the anchor: the anchor's copy differs."""

    events, state = _anchored_history()
    forged = events[:]
    forged[4] = _rehashed(forged[4], instance_id="gefälscht")  # number 5, linked by 6
    count, digest = historic_fingerprint((e.seq, e.entry_hash) for e in forged if e.seq <= 7)
    rewritten = dataclasses.replace(state, historic_events=count, historic_digest=digest)

    kinds = {kind for kind, _ in _kinds(verify_chain(forged, rewritten))}

    assert "historic-record-changed" in kinds


def test_only_the_record_changed_after_the_anchor_is_found_too() -> None:
    events, state = _anchored_history()
    changed = dataclasses.replace(state, historic_digest="0" * 64)

    report = verify_chain(events, changed)

    assert ("historic-record-changed", 8) in _kinds(report)


def test_a_record_removed_after_the_anchor_is_found() -> None:
    events, state = _anchored_history()
    removed = dataclasses.replace(state, historic_events=None, historic_digest=None)

    assert ("historic-record-changed", 8) in _kinds(verify_chain(events, removed))


def test_the_endpoint_shows_the_digest_and_the_anchor_records_it(sql_api: str) -> None:
    _plant(sql_api, _history())
    _start_chain_end(sql_api)
    shown = client.get("/admin/audit-chain").json()
    assert shown["historic_events"] == 7
    assert isinstance(shown["historic_digest"], str) and len(shown["historic_digest"]) == 64

    resp = client.post("/admin/audit-chain/anchor", json={"reason": "geprüft"})

    assert resp.status_code == 200, resp.text
    [anchor] = _anchors()
    assert anchor.detail["historic_digest"] == shown["historic_digest"]
    assert anchor.detail["historic_events"] == "7"
