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

from procworks.model import NodeType, ProcessSchema, block_join, loop_block


class EventType(StrEnum):
    """The kinds of runtime events recorded in the audit log."""

    INSTANCE_CREATED = "INSTANCE_CREATED"
    ACTIVITY_STARTED = "ACTIVITY_STARTED"
    ACTIVITY_COMPLETED = "ACTIVITY_COMPLETED"
    #: Work-item ownership events (E1, worklist state machine). Deliberately
    #: NOT in ``NON_PROCESS_EVENTS``: they are real per-instance history, and
    #: the KPI/mining aggregations filter on concrete event types anyway, so
    #: these never distort a figure.
    ACTIVITY_CLAIMED = "ACTIVITY_CLAIMED"
    ACTIVITY_RETURNED = "ACTIVITY_RETURNED"
    #: Supervision completion: a login without an agent binding completed a
    #: staff-rule step (mandatory reason in ``detail``). Type-filtered like the
    #: claim events -- no KPI/mining impact; the regular ``ACTIVITY_COMPLETED``
    #: event is written alongside, so figures stay unchanged.
    ACTIVITY_SUPERVISED = "ACTIVITY_SUPERVISED"
    #: An escalation stage fired for an overdue task (T3/E9). Real
    #: per-instance history; type-filtered like the claim events, so it never
    #: distorts a KPI or the mined process map.
    TASK_ESCALATED = "TASK_ESCALATED"
    #: Detail-state transitions of one activity (E2, §4): pause/continue,
    #: failure report (reason in ``detail``) and the recovery reset. Type-
    #: filtered like the claim events -- no KPI/mining impact.
    ACTIVITY_SUSPENDED = "ACTIVITY_SUSPENDED"
    ACTIVITY_RESUMED = "ACTIVITY_RESUMED"
    ACTIVITY_FAILED = "ACTIVITY_FAILED"
    ACTIVITY_RESET = "ACTIVITY_RESET"
    BRANCH_DECIDED = "BRANCH_DECIDED"
    ADHOC_INSERTED = "ADHOC_INSERTED"
    ADHOC_DELETED = "ADHOC_DELETED"
    ADHOC_RENAMED = "ADHOC_RENAMED"
    INSTANCE_MIGRATED = "INSTANCE_MIGRATED"
    #: A process variable was set directly on an instance (``PUT …/data``),
    #: outside an activity completion. One event per changed element; ``detail``
    #: carries ``element``, ``old``/``new`` (JSON) and, where known, ``actor``
    #: and a supervision ``reason``. Type-filtered like the claim events -- no
    #: KPI/mining impact, so the audit need not stay silent to keep the figures
    #: clean (Validierung 2026-09, VAL-01).
    INSTANCE_DATA_SET = "INSTANCE_DATA_SET"
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
        at: datetime | None = None,
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
        at: datetime | None = None,
    ) -> AuditEvent:
        self._seq += 1
        timestamp = at or datetime.now(UTC)
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
    """Per-activity throughput figures used to spot bottlenecks.

    Three durations, each averaged over the completions that allow it:

    * ``avg_duration_seconds`` -- **processing** (started/claimed -> completed).
      Only for completions preceded by ``ACTIVITY_STARTED``; unchanged meaning.
    * ``avg_wait_seconds`` -- **waiting** (ready -> started), same completions.
    * ``avg_total_seconds`` -- **lead time of the step** (ready -> completed),
      for every completion that carries ``detail["ready_at"]``. This is the
      figure that exists even when a task is completed without being claimed
      (allowed, E1) -- previously the bottleneck view then showed nothing.

    ``ready_at`` is the activation stamp the API boundary already keeps
    (``ProcessInstance.node_activated_at``) and hands to the completion event;
    there is deliberately still no separate activation event in the log.
    """

    node_id: str
    label: str | None = None
    completed: int
    avg_duration_seconds: float | None = None
    avg_wait_seconds: float | None = None
    avg_total_seconds: float | None = None


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


def _mean(values: list[float] | None) -> float | None:
    """Arithmetic mean, ``None`` for no values."""

    return sum(values) / len(values) if values else None


def _ready_at(event: AuditEvent) -> datetime | None:
    """The activation stamp a completion event carries (``detail["ready_at"]``).

    Absent on events written before the field existed and on steps without a
    worklist clock (automatic steps); an unparsable value is ignored rather
    than failing the whole report.
    """

    raw = event.detail.get("ready_at")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
    waits: dict[str, list[float]] = {}
    totals: dict[str, list[float]] = {}
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
                ready = _ready_at(event)
                if ready is not None:
                    totals.setdefault(event.node_id, []).append(
                        max(0.0, (event.timestamp - ready).total_seconds())
                    )
                    if start is not None:
                        waits.setdefault(event.node_id, []).append(
                            max(0.0, (start - ready).total_seconds())
                        )

    activity_stats = [
        ActivityStat(
            node_id=node_id,
            label=labels.get(node_id),
            completed=count,
            avg_duration_seconds=_mean(durations.get(node_id)),
            avg_wait_seconds=_mean(waits.get(node_id)),
            avg_total_seconds=_mean(totals.get(node_id)),
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


# --- conformance (target vs. actual) --------------------------------------


class ConformanceStep(BaseModel):
    """How often a modelled activity was completed, and how long it took."""

    node_id: str
    label: str | None = None
    completed: int
    avg_total_seconds: float | None = None


class ConformanceDeviation(BaseModel):
    """An observed directly-follows transition the model does not allow."""

    source: str
    target: str
    source_label: str | None = None
    target_label: str | None = None
    frequency: int


class ConformanceReport(BaseModel):
    """Target/actual comparison of one schema against its recorded history.

    ``steps`` covers every ACTIVITY/SUBPROCESS of the model (``completed == 0``
    = never executed). ``deviations`` lists observed transitions the model
    cannot produce -- typically ad-hoc changes, or history recorded on an
    earlier revision whose structure differed. ``foreign_steps`` are completed
    node ids the model does not contain at all (ad-hoc inserted steps).
    Read-only, never persisted (like :func:`compute_kpis`).
    """

    schema_id: str
    instances: int
    steps: list[ConformanceStep]
    deviations: list[ConformanceDeviation]
    foreign_steps: list[str]


_STEP_TYPES = frozenset({NodeType.ACTIVITY, NodeType.SUBPROCESS})


def model_directly_follows(schema: ProcessSchema) -> set[tuple[str, str]]:
    """All ``(a, b)`` step pairs where ``b`` may directly follow ``a`` in a trace.

    Three sources, matching how the engine can order completions:

    1. **Sequence through gateways** -- from ``a`` along control edges, passing
       through non-step nodes (splits, joins, loop delimiters), to the next
       steps.
    2. **Loop back-jump** -- reaching a ``LOOP_END`` also continues at its
       ``LOOP_START`` (the back edge is implicit, never stored).
    3. **Parallel interleaving** -- steps in different branches of the same
       ``AND_SPLIT`` can complete in any order, so every cross-branch pair is
       allowed in both directions.

    An over-approximation on purpose (it ignores data conditions of XOR
    branches): a transition reported as a deviation is then certainly outside
    the model, never a false alarm caused by a branch decision.
    """

    loop_start_of: dict[str, str] = {}
    for node in schema.nodes.values():
        if node.type is NodeType.LOOP_START:
            try:
                end, _ = loop_block(schema, node.id)
            except ValueError:
                continue
            loop_start_of[end] = node.id

    def next_steps(node_id: str) -> set[str]:
        found: set[str] = set()
        stack = [e.target for e in schema.outgoing(node_id)]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            node = schema.nodes.get(current)
            if node is None:
                continue
            if node.type in _STEP_TYPES:
                found.add(current)
                continue
            stack.extend(e.target for e in schema.outgoing(current))
            if current in loop_start_of:
                stack.extend(e.target for e in schema.outgoing(loop_start_of[current]))
        return found

    steps = [n.id for n in schema.nodes.values() if n.type in _STEP_TYPES]
    allowed = {(a, b) for a in steps for b in next_steps(a)}
    for node in schema.nodes.values():
        if node.type is not NodeType.AND_SPLIT:
            continue
        try:
            _, branches = block_join(schema, node.id)
        except ValueError:
            continue
        step_sets = [
            {n for n in body if schema.nodes[n].type in _STEP_TYPES} for body in branches
        ]
        for i, left in enumerate(step_sets):
            for j, right in enumerate(step_sets):
                if i != j:
                    allowed |= {(a, b) for a in left for b in right}
    return allowed


def conformance(schema: ProcessSchema, events: Iterable[AuditEvent]) -> ConformanceReport:
    """Compare the recorded history of ``schema`` with the model (target/actual)."""

    events = list(events)
    grouped = _by_instance(events, schema.id)
    counts: dict[str, int] = {}
    observed: dict[tuple[str, str], int] = {}
    seen_labels: dict[str, str] = {}
    for entries in grouped.values():
        previous: str | None = None
        for event in entries:
            if event.event_type is not EventType.ACTIVITY_COMPLETED or event.node_id is None:
                continue
            counts[event.node_id] = counts.get(event.node_id, 0) + 1
            if event.label:
                seen_labels[event.node_id] = event.label
            if previous is not None:
                key = (previous, event.node_id)
                observed[key] = observed.get(key, 0) + 1
            previous = event.node_id

    kpis = {s.node_id: s for s in compute_kpis(events, schema.id).activity_stats}
    steps = [
        ConformanceStep(
            node_id=node.id,
            label=node.label,
            completed=counts.get(node.id, 0),
            avg_total_seconds=kpis[node.id].avg_total_seconds if node.id in kpis else None,
        )
        for node in schema.nodes.values()
        if node.type in _STEP_TYPES
    ]
    allowed = model_directly_follows(schema)

    def label(node_id: str) -> str | None:
        # Model label first; an ad-hoc step is not in the model, but its
        # completion event recorded the name it had in the instance.
        node = schema.nodes.get(node_id)
        if node is not None and node.label:
            return node.label
        return seen_labels.get(node_id)

    deviations = [
        ConformanceDeviation(
            source=a, target=b, source_label=label(a), target_label=label(b), frequency=f
        )
        for (a, b), f in sorted(observed.items(), key=lambda kv: -kv[1])
        if (a, b) not in allowed
    ]
    foreign = sorted(nid for nid in counts if nid not in schema.nodes)
    return ConformanceReport(
        schema_id=schema.id,
        instances=len(grouped),
        steps=steps,
        deviations=deviations,
        foreign_steps=foreign,
    )

