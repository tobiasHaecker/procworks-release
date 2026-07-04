# SPDX-License-Identifier: BUSL-1.1
"""Unit tests for the audit log and monitoring aggregation (roadmap step 15)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from procworks.audit import (
    AuditEvent,
    EventType,
    InMemoryAuditLog,
    compute_kpis,
    create_audit_log,
    discover_process_map,
    instance_timeline,
)


def test_in_memory_log_appends_with_monotonic_seq() -> None:
    log = InMemoryAuditLog()
    first = log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    second = log.append(EventType.ACTIVITY_COMPLETED, "i1", "s1", node_id="a")
    assert first.seq == 1
    assert second.seq == 2
    assert first.timestamp <= second.timestamp
    assert len(log.list_all()) == 2


def test_revision_tracks_append_and_clear() -> None:
    # The revision counter powers the web client's auto-refresh poll: it starts
    # at zero, advances with every appended event and resets when the log clears.
    log = InMemoryAuditLog()
    assert log.revision() == 0
    log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    assert log.revision() == 1
    log.append(EventType.ACTIVITY_COMPLETED, "i1", "s1", node_id="a")
    assert log.revision() == 2
    log.clear()
    assert log.revision() == 0


def test_for_instance_filters_by_instance() -> None:
    log = InMemoryAuditLog()
    log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    log.append(EventType.INSTANCE_CREATED, "i2", "s1")
    log.append(EventType.ACTIVITY_COMPLETED, "i1", "s1", node_id="a")
    assert {e.instance_id for e in log.for_instance("i1")} == {"i1"}
    assert len(log.for_instance("i1")) == 2


def test_create_audit_log_returns_usable_log() -> None:
    log = create_audit_log()
    event = log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    assert event.event_type is EventType.INSTANCE_CREATED


def _ev(
    seq: int,
    event_type: EventType,
    instance_id: str,
    *,
    schema_id: str = "s1",
    node_id: str | None = None,
    label: str | None = None,
    seconds: int = 0,
) -> AuditEvent:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    return AuditEvent(
        seq=seq,
        timestamp=base + timedelta(seconds=seconds),
        event_type=event_type,
        instance_id=instance_id,
        schema_id=schema_id,
        node_id=node_id,
        label=label,
    )


def test_instance_timeline_is_sorted_and_scoped() -> None:
    events = [
        _ev(3, EventType.INSTANCE_COMPLETED, "i1"),
        _ev(1, EventType.INSTANCE_CREATED, "i1"),
        _ev(2, EventType.ACTIVITY_COMPLETED, "i2", node_id="a"),
    ]
    timeline = instance_timeline(events, "i1")
    assert [e.seq for e in timeline] == [1, 3]


def test_compute_kpis_counts_and_cycle_time() -> None:
    events = [
        _ev(1, EventType.INSTANCE_CREATED, "i1", seconds=0),
        _ev(2, EventType.ACTIVITY_STARTED, "i1", node_id="a", seconds=10),
        _ev(3, EventType.ACTIVITY_COMPLETED, "i1", node_id="a", label="A", seconds=40),
        _ev(4, EventType.INSTANCE_COMPLETED, "i1", seconds=100),
        _ev(5, EventType.INSTANCE_CREATED, "i2", seconds=0),
    ]
    report = compute_kpis(events)
    assert report.total_instances == 2
    assert report.running == 1
    assert report.completed == 1
    assert report.avg_cycle_seconds == 100.0
    assert len(report.activity_stats) == 1
    stat = report.activity_stats[0]
    assert stat.node_id == "a"
    assert stat.label == "A"
    assert stat.completed == 1
    assert stat.avg_duration_seconds == 30.0


def test_compute_kpis_filters_by_schema() -> None:
    events = [
        _ev(1, EventType.INSTANCE_CREATED, "i1", schema_id="s1"),
        _ev(2, EventType.INSTANCE_CREATED, "i2", schema_id="s2"),
    ]
    report = compute_kpis(events, "s1")
    assert report.total_instances == 1
    assert report.schema_id == "s1"


def test_discover_process_map_directly_follows() -> None:
    events = [
        _ev(1, EventType.ACTIVITY_COMPLETED, "i1", node_id="a", label="A"),
        _ev(2, EventType.ACTIVITY_COMPLETED, "i1", node_id="b", label="B"),
        _ev(3, EventType.ACTIVITY_COMPLETED, "i2", node_id="a", label="A"),
        _ev(4, EventType.ACTIVITY_COMPLETED, "i2", node_id="b", label="B"),
    ]
    pmap = discover_process_map(events)
    freq = {n.node_id: n.frequency for n in pmap.nodes}
    assert freq == {"a": 2, "b": 2}
    assert len(pmap.edges) == 1
    edge = pmap.edges[0]
    assert (edge.source, edge.target, edge.frequency) == ("a", "b", 2)
