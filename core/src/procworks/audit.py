# SPDX-License-Identifier: BUSL-1.1
"""Audit/Event Log and monitoring aggregation (roadmap step 15).

The execution core stays pure: runtime events are recorded at the API boundary
into an append-only :class:`AuditLog`. The recorded history is the single basis
for monitoring KPIs, the per-instance audit timeline and a lightweight
process-mining map (directly-follows graph), mirroring Section 5.3/8.4 of the
architecture concept.

This module holds no correctness logic; it only observes what already happened.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """The kinds of runtime events recorded in the audit log."""

    INSTANCE_CREATED = "INSTANCE_CREATED"
    ACTIVITY_STARTED = "ACTIVITY_STARTED"
    ACTIVITY_COMPLETED = "ACTIVITY_COMPLETED"
    BRANCH_DECIDED = "BRANCH_DECIDED"
    ADHOC_INSERTED = "ADHOC_INSERTED"
    ADHOC_DELETED = "ADHOC_DELETED"
    ADHOC_RENAMED = "ADHOC_RENAMED"
    INSTANCE_MIGRATED = "INSTANCE_MIGRATED"
    INSTANCE_COMPLETED = "INSTANCE_COMPLETED"
    MAIL_SENT = "MAIL_SENT"          # modelled notification delivered (metadata only)
    MAIL_FAILED = "MAIL_FAILED"      # notification dead-lettered after retries (metadata)
    #: A monotone time-ratchet checkpoint of the licensing layer, embedded into
    #: the hash chain so the effective-time high-water-mark cannot be silently
    #: rolled back (licensing concept §5A.4). It is *not* a process event and is
    #: excluded from KPI/mining aggregation via :data:`NON_PROCESS_EVENTS`.
    TIME_ANCHOR = "TIME_ANCHOR"


#: Event types that are recorded for tamper evidence but do not describe process
#: progress; aggregation (KPIs, process map, per-instance grouping) skips them.
NON_PROCESS_EVENTS: frozenset[EventType] = frozenset({EventType.TIME_ANCHOR})


class AuditEvent(BaseModel):
    """A single, immutable entry of the event history.

    ``prev_hash``/``entry_hash`` form an append-only hash chain over the whole
    log: ``entry_hash = H(prev_hash ‖ canonical(event))``. Rewinding or editing
    any past entry requires re-writing every subsequent hash, turning tampering
    (e.g. to roll back the licensing time ratchet) into a visible, consistent
    rewrite rather than a single silent field change. The fields default to the
    empty string so pre-existing callers/records stay valid (additive).
    """

    seq: int
    timestamp: datetime
    event_type: EventType
    instance_id: str
    schema_id: str
    schema_version: int = 1
    node_id: str | None = None
    label: str | None = None
    agent_id: str | None = None
    detail: dict[str, str] = Field(default_factory=dict)
    prev_hash: str = ""
    entry_hash: str = ""


def chain_hash(
    prev_hash: str,
    *,
    seq: int,
    timestamp: datetime,
    event_type: EventType,
    instance_id: str,
    schema_id: str,
    schema_version: int,
    node_id: str | None,
    label: str | None,
    agent_id: str | None,
    detail: dict[str, str],
) -> str:
    """Return the chain hash of one entry given the previous entry's hash.

    Both audit-log backends call this so the chain is computed identically. The
    canonical form pins the semantic fields (never the hashes themselves) in a
    fixed order with sorted ``detail`` keys.
    """

    core = "\x1f".join(
        [
            prev_hash,
            str(seq),
            timestamp.isoformat(),
            event_type.value,
            instance_id,
            schema_id,
            str(schema_version),
            node_id or "",
            label or "",
            agent_id or "",
            "\x1e".join(f"{k}={detail[k]}" for k in sorted(detail)),
        ]
    )
    return hashlib.sha256(core.encode()).hexdigest()


class AuditLog(Protocol):
    """Minimal append-only interface for the event history."""

    def append(
        self,
        event_type: EventType,
        instance_id: str,
        schema_id: str,
        *,
        schema_version: int = 1,
        node_id: str | None = None,
        label: str | None = None,
        agent_id: str | None = None,
        detail: dict[str, str] | None = None,
    ) -> AuditEvent: ...

    def list_all(self) -> list[AuditEvent]: ...

    def for_instance(self, instance_id: str) -> list[AuditEvent]: ...

    def revision(self) -> int: ...

    def head_hash(self) -> str: ...

    def max_event_time(self) -> float: ...

    def clear(self) -> None: ...


class InMemoryAuditLog:
    """A trivial list-backed event log with a monotonic sequence counter."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._seq = 0
        self._head = ""

    def append(
        self,
        event_type: EventType,
        instance_id: str,
        schema_id: str,
        *,
        schema_version: int = 1,
        node_id: str | None = None,
        label: str | None = None,
        agent_id: str | None = None,
        detail: dict[str, str] | None = None,
    ) -> AuditEvent:
        self._seq += 1
        timestamp = datetime.now(UTC)
        detail = detail or {}
        entry_hash = chain_hash(
            self._head,
            seq=self._seq,
            timestamp=timestamp,
            event_type=event_type,
            instance_id=instance_id,
            schema_id=schema_id,
            schema_version=schema_version,
            node_id=node_id,
            label=label,
            agent_id=agent_id,
            detail=detail,
        )
        event = AuditEvent(
            seq=self._seq,
            timestamp=timestamp,
            event_type=event_type,
            instance_id=instance_id,
            schema_id=schema_id,
            schema_version=schema_version,
            node_id=node_id,
            label=label,
            agent_id=agent_id,
            detail=detail,
            prev_hash=self._head,
            entry_hash=entry_hash,
        )
        self._head = entry_hash
        self._events.append(event)
        return event

    def list_all(self) -> list[AuditEvent]:
        return list(self._events)

    def for_instance(self, instance_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.instance_id == instance_id]

    def revision(self) -> int:
        """Return a monotonic revision counter of the recorded history.

        The counter equals the highest sequence number appended so far (0 for an
        empty log). Clients poll it cheaply to detect that runtime progress has
        happened and refresh their live views without fetching the full history.
        """

        return self._seq

    def head_hash(self) -> str:
        """Return the newest entry's chain hash ("" when the log is empty)."""

        return self._head

    def max_event_time(self) -> float:
        """Return the newest recorded timestamp as epoch seconds (0.0 if empty).

        A monotone lower bound on real time for the licensing ratchet: an
        append-only log never moves this backwards.
        """

        if not self._events:
            return 0.0
        return max(e.timestamp.timestamp() for e in self._events)

    def clear(self) -> None:
        self._events.clear()
        self._seq = 0
        self._head = ""


def create_audit_log() -> AuditLog:
    """Build the audit log from the environment.

    If ``DATABASE_URL`` is set, use the SQLAlchemy-backed log (durable,
    append-only event history; tables are created on first use for convenience,
    production should rely on Alembic). Otherwise fall back to in-memory.
    """

    url = os.environ.get("DATABASE_URL")
    if url:
        # Imported lazily so the in-memory path has no SQLAlchemy import cost.
        from procworks.db import SqlAlchemyAuditLog

        return SqlAlchemyAuditLog(url, create_tables=True)
    return InMemoryAuditLog()


# --- aggregation / reporting --------------------------------------------


class ActivityStat(BaseModel):
    """Per-activity throughput figures used to spot bottlenecks."""

    node_id: str
    label: str | None = None
    completed: int
    avg_duration_seconds: float | None = None


class KpiReport(BaseModel):
    """Aggregated key figures over the event history.

    The figures cover the measurable corners of the Devil's Quadrangle
    (Section 8.4.1): *time* via the cycle/activity durations and *flexibility*
    via the share of instances that used an ad-hoc change. Cost and quality are
    deliberately left out -- the engine collects no cost or rework data, so
    reporting them would be dishonest (an explicit, documented gap).
    """

    schema_id: str | None = None
    total_instances: int
    running: int
    completed: int
    avg_cycle_seconds: float | None = None
    activity_stats: list[ActivityStat]
    #: Number of instances that applied at least one ad-hoc change (E4/E6.5).
    adhoc_instances: int = 0
    #: Share of instances with an ad-hoc change (0..1), the flexibility proxy.
    flexibility_adhoc_ratio: float | None = None


class ProcessMapNode(BaseModel):
    """A discovered activity node with its observed frequency."""

    node_id: str
    label: str | None = None
    frequency: int


class ProcessMapEdge(BaseModel):
    """A discovered directly-follows relation with its observed frequency."""

    source: str
    target: str
    frequency: int


class ProcessMap(BaseModel):
    """A lightweight discovered process map (directly-follows graph)."""

    schema_id: str | None = None
    nodes: list[ProcessMapNode]
    edges: list[ProcessMapEdge]


def instance_timeline(
    events: Iterable[AuditEvent], instance_id: str
) -> list[AuditEvent]:
    """Return the chronological event history of a single instance."""

    selected = [e for e in events if e.instance_id == instance_id]
    return sorted(selected, key=lambda e: e.seq)


def _by_instance(
    events: Iterable[AuditEvent], schema_id: str | None
) -> dict[str, list[AuditEvent]]:
    grouped: dict[str, list[AuditEvent]] = {}
    for event in events:
        if event.event_type in NON_PROCESS_EVENTS:
            continue  # tamper-evidence checkpoints are not process progress
        if schema_id is not None and event.schema_id != schema_id:
            continue
        grouped.setdefault(event.instance_id, []).append(event)
    for entries in grouped.values():
        entries.sort(key=lambda e: e.seq)
    return grouped


def compute_kpis(
    events: Iterable[AuditEvent], schema_id: str | None = None
) -> KpiReport:
    """Derive instance counts, average cycle time and per-activity figures."""

    grouped = _by_instance(events, schema_id)

    running = 0
    completed = 0
    cycle_times: list[float] = []
    completions: dict[str, int] = {}
    labels: dict[str, str | None] = {}
    durations: dict[str, list[float]] = {}
    adhoc_instances = 0

    for entries in grouped.values():
        created = next(
            (e for e in entries if e.event_type is EventType.INSTANCE_CREATED), None
        )
        done = next(
            (e for e in entries if e.event_type is EventType.INSTANCE_COMPLETED), None
        )
        if done is not None:
            completed += 1
            if created is not None:
                cycle_times.append((done.timestamp - created.timestamp).total_seconds())
        else:
            running += 1

        if any(
            e.event_type
            in (
                EventType.ADHOC_INSERTED,
                EventType.ADHOC_DELETED,
                EventType.ADHOC_RENAMED,
            )
            for e in entries
        ):
            adhoc_instances += 1

        starts: dict[str, datetime] = {}
        for event in entries:
            if event.node_id is None:
                continue
            if event.event_type is EventType.ACTIVITY_STARTED:
                starts.setdefault(event.node_id, event.timestamp)
            elif event.event_type is EventType.ACTIVITY_COMPLETED:
                completions[event.node_id] = completions.get(event.node_id, 0) + 1
                labels[event.node_id] = event.label
                start = starts.pop(event.node_id, None)
                if start is not None:
                    durations.setdefault(event.node_id, []).append(
                        (event.timestamp - start).total_seconds()
                    )

    activity_stats = [
        ActivityStat(
            node_id=node_id,
            label=labels.get(node_id),
            completed=count,
            avg_duration_seconds=(
                sum(durations[node_id]) / len(durations[node_id])
                if durations.get(node_id)
                else None
            ),
        )
        for node_id, count in sorted(
            completions.items(), key=lambda kv: kv[1], reverse=True
        )
    ]

    return KpiReport(
        schema_id=schema_id,
        total_instances=len(grouped),
        running=running,
        completed=completed,
        avg_cycle_seconds=(
            sum(cycle_times) / len(cycle_times) if cycle_times else None
        ),
        activity_stats=activity_stats,
        adhoc_instances=adhoc_instances,
        flexibility_adhoc_ratio=(
            adhoc_instances / len(grouped) if grouped else None
        ),
    )


def discover_process_map(
    events: Iterable[AuditEvent], schema_id: str | None = None
) -> ProcessMap:
    """Mine a directly-follows graph from completed activities (process mining)."""

    grouped = _by_instance(events, schema_id)

    node_freq: dict[str, int] = {}
    labels: dict[str, str | None] = {}
    edge_freq: dict[tuple[str, str], int] = {}

    for entries in grouped.values():
        sequence = [
            e for e in entries if e.event_type is EventType.ACTIVITY_COMPLETED
        ]
        previous: str | None = None
        for event in sequence:
            if event.node_id is None:
                continue
            node_freq[event.node_id] = node_freq.get(event.node_id, 0) + 1
            labels[event.node_id] = event.label
            if previous is not None:
                key = (previous, event.node_id)
                edge_freq[key] = edge_freq.get(key, 0) + 1
            previous = event.node_id

    nodes = [
        ProcessMapNode(node_id=node_id, label=labels.get(node_id), frequency=freq)
        for node_id, freq in sorted(
            node_freq.items(), key=lambda kv: kv[1], reverse=True
        )
    ]
    edges = [
        ProcessMapEdge(source=src, target=dst, frequency=freq)
        for (src, dst), freq in sorted(
            edge_freq.items(), key=lambda kv: kv[1], reverse=True
        )
    ]
    return ProcessMap(schema_id=schema_id, nodes=nodes, edges=edges)
