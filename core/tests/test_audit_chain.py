# SPDX-License-Identifier: BUSL-1.1
"""The audit hash chain stays linear under concurrency, and its check tells
historic branches from tampering.

Every audit event names the hash of its predecessor (``prev_hash``), so the
history forms one chain: a changed or removed event breaks it visibly. Two
requests that recorded an event at the same moment used to read the same end
of the chain and both hang their event onto it -- the chain branched, and a
check of it reported tampering where there was none.

Pinned here:

* **Serialised appending.** Two units of work that append at the same moment
  (forced with a barrier inside the hash computation) produce one linear
  chain with gap-free sequence numbers -- on an SQLite file and in memory.
  A restarted store continues from the stored end.
* **The check** (:func:`procworks.audit.verify_chain`): a linear chain is
  intact; a branch written before appending was serialised is reported as a
  *historic branch* with its time and stays visible, and an anchor event
  closes it; a branch after that point is a defect.
* **Tampering is never excused as historic** -- the most important negative
  case: a changed hash, a changed event, a removed event, a removed end, and an
  event slipped in before the anchor all stay defects, each with its own
  reason, also inside the historic region and also after an anchor.
* **The admin endpoints**: reading the check, writing the anchor (reason
  required, refused over defects or when nothing is open), the anchor kept out
  of the figures.
* **What an anchor names stays as it was.** An event at the end of a historic
  branch (a *dead end*: no later event links to it) can be removed or
  rewritten without breaking any link. The anchor therefore records every
  open branch and every dead end with its hash; the check requires each of
  them to be still there, unchanged, and every named branch to be still a
  branch -- and no dead end the anchor does not name.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy import create_engine, delete, insert

import procworks.api as api_module
from procworks import audit as audit_module
from procworks import db as db_module
from procworks import unit_of_work
from procworks.api import app
from procworks.audit import (
    NON_PROCESS_EVENTS,
    AuditEvent,
    ChainBranch,
    ChainReport,
    ChainState,
    EventType,
    InMemoryAuditLog,
    anchor_record,
    chain_hash,
    check_chain,
    compute_kpis,
    verify_chain,
)
from procworks.auth_token import TokenAuthBackend
from procworks.db import AuditEventRow, SqlAlchemyAuditLog

#: Seconds the barrier waits for the second writer. Once appending is
#: serialised, the second writer can never reach the barrier while the first
#: holds the end of the chain -- the wait then ends after this timeout.
_BARRIER_TIMEOUT = 0.5


def _gate_hashing(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """Hold the first two hash computations of ``module`` until both arrived.

    The hash is computed after the writer has read the end of the chain and
    before it records the new end -- exactly the window in which a second
    writer read the same end before. Later calls pass straight through.
    """

    original: Callable[..., str] = module.chain_hash
    barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)
    calls = [0]
    lock = threading.Lock()

    def gated(*args: Any, **kwargs: Any) -> str:
        with lock:
            calls[0] += 1
            call = calls[0]
        if call <= 2:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "chain_hash", gated)


def _append_in_unit(log: Any, instance_id: str) -> None:
    unit_of_work.run(lambda: log.append(EventType.INSTANCE_CREATED, instance_id, "s1"))


def _assert_linear(events: list[AuditEvent]) -> None:
    """Every event names its immediate predecessor; numbers have no gap."""

    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    previous = ""
    for event in events:
        assert event.prev_hash == previous, f"event {event.seq} branches off"
        previous = event.entry_hash


@pytest.fixture(params=["memory", "sqlite"])
def log_and_module(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[Any, Any]:
    """An empty audit log, in memory or on an SQLite file, with the module
    whose ``chain_hash`` it calls."""

    if request.param == "memory":
        return InMemoryAuditLog(), audit_module
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    return SqlAlchemyAuditLog(url, create_tables=True), db_module


def test_two_writers_appending_at_once_keep_one_linear_chain(
    log_and_module: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    log, module = log_and_module
    log.append(EventType.INSTANCE_CREATED, "i0", "s1")
    _gate_hashing(monkeypatch, module)

    writers = [
        threading.Thread(target=_append_in_unit, args=(log, f"i{n}")) for n in (1, 2)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=30)

    events = log.list_all()
    assert len(events) == 3
    _assert_linear(events)
    assert log.head_hash() == events[-1].entry_hash


def test_many_writers_in_units_number_without_gap_and_never_branch(tmp_path: Path) -> None:
    """Without a barrier: eight threads, five units each, one SQLite file."""

    log = SqlAlchemyAuditLog(f"sqlite:///{tmp_path / 'audit.db'}", create_tables=True)

    def write(worker: int) -> None:
        for n in range(5):
            _append_in_unit(log, f"w{worker}-{n}")

    threads = [threading.Thread(target=write, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    events = log.list_all()
    assert len(events) == 40
    _assert_linear(events)
    assert check_chain(log).status == "intact"


def test_a_restarted_store_continues_from_the_stored_end(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    before = SqlAlchemyAuditLog(url, create_tables=True)
    before.append(EventType.INSTANCE_CREATED, "i1", "s1")
    head = before.append(EventType.INSTANCE_COMPLETED, "i1", "s1")

    after = SqlAlchemyAuditLog(url, create_tables=True)  # a new process
    event = after.append(EventType.INSTANCE_CREATED, "i2", "s1")

    assert event.seq == 3
    assert event.prev_hash == head.entry_hash
    _assert_linear(after.list_all())
    assert after.chain_state() == ChainState(
        head_hash=event.entry_hash, head_seq=3, serialized_since=0
    )


def test_a_rolled_back_append_gives_its_number_back(tmp_path: Path) -> None:
    """A request that fails after recording an event leaves no gap behind."""

    log = SqlAlchemyAuditLog(f"sqlite:///{tmp_path / 'audit.db'}", create_tables=True)
    log.append(EventType.INSTANCE_CREATED, "i1", "s1")

    def failing() -> None:
        log.append(EventType.INSTANCE_CREATED, "lost", "s1")
        raise RuntimeError("the request failed after the event")

    with pytest.raises(RuntimeError, match="after the event"):
        unit_of_work.run(failing)
    event = log.append(EventType.INSTANCE_CREATED, "i2", "s1")

    assert event.seq == 2
    assert [e.instance_id for e in log.list_all()] == ["i1", "i2"]
    _assert_linear(log.list_all())


def test_clear_starts_a_new_chain(log_and_module: tuple[Any, Any]) -> None:
    log, _ = log_and_module
    log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    log.clear()

    event = log.append(EventType.INSTANCE_CREATED, "i2", "s1")

    assert (event.seq, event.prev_hash) == (1, "")
    assert log.chain_state() == ChainState(
        head_hash=event.entry_hash, head_seq=1, serialized_since=0
    )
    assert check_chain(log).status == "intact"


# --- the check: building chains by hand ------------------------------------------

_T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _event(
    seq: int,
    prev_hash: str,
    *,
    event_type: EventType = EventType.INSTANCE_CREATED,
    detail: dict[str, str] | None = None,
) -> AuditEvent:
    """One correctly hashed event, a minute per number after ``_T0``."""

    fields: dict[str, Any] = {
        "seq": seq,
        "timestamp": _T0 + timedelta(minutes=seq),
        "event_type": event_type,
        "instance_id": f"i{seq}",
        "schema_id": "s1",
        "schema_version": 1,
        "node_id": None,
        "label": None,
        "agent_id": None,
        "detail": detail or {},
    }
    return AuditEvent(**fields, prev_hash=prev_hash, entry_hash=chain_hash(prev_hash, **fields))


def _chain(seqs: list[int], links: dict[int, int] | None = None) -> list[AuditEvent]:
    """Events with the numbers ``seqs``, each linked to its predecessor --
    except where ``links`` names another target (number; 0 = chain start):
    a branch as two concurrent writers left it."""

    links = links or {}
    events: list[AuditEvent] = []
    by_seq: dict[int, AuditEvent] = {}
    for seq in seqs:
        target = links.get(seq, events[-1].seq if events else 0)
        prev = by_seq[target].entry_hash if target else ""
        event = _event(seq, prev)
        events.append(event)
        by_seq[seq] = event
    return events


def _anchor(seq: int, after: AuditEvent, branches: str) -> AuditEvent:
    return _event(
        seq,
        after.entry_hash,
        event_type=EventType.CHAIN_ANCHOR,
        detail={"branches": branches, "checked_through": str(after.seq),
                "reason": "geprüft", "actor": "admin"},
    )


def _state(events: list[AuditEvent], serialized_since: int) -> ChainState:
    newest = events[-1]
    return ChainState(
        head_hash=newest.entry_hash, head_seq=newest.seq, serialized_since=serialized_since
    )


def _kinds(report: ChainReport) -> list[tuple[str, int]]:
    return [(d.kind, d.seq) for d in report.defects]


def test_a_linear_chain_is_intact() -> None:
    events = _chain([1, 2, 3, 4])

    report = verify_chain(events, _state(events, 0))

    assert report.status == "intact"
    assert (report.branches, report.defects, report.events) == ([], [], 4)


def test_an_empty_log_is_intact() -> None:
    report = verify_chain([], ChainState(head_hash="", head_seq=0, serialized_since=0))

    assert report.status == "intact"


def test_a_branch_from_before_the_serialisation_is_reported_as_historic_with_its_time() -> None:
    events = _chain([1, 2, 3, 4, 5], links={3: 1})

    report = verify_chain(events, _state(events, serialized_since=5))

    assert report.status == "open"
    assert report.defects == []
    assert report.branches == [
        ChainBranch(seq=3, timestamp=_T0 + timedelta(minutes=3), links_to=1, anchored_by=None)
    ]


def test_a_branch_to_the_chain_start_is_historic_too() -> None:
    """Two first events written at once both named the empty start."""

    events = _chain([1, 2, 3], links={2: 0})

    report = verify_chain(events, _state(events, serialized_since=3))

    assert [(b.seq, b.links_to) for b in report.branches] == [(2, 0)]
    assert report.status == "open"


def test_an_anchor_closes_the_historic_branch_and_it_stays_visible() -> None:
    events = _chain([1, 2, 3, 4, 5], links={3: 1})
    events.append(_anchor(6, events[-1], "3"))
    events.append(_event(7, events[-1].entry_hash))

    report = verify_chain(events, _state(events, serialized_since=5))

    assert report.status == "intact"
    assert report.defects == []
    assert [(b.seq, b.anchored_by) for b in report.branches] == [(3, 6)]
    assert report.anchors == [6]


def test_a_branch_after_the_serialisation_is_a_defect() -> None:
    events = _chain([1, 2, 3, 4], links={4: 2})

    report = verify_chain(events, _state(events, serialized_since=3))

    assert report.status == "defective"
    assert _kinds(report) == [("unserialized-branch", 4)]
    assert report.branches == []


def test_legacy_events_without_hash_are_accepted_only_at_the_start() -> None:
    legacy = [
        AuditEvent(seq=seq, timestamp=_T0, event_type=EventType.INSTANCE_CREATED,
                   instance_id="old", schema_id="s1")
        for seq in (1, 2)
    ]
    chained = [_event(3, ""), ]
    chained.append(_event(4, chained[0].entry_hash))

    report = verify_chain(legacy + chained, _state(chained, serialized_since=4))
    assert (report.status, report.unchained) == ("intact", 2)

    late = AuditEvent(seq=5, timestamp=_T0, event_type=EventType.INSTANCE_CREATED,
                      instance_id="x", schema_id="s1")
    state = ChainState(head_hash="", head_seq=5, serialized_since=5)
    report = verify_chain(legacy + chained + [late], state)
    assert ("unchained-after-chain", 5) in _kinds(report)


# --- tampering is never excused as historic ---------------------------------------
#
# Every case below lies in the region where a branch would be historic
# (``serialized_since`` covers all events) and carries a correctly anchored
# historic branch -- the most lenient setting the check knows. Each must stay
# a defect, with its own reason, and must not appear among the branches.


def _anchored_history() -> list[AuditEvent]:
    """1..7 with a historic branch at 4 (to 2) and an anchor at 8 naming it."""

    events = _chain([1, 2, 3, 4, 5, 6, 7], links={4: 2})
    events.append(_anchor(8, events[-1], "4"))
    return events


def _assert_tampering_found(
    events: list[AuditEvent], state: ChainState, expected: list[tuple[str, int]]
) -> None:
    report = verify_chain(events, state)
    assert report.status == "defective"
    assert _kinds(report) == expected
    flagged = {seq for _, seq in expected}
    assert not flagged & {b.seq for b in report.branches}, "tampering excused as a branch"


def test_the_untouched_anchored_history_is_intact() -> None:
    """Baseline for the cases below: without tampering, all is well."""

    events = _anchored_history()
    report = verify_chain(events, _state(events, serialized_since=7))
    assert report.status == "intact"
    assert [(b.seq, b.anchored_by) for b in report.branches] == [(4, 8)]


def test_a_changed_event_is_reported_as_exactly_that_event() -> None:
    events = _anchored_history()
    events[2] = events[2].model_copy(update={"instance_id": "geändert"})

    _assert_tampering_found(events, _state(events, 7), [("entry-altered", 3)])


def test_a_changed_hash_is_never_a_historic_branch() -> None:
    """A rewritten hash makes the next event name a hash nobody carries."""

    events = _anchored_history()
    events[4] = events[4].model_copy(update={"entry_hash": "0" * 64})  # number 5

    _assert_tampering_found(
        events, _state(events, 7), [("entry-altered", 5), ("link-missing", 6)]
    )


def test_a_rehashed_changed_event_breaks_the_link_after_it() -> None:
    """Changing an event and recomputing its hash still leaves the next link
    naming the old hash."""

    events = _anchored_history()
    changed = events[4].model_copy(update={"instance_id": "geändert"})
    events[4] = changed.model_copy(
        update={"entry_hash": chain_hash(changed.prev_hash, **{
            k: getattr(changed, k) for k in (
                "seq", "timestamp", "event_type", "instance_id", "schema_id",
                "schema_version", "node_id", "label", "agent_id", "detail")
        })}
    )

    _assert_tampering_found(events, _state(events, 7), [("link-missing", 6)])


def test_a_removed_event_is_never_a_historic_branch() -> None:
    events = _anchored_history()
    del events[5]  # number 6

    _assert_tampering_found(events, _state(events, 7), [("link-missing", 7)])


def test_removing_the_event_a_branch_names_is_a_defect() -> None:
    events = _anchored_history()
    del events[1]  # number 2, which the historic branch at 4 links to

    report = verify_chain(events, _state(events, 7))
    assert report.status == "defective"
    assert ("link-missing", 3) in _kinds(report)
    assert ("link-missing", 4) in _kinds(report)
    assert report.branches == []


def test_a_removed_end_is_found_against_the_recorded_end() -> None:
    events = _anchored_history()
    state = _state(events, 7)

    _assert_tampering_found(events[:-1], state, [("end-mismatch", 8)])


def test_an_event_slipped_in_before_the_anchor_is_not_covered_by_it() -> None:
    """A forged event in a gap of the numbering, hashed like a branch: the
    anchor names only the branches it saw, so the forgery and the event it
    displaces stay defects."""

    events = _chain([1, 2, 3, 5, 6, 7], links={})
    events.append(_anchor(8, events[-1], ""))
    forged = _event(4, events[0].entry_hash)  # claims to branch off event 1
    tampered = sorted([*events, forged], key=lambda e: e.seq)

    _assert_tampering_found(
        tampered,
        _state(events, 7),
        [("branch-not-anchored", 4), ("branch-not-anchored", 5)],
    )


def test_a_widened_anchor_does_not_count() -> None:
    """Adding a forged branch to the anchor's list breaks the anchor's hash."""

    events = _anchored_history()
    events[7] = events[7].model_copy(
        update={"detail": {**events[7].detail, "branches": "4,6"}}
    )

    report = verify_chain(events, _state(events, 7))
    assert ("entry-altered", 8) in _kinds(report)
    assert report.anchors == []
    assert all(b.anchored_by is None for b in report.branches)
    assert report.status == "defective"


def test_a_changed_time_is_found_although_other_time_forms_are_accepted() -> None:
    """The check accepts the forms a database hands one instant back in (no
    zone, another zone) -- but never another instant."""

    events = _anchored_history()
    original = events[2].timestamp
    events[2] = events[2].model_copy(
        update={"timestamp": original.astimezone(timezone(timedelta(hours=2)))}
    )
    assert verify_chain(events, _state(events, 7)).status == "intact"
    events[2] = events[2].model_copy(update={"timestamp": original.replace(tzinfo=None)})
    assert verify_chain(events, _state(events, 7)).status == "intact"

    events[2] = events[2].model_copy(update={"timestamp": original + timedelta(seconds=1)})
    _assert_tampering_found(events, _state(events, 7), [("entry-altered", 3)])


# --- stored data from before the serialisation ------------------------------------


def _plant(url: str, events: list[AuditEvent]) -> None:
    """Write ``events`` into ``audit_event`` as they are -- the way an older
    version left them, past every store method."""

    engine = create_engine(url)
    with engine.begin() as conn:
        for event in events:
            conn.execute(
                insert(AuditEventRow.__table__).values(
                    seq=event.seq,
                    timestamp=event.timestamp,
                    event_type=event.event_type.value,
                    instance_id=event.instance_id,
                    schema_id=event.schema_id,
                    schema_version=event.schema_version,
                    node_id=event.node_id,
                    label=event.label,
                    agent_id=event.agent_id,
                    detail=event.detail,
                    prev_hash=event.prev_hash,
                    entry_hash=event.entry_hash,
                )
            )
    engine.dispose()


def test_events_stored_before_the_chain_end_existed_count_as_historic(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    log = SqlAlchemyAuditLog(url, create_tables=True)
    _plant(url, _chain([1, 2, 3, 4], links={3: 1}))

    assert log.chain_state().serialized_since == 4
    event = log.append(EventType.INSTANCE_CREATED, "new", "s1")

    assert event.seq == 5
    report = check_chain(log)
    assert report.status == "open"
    assert [(b.seq, b.links_to) for b in report.branches] == [(3, 1)]
    assert report.defects == []


# --- the admin endpoints ------------------------------------------------------------

client = TestClient(app)


@pytest.fixture
def sql_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """The API on an SQLite file (see ``sql_backed_api``); returns its URL."""

    return use_sql_backend(monkeypatch, tmp_path)


def _anchor_events() -> list[AuditEvent]:
    return [e for e in api_module._audit.list_all() if e.event_type is EventType.CHAIN_ANCHOR]


def test_the_check_of_a_running_system_is_intact(sql_api: str) -> None:
    sid = client.post("/schemas", json={"name": "Kette"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})

    resp = client.get("/admin/audit-chain")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "intact"


def test_historic_branches_are_listed_and_an_anchor_closes_them(sql_api: str) -> None:
    _plant(sql_api, _chain([1, 2, 3, 4, 5], links={2: 0, 4: 2}))

    report = client.get("/admin/audit-chain").json()
    assert report["status"] == "open"
    assert [(b["seq"], b["links_to"], b["anchored_by"]) for b in report["branches"]] == [
        (2, 0, None),
        (4, 2, None),
    ]
    assert report["branches"][0]["timestamp"].startswith("2026-09-01T08:02")

    resp = client.post("/admin/audit-chain/anchor", json={"reason": "Mehrprozessbetrieb geprüft"})

    assert resp.status_code == 200, resp.text
    after = resp.json()
    assert after["status"] == "intact"
    assert [(b["seq"], b["anchored_by"]) for b in after["branches"]] == [(2, 6), (4, 6)]
    (anchor,) = _anchor_events()
    assert anchor.seq == 6
    assert anchor.detail["branches"] == "2,4"
    assert anchor.detail["checked_through"] == "5"
    assert anchor.detail["reason"] == "Mehrprozessbetrieb geprüft"
    assert anchor.detail["actor"]
    # still visible later, and the anchor is no process event
    assert client.get("/admin/audit-chain").json()["status"] == "intact"
    assert EventType.CHAIN_ANCHOR in NON_PROCESS_EVENTS
    assert compute_kpis(api_module._audit.list_all()).total_instances == 5


@pytest.mark.parametrize("reason", ["", "   ", "​​"])
def test_an_anchor_needs_a_visible_reason(sql_api: str, reason: str) -> None:
    _plant(sql_api, _chain([1, 2, 3], links={3: 1}))

    resp = client.post("/admin/audit-chain/anchor", json={"reason": reason})

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "REQ.reason-required"
    assert resp.json()["detail"]["params"] == {"what": "chain-anchor"}
    assert _anchor_events() == []


def test_an_anchor_is_refused_over_a_defect(sql_api: str) -> None:
    """The anchor acknowledges concurrent writing -- never a changed event."""

    events = _chain([1, 2, 3, 4], links={3: 1})
    events[1] = events[1].model_copy(update={"label": "nachträglich geändert"})
    _plant(sql_api, events)

    report = client.get("/admin/audit-chain").json()
    assert report["status"] == "defective"
    assert [(d["kind"], d["seq"]) for d in report["defects"]] == [("entry-altered", 2)]

    resp = client.post("/admin/audit-chain/anchor", json={"reason": "passt schon"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "AUD.chain-defective"
    assert resp.json()["detail"]["params"] == {"defects": "1"}
    assert _anchor_events() == []


def test_an_anchor_without_open_branches_is_refused(sql_api: str) -> None:
    _plant(sql_api, _chain([1, 2, 3], links={3: 1}))
    assert client.post("/admin/audit-chain/anchor", json={"reason": "geprüft"}).status_code == 200

    resp = client.post("/admin/audit-chain/anchor", json={"reason": "noch einmal"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "AUD.chain-nothing-open"
    assert len(_anchor_events()) == 1


def test_only_an_admin_reads_the_check_or_sets_an_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_module,
        "_auth_backend",
        TokenAuthBackend(
            {
                "view": {"subject": "leser", "roles": ["viewer"]},
                "model": {"subject": "modell", "roles": ["modeler"]},
            }
        ),
    )
    for token in ("view", "model"):
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/admin/audit-chain", headers=headers).status_code == 403
        resp = client.post(
            "/admin/audit-chain/anchor", json={"reason": "x"}, headers=headers
        )
        assert resp.status_code == 403


# --- what an anchor names stays as it was --------------------------------------------
#
# An event at the end of a historic branch has no successor that names its
# hash. Removing it, or changing it together with its own hash, breaks no
# link: without a record the check stayed ``intact`` -- also after an anchor
# that named the branch which then silently disappeared.


def _recording_anchor(seq: int, events: list[AuditEvent], serialized_since: int) -> AuditEvent:
    """The anchor the endpoint writes: what the check finds open, with hashes."""

    report = verify_chain(events, _state(events, serialized_since))
    detail = {**anchor_record(events, report), "checked_through": str(events[-1].seq),
              "reason": "geprüft", "actor": "admin"}
    return _event(seq, events[-1].entry_hash, event_type=EventType.CHAIN_ANCHOR, detail=detail)


def _recorded_history() -> list[AuditEvent]:
    """1..7 with a historic branch at 4 (to 2), so 3 is a dead end; anchor at 8."""

    events = _chain([1, 2, 3, 4, 5, 6, 7], links={4: 2})
    events.append(_recording_anchor(8, events, serialized_since=7))
    return events


def _rehashed(event: AuditEvent, **changes: Any) -> AuditEvent:
    """``event`` with ``changes`` and a hash recomputed to match them."""

    changed = event.model_copy(update=changes)
    fields = {k: getattr(changed, k) for k in (
        "seq", "timestamp", "event_type", "instance_id", "schema_id",
        "schema_version", "node_id", "label", "agent_id", "detail")}
    return changed.model_copy(update={"entry_hash": chain_hash(changed.prev_hash, **fields)})


def test_the_anchor_records_open_branches_and_dead_ends_with_their_hashes() -> None:
    events = _chain([1, 2, 3, 4, 5, 6, 7], links={4: 2})
    by_seq = {e.seq: e for e in events}

    record = anchor_record(events, verify_chain(events, _state(events, 7)))

    assert record == {
        "branches": "4",
        "branch_hashes": f"4:{by_seq[4].entry_hash}",
        "dead_ends": f"3:{by_seq[3].entry_hash}",
    }


def test_the_untouched_recorded_history_is_intact_and_lists_its_dead_ends() -> None:
    events = _recorded_history()

    report = verify_chain(events, _state(events, serialized_since=7))

    assert report.status == "intact"
    assert report.defects == []
    assert [(b.seq, b.anchored_by) for b in report.branches] == [(4, 8)]
    assert [(d.seq, d.entry_hash, d.anchored_by) for d in report.dead_ends] == [
        (3, events[2].entry_hash, 8)
    ]


def test_removing_a_dead_end_is_found_also_where_the_branch_after_it_vanishes() -> None:
    """The reported case: without 3, the branch at 4 links to its immediate
    predecessor and looks linear -- the anchor named both."""

    events = _recorded_history()
    del events[2]  # number 3, the dead end

    _assert_tampering_found(
        events,
        _state(events, 7),
        [("anchored-branch-missing", 3), ("anchored-branch-missing", 4)],
    )


def test_a_named_branch_that_vanished_is_found_also_with_an_older_anchor() -> None:
    """Anchors written before dead ends were recorded name only the branches;
    a named branch that is no longer one is still a defect."""

    events = _anchored_history()  # anchor without a record of dead ends
    del events[2]  # number 3

    _assert_tampering_found(events, _state(events, 7), [("anchored-branch-missing", 4)])


def test_removing_a_dead_end_is_found_where_the_branch_after_it_remains() -> None:
    """3 links to 2, 4 branches off 1: without 3, 4 is still a branch to 1 --
    only the recorded dead end shows the removal (and 2, now nobody's
    predecessor, is an end the anchor never saw)."""

    events = _chain([1, 2, 3, 4, 5, 6], links={4: 1})
    events.append(_recording_anchor(7, events, serialized_since=6))
    del events[2]  # number 3

    report = verify_chain(events, _state(events, 6))
    assert [(b.seq, b.links_to) for b in report.branches] == [(4, 1)]
    _assert_tampering_found(
        events,
        _state(events, 6),
        [("branch-not-anchored", 2), ("anchored-branch-missing", 3)],
    )


def test_a_dead_end_changed_together_with_its_hash_is_found() -> None:
    """Nobody links to a dead end, so a recomputed hash breaks no link."""

    events = _recorded_history()
    events[2] = _rehashed(events[2], instance_id="geändert")  # number 3
    assert verify_chain(events[:-1], _state(events[:-1], 7)).defects == []

    _assert_tampering_found(events, _state(events, 7), [("anchored-branch-altered", 3)])


def test_a_dead_end_slipped_in_before_the_anchor_is_not_covered_by_it() -> None:
    """A forged event in a gap of the numbering after a dead end: it becomes
    the new dead end, the branch after it stays a branch -- the anchor does
    not name it."""

    events = _chain([1, 2, 3, 5, 6, 7], links={5: 2})
    events.append(_recording_anchor(8, events, serialized_since=7))
    forged = _event(4, events[2].entry_hash)  # continues the dead end 3
    tampered = sorted([*events, forged], key=lambda e: e.seq)

    report = verify_chain(tampered, _state(events, 7))
    assert report.status == "defective"
    assert _kinds(report) == [("branch-not-anchored", 4)]


def test_a_shortened_dead_end_record_breaks_the_anchor() -> None:
    events = _recorded_history()
    events[7] = events[7].model_copy(update={"detail": {**events[7].detail, "dead_ends": ""}})

    report = verify_chain(events, _state(events, 7))
    assert ("entry-altered", 8) in _kinds(report)
    assert report.anchors == []
    assert report.status == "defective"


def test_the_anchor_endpoint_records_dead_ends_and_the_check_holds_it_to_them(
    sql_api: str,
) -> None:
    planted = _chain([1, 2, 3, 4, 5], links={2: 0, 4: 2})
    _plant(sql_api, planted)
    by_seq = {e.seq: e for e in planted}

    assert client.post("/admin/audit-chain/anchor", json={"reason": "geprüft"}).status_code == 200
    (anchor,) = _anchor_events()
    assert anchor.detail["dead_ends"] == (
        f"1:{by_seq[1].entry_hash},3:{by_seq[3].entry_hash}"
    )
    assert anchor.detail["branch_hashes"] == (
        f"2:{by_seq[2].entry_hash},4:{by_seq[4].entry_hash}"
    )

    engine = create_engine(sql_api)
    with engine.begin() as conn:
        conn.execute(delete(AuditEventRow.__table__).where(AuditEventRow.__table__.c.seq == 3))
    engine.dispose()

    report = client.get("/admin/audit-chain").json()
    assert report["status"] == "defective"
    assert [(d["kind"], d["seq"]) for d in report["defects"]] == [
        ("anchored-branch-missing", 3),
        ("anchored-branch-missing", 4),
        ("historic-range-changed", 5),  # the record of the history, too
    ]
    refused = client.post("/admin/audit-chain/anchor", json={"reason": "noch einmal"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "AUD.chain-defective"


def test_an_installation_anchored_without_dead_ends_can_record_them(sql_api: str) -> None:
    """An anchor written before dead ends were recorded closes the branches;
    the dead ends stay open (listed, status unchanged) until a new anchor
    records just those."""

    planted = _chain([1, 2, 3, 4, 5, 6, 7], links={4: 2})
    planted.append(_anchor(8, planted[-1], "4"))  # anchor without a record
    _plant(sql_api, planted)

    before = client.get("/admin/audit-chain").json()
    assert before["status"] == "intact"
    assert [(d["seq"], d["anchored_by"]) for d in before["dead_ends"]] == [(3, None)]

    resp = client.post("/admin/audit-chain/anchor", json={"reason": "Sackgassen nachverankert"})

    assert resp.status_code == 200, resp.text
    anchor = _anchor_events()[-1]
    assert (anchor.seq, anchor.detail["branches"]) == (9, "")
    assert anchor.detail["dead_ends"] == f"3:{planted[2].entry_hash}"
    assert [(d["seq"], d["anchored_by"]) for d in resp.json()["dead_ends"]] == [(3, 9)]
    again = client.post("/admin/audit-chain/anchor", json={"reason": "noch einmal"})
    assert again.json()["detail"]["code"] == "AUD.chain-nothing-open"
