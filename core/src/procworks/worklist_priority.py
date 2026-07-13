# SPDX-License-Identifier: BUSL-1.1
"""Time-based, automatic worklist prioritisation (read-only view logic).

This module turns the *target times* modelled on activities into the runtime
ordering of every agent's todo list: a task that is about to blow its target
time rises to the top, an overdue task sits above everything else. It realises
the "Zeitbasierte-Priorisierung-Konzept" and is deliberately a **boundary /
view** concern, exactly like ``metrics.py`` and ``assignment.py``:

* it holds **no state** and **persists nothing** -- the criticality of a task
  is *derived* at read time from the schema target times, the instance
  activation clock and the current wall-clock, just like ``WorkItemPriority``
  derives its level and never stores it;
* it changes **no** correctness rule -- ``validator.py`` and ``execution.py``
  are untouched. Correctness by Construction stays intact (leitplanke L2).

The logic is pure and clock-injected (``TimeContext.now``), so it is fully
deterministic and testable with a fake clock, without the API (concept step Z0).

Consistency note (deviation from the concept draft): the concept first proposed
deriving the node activation time from the audit log (variant P1). The audit log
has **no** node-activation event (only ``INSTANCE_CREATED`` /
``ACTIVITY_STARTED`` / ``ACTIVITY_COMPLETED``), so that derivation would require
replaying the engine's activation semantics (walking back through gateways and
loops) outside the engine -- fragile and a duplication of core logic. The
robust, precise mechanism is therefore the explicit
``ProcessInstance.node_activated_at`` clock stamped at the API boundary; this
module only *reads* it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from procworks.model import (
    CRITICALITY_RANK,
    ProcessSchema,
    TimeConstraint,
    TimeCriticality,
)

#: Consumption-ratio thresholds of the criticality bands (Section 5.1). Kept as
#: named constants so the bands stay explainable and can later be made
#: configurable per process/priority class without touching the formula.
WARNING_THRESHOLD = 0.5
AT_RISK_THRESHOLD = 0.8


def target_seconds(constraint: TimeConstraint | None) -> float | None:
    """Resolve the reaction target time of a node (fallback rule S).

    Returns ``target_lead_seconds`` when set (the SLA reaction time measured from
    activation), otherwise ``max_duration_seconds`` as a pragmatic fallback so a
    single annotated duration already drives the automatic ordering. Returns
    ``None`` when neither is set -- the task then takes no part in the time
    ordering.
    """

    if constraint is None:
        return None
    if constraint.target_lead_seconds is not None:
        return constraint.target_lead_seconds
    return constraint.max_duration_seconds


def criticality_from_ratio(ratio: float) -> TimeCriticality:
    """Map a consumption ratio ``rho`` to its criticality band (Section 5.1).

    ``rho < 0.5`` -> ON_TRACK, ``0.5 <= rho < 0.8`` -> WARNING,
    ``0.8 <= rho < 1.0`` -> AT_RISK, ``rho >= 1.0`` -> OVERDUE.
    """

    if ratio >= 1.0:
        return TimeCriticality.OVERDUE
    if ratio >= AT_RISK_THRESHOLD:
        return TimeCriticality.AT_RISK
    if ratio >= WARNING_THRESHOLD:
        return TimeCriticality.WARNING
    return TimeCriticality.ON_TRACK


def _bump(band: TimeCriticality) -> TimeCriticality:
    """Raise a band by one step (WARNING -> AT_RISK -> OVERDUE), capped at
    OVERDUE; ``NONE`` is never bumped (no time statement)."""

    order = [
        TimeCriticality.ON_TRACK,
        TimeCriticality.WARNING,
        TimeCriticality.AT_RISK,
        TimeCriticality.OVERDUE,
    ]
    if band not in order:
        return band
    return order[min(order.index(band) + 1, len(order) - 1)]


def remaining_critical_path_seconds(
    schema: ProcessSchema, node_id: str
) -> float | None:
    """Longest accumulated target duration from ``node_id`` to any END node.

    A *forward* dynamic program (this node's own duration plus the longest path
    ahead of it), the mirror image of the validator's start->node critical path
    ``validator._critical_path_seconds``. It is used for the process-deadline
    slack (Section 5.2): how much target work still lies between an open task and
    the end of the process. Returns ``None`` if the control graph is not a
    well-formed DAG (e.g. during incremental construction) -- the slack factor is
    then simply omitted.

    The two DPs are intentionally kept separate: the validator's version runs
    forward from START (completion time), this one runs backward from a node
    (remaining work), so reusing the validator function verbatim -- as the
    concept draft assumed -- is not possible.
    """

    nodes = schema.nodes
    if node_id not in nodes:
        return None

    succ: dict[str, list[str]] = {nid: [] for nid in nodes}
    outdegree: dict[str, int] = {nid: 0 for nid in nodes}
    for edge in schema.edges:
        if edge.source in nodes and edge.target in nodes:
            succ[edge.source].append(edge.target)
            outdegree[edge.source] += 1

    def duration(nid: str) -> float:
        constraint = schema.time_constraints.get(nid)
        if constraint is None or constraint.max_duration_seconds is None:
            return 0.0
        return constraint.max_duration_seconds

    # Reverse topological longest path: process nodes once all successors are
    # done. ``longest[n]`` = duration(n) + max longest over successors.
    remaining_out = dict(outdegree)
    longest: dict[str, float] = {}
    pred: dict[str, list[str]] = {nid: [] for nid in nodes}
    for src, targets in succ.items():
        for tgt in targets:
            pred[tgt].append(src)

    queue = [nid for nid, deg in outdegree.items() if deg == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        best_ahead = max((longest[t] for t in succ[current]), default=0.0)
        longest[current] = duration(current) + best_ahead
        for p in pred[current]:
            remaining_out[p] -= 1
            if remaining_out[p] == 0:
                queue.append(p)
    if visited != len(nodes):  # a cycle -> not a DAG, leave the factor out
        return None
    return longest.get(node_id)


@dataclass
class TimeContext:
    """The runtime clock inputs for prioritising one instance's worklist.

    Injected rather than read from a global clock so the whole logic is
    deterministic and testable with a fake ``now`` (concept step Z0).
    """

    #: Current wall-clock time (the "reading time").
    now: datetime
    #: Per-node activation time (``ProcessInstance.node_activated_at``).
    activated_at: dict[str, datetime] = field(default_factory=dict)
    #: Instance creation time, origin of the process-deadline slack; optional.
    started_at: datetime | None = None
    #: Whole-process deadline in seconds (``ProcessSchema.deadline_seconds``).
    deadline_seconds: float | None = None


@dataclass(frozen=True)
class TimeAssessment:
    """The derived time view of a single open task (never persisted)."""

    #: Resolved reaction target time in seconds (fallback rule S), or ``None``.
    target_seconds: float | None
    #: Seconds elapsed since the node became ready, or ``None`` without a clock.
    elapsed_seconds: float | None
    #: Absolute due time (activation + target), or ``None``.
    due_at: datetime | None
    #: Target minus elapsed; negative once overdue. ``None`` without a target.
    remaining_seconds: float | None
    #: The criticality band (already including the process-slack bump).
    criticality: TimeCriticality


def assess(
    schema: ProcessSchema, node_id: str, ctx: TimeContext
) -> TimeAssessment:
    """Derive the full time view of one open task (Sections 5.1-5.2).

    Returns the ``NONE`` band (and all-``None`` numbers) when the task has no
    resolvable target time or no activation stamp yet, so an unannotated model or
    a pre-feature instance behaves exactly as before (backward compatible).
    """

    target = target_seconds(schema.time_constraints.get(node_id))
    activated = ctx.activated_at.get(node_id)
    if target is None or target <= 0 or activated is None:
        return TimeAssessment(
            target_seconds=target,
            elapsed_seconds=None,
            due_at=None,
            remaining_seconds=None,
            criticality=TimeCriticality.NONE,
        )

    elapsed = (ctx.now - activated).total_seconds()
    due_at = activated + timedelta(seconds=target)
    remaining = target - elapsed
    band = criticality_from_ratio(elapsed / target)

    # Process-deadline slack (Section 5.2, optional second factor): when the
    # remaining wall-clock to the deadline is smaller than the target work still
    # ahead of this node, the step's criticality is raised one band. Omitted
    # geräuschlos when there is no deadline / start time / well-formed DAG.
    if (
        band is not TimeCriticality.OVERDUE
        and ctx.deadline_seconds is not None
        and ctx.started_at is not None
    ):
        remaining_ahead = remaining_critical_path_seconds(schema, node_id)
        if remaining_ahead is not None:
            deadline_at = ctx.started_at + timedelta(seconds=ctx.deadline_seconds)
            slack = (deadline_at - ctx.now).total_seconds() - remaining_ahead
            if slack < 0:
                band = _bump(band)

    return TimeAssessment(
        target_seconds=target,
        elapsed_seconds=elapsed,
        due_at=due_at,
        remaining_seconds=remaining,
        criticality=band,
    )


def sort_key(
    criticality: TimeCriticality,
    priority_rank: int,
    due_at: datetime | None,
    label: str,
    node_id: str,
) -> tuple[int, int, float, str, str]:
    """The lexicographic worklist sort key (Section 5.3, descending urgency).

    Ordered by (1) time criticality band -- deadline risk always dominates,
    (2) static business priority as the tie-break within a band, (3) earliest
    due date (EDD) so what breaks first comes first, (4) label/node_id for a
    stable, reproducible tail. Deliberately *not* a weighted score: a
    lexicographic banding is deterministic, testable and explainable (L4).

    Sort the list ascending by this key for the most-urgent-first ordering
    (ranks are negated so higher rank sorts earlier).
    """

    due = due_at.timestamp() if due_at is not None else float("inf")
    return (-CRITICALITY_RANK[criticality], -priority_rank, due, label, node_id)
