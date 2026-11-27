# SPDX-License-Identifier: BUSL-1.1
"""Audit/Event Log and monitoring aggregation (roadmap step 15).

The execution core stays pure: runtime events are recorded at the API boundary
into an append-only :class:`AuditLog`. The recorded history is the single basis
for monitoring KPIs, the per-instance audit timeline and a lightweight
process-mining map (directly-follows graph).

This module holds no correctness logic; it only observes what already happened.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
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
    #: Detail-state transitions of one activity (E2): pause/continue,
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
    #: clean.
    INSTANCE_DATA_SET = "INSTANCE_DATA_SET"
    INSTANCE_COMPLETED = "INSTANCE_COMPLETED"
    MAIL_SENT = "MAIL_SENT"          # modelled notification delivered (metadata only)
    MAIL_FAILED = "MAIL_FAILED"      # notification dead-lettered after retries (metadata)
    #: A monotone time-ratchet checkpoint of the licensing layer, embedded into
    #: the hash chain so the effective-time high-water-mark cannot be silently
    #: rolled back. It is *not* a process event and is
    #: excluded from KPI/mining aggregation via :data:`NON_PROCESS_EVENTS`.
    TIME_ANCHOR = "TIME_ANCHOR"
    #: An administrator acknowledged the historic branches of the hash chain
    #: (see :func:`verify_chain`). ``detail`` names them (``branches``, the
    #: sequence numbers, comma-separated), the last event checked
    #: (``checked_through``), the ``reason`` and the ``actor``. Written once
    #: the branches are reviewed; it rewrites nothing. Not a process event.
    CHAIN_ANCHOR = "CHAIN_ANCHOR"


#: Event types that are recorded for tamper evidence but do not describe process
#: progress; aggregation (KPIs, process map, per-instance grouping) skips them.
NON_PROCESS_EVENTS: frozenset[EventType] = frozenset(
    {EventType.TIME_ANCHOR, EventType.CHAIN_ANCHOR}
)

#: Instance and schema id of a :attr:`EventType.CHAIN_ANCHOR` event -- it
#: belongs to no case, like the licensing time anchor.
CHAIN_PSEUDO_ID = "__audit_chain__"


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


def _stored_time_forms(timestamp: datetime) -> list[datetime]:
    """The forms in which one stored instant may have been hashed.

    The hash covers ``timestamp.isoformat()`` as the writer had it -- in
    practice an aware UTC time. The database hands the same instant back in
    another form: SQLite without zone (naive), PostgreSQL in the session's
    zone (``+02:00`` on a server set to local time). Recomputing with the form
    read back would call an untouched event changed. All forms name the same
    instant, so accepting them does not let a changed time pass.

    :param timestamp: the time stamp as read back.
    :returns: as read, in UTC, and both without zone (no duplicates).
    """

    aware = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)
    in_utc = aware.astimezone(UTC)
    forms: list[datetime] = []
    for form in (
        timestamp,
        in_utc,
        timestamp.replace(tzinfo=None),
        in_utc.replace(tzinfo=None),
    ):
        if all(form.isoformat() != known.isoformat() for known in forms):
            forms.append(form)
    return forms


def _hash_matches(event: AuditEvent) -> bool:
    """Whether ``event`` still carries the hash of its stored fields.

    :param event: a stored event (its own ``prev_hash`` is taken as given).
    :returns: ``True`` when :func:`chain_hash` over the event -- with its time
        in one of the forms of :func:`_stored_time_forms` -- equals
        ``event.entry_hash``; ``False`` exactly when a field or the hash was
        changed.
    """

    return any(
        chain_hash(
            event.prev_hash,
            seq=event.seq,
            timestamp=form,
            event_type=event.event_type,
            instance_id=event.instance_id,
            schema_id=event.schema_id,
            schema_version=event.schema_version,
            node_id=event.node_id,
            label=event.label,
            agent_id=event.agent_id,
            detail=event.detail,
        )
        == event.entry_hash
        for form in _stored_time_forms(event.timestamp)
    )


@dataclass(frozen=True)
class ChainState:
    """Where the hash chain ends, as the audit log itself records it.

    Kept by the log next to the events (a database row, or the in-memory
    counters) and moved forward under the same lock as every append, so it
    always names the event the next append will link to.

    :param head_hash: ``entry_hash`` of the newest chained event ("" when empty).
    :param head_seq: its sequence number (0 when empty). A check compares it
        with the newest stored event: an end that was removed, or an event
        added without taking the lock, no longer matches.
    :param serialized_since: the newest sequence number written *before*
        appending was serialised (0 for a log that always was). Only events up
        to it can carry a branch that is not a defect; a branch after it means
        a writer bypassed the lock.
    """

    head_hash: str
    head_seq: int
    serialized_since: int
    #: Number of events up to ``serialized_since`` when the update recorded
    #: the historic region (:func:`historic_fingerprint`); ``None`` when no
    #: record exists (in memory, or a state built without one).
    historic_events: int | None = None
    #: Digest over those events' numbers and hashes, recorded with
    #: ``historic_events``; ``None`` when no record exists.
    historic_digest: str | None = None


def historic_fingerprint(pairs: Iterable[tuple[int, str]]) -> tuple[int, str]:
    """Count and digest of the historic region of the chain.

    Recorded once, when the update introduces serialised appending
    (Alembic ``0017``, or a database that creates the chain end on first
    use), and compared by :func:`verify_chain` on every check. Before that
    record, an event at the end of a historic branch -- no later event names
    its hash -- could be removed or rewritten together with its own hash
    without breaking any link, and the first anchor then recorded the forged
    state as reviewed.

    The digest is SHA-256 over one line ``"<seq>:<entry_hash>\\n"`` per event
    in sequence order: the number pins removals and events slipped into a
    gap of the numbering, the hash every change of content. A plain digest
    instead of a list keeps the record small for any history; it says
    *that* the region changed, not where -- the anchors' records and a
    backup from before the update say where.

    Events without hash (written before the chain existed) enter as
    ``"<seq>:"``: their removal is found, a change of their content is not
    (they never had a hash to compare with).

    The migration computes the same lines itself (it must not import the
    application); a test pins that both agree.

    :param pairs: ``(seq, entry_hash)`` of every event of the region, in
        ascending ``seq`` order.
    :returns: ``(count, hex digest)``; an empty region gives ``0`` and the
        digest of no input.
    """

    digest = hashlib.sha256()
    count = 0
    for seq, entry_hash in pairs:
        digest.update(f"{seq}:{entry_hash}\n".encode())
        count += 1
    return count, digest.hexdigest()


class ChainBranch(BaseModel):
    """A historic branch: an event that names an earlier event than its
    immediate predecessor, written before appending was serialised.

    Two requests recording an event at the same moment both linked to the same
    end of the chain. Each event is intact (its own hash matches); only the
    chain is not a single line there. Reported, never rewritten -- the log is
    append-only.
    """

    seq: int
    timestamp: datetime
    #: Sequence number of the event it links to (0 = the start of the chain).
    links_to: int
    #: Sequence number of the :attr:`EventType.CHAIN_ANCHOR` event that
    #: acknowledged it, ``None`` while it is still open.
    anchored_by: int | None = None


class ChainDefect(BaseModel):
    """A finding of the chain check that no anchor can excuse.

    ``kind`` is a stable token for programs:

    * ``entry-altered`` -- the event's stored fields or hash no longer match
      (the event itself was changed);
    * ``link-missing`` -- the event links to a hash no earlier event carries
      (its predecessor was removed or changed);
    * ``unserialized-branch`` -- a branch after appending was serialised (a
      writer bypassed the lock, e.g. an older process during an update);
    * ``branch-not-anchored`` -- a branch before an anchor that the anchor does
      not name (it appeared after the anchor was written);
    * ``unchained-after-chain`` -- an event without hash after chained events;
    * ``end-mismatch`` -- the newest event is not the end the log recorded
      (the end was removed, or something was appended without the lock);
    * ``anchored-branch-missing`` -- an event an anchor names (a branch or a
      dead end) is gone, or a branch it names is no longer a branch (an event
      before it, typically the dead end of the branch, was removed);
    * ``anchored-branch-altered`` -- an event an anchor names carries another
      hash than the anchor recorded (changed together with its own hash,
      which at a dead end breaks no link);
    * ``historic-range-changed`` -- the events up to ``serialized_since`` no
      longer match the count and digest the update recorded
      (:func:`historic_fingerprint`): one was removed, rewritten together
      with its hash, or slipped into a gap -- found also before any anchor.
      ``seq`` is ``serialized_since``; the record does not say which event.
    * ``historic-record-changed`` -- the record of the historic region (count
      and digest at the chain end) is not what an anchor recorded: it was
      changed or removed after that anchor. ``seq`` is the anchor's.

    An unnamed dead end before an anchor that records dead ends is reported
    as ``branch-not-anchored`` as well (it appeared after the anchor).
    """

    kind: str
    seq: int
    timestamp: datetime | None = None
    message: str


class ChainDeadEnd(BaseModel):
    """The last event of a historic branch: no later event links to it.

    Removing such an event, or changing it together with its own hash, breaks
    no link -- the chain alone cannot show it. An anchor therefore records
    every open dead end with its hash (``dead_ends``), and the check holds
    the log to that record. Listed in the report so an administrator sees
    what an anchor will record.
    """

    seq: int
    timestamp: datetime
    #: The hash the event carries now (what an anchor records).
    entry_hash: str
    #: Sequence number of the recording anchor that names it, ``None`` while
    #: no such anchor exists.
    anchored_by: int | None = None


class ChainReport(BaseModel):
    """Result of :func:`verify_chain`.

    ``status``:

    * ``intact`` -- one linear chain, or every historic branch is anchored;
    * ``open`` -- no defect, but historic branches wait for an anchor;
    * ``defective`` -- at least one defect (see :class:`ChainDefect`).
    """

    status: str
    events: int
    #: Events from before the hash chain existed (no hashes, only at the start).
    unchained: int = 0
    head_seq: int
    serialized_since: int
    branches: list[ChainBranch] = Field(default_factory=list)
    defects: list[ChainDefect] = Field(default_factory=list)
    #: Sequence numbers of the anchor events that passed the check.
    anchors: list[int] = Field(default_factory=list)
    #: Ends of historic branches (see :class:`ChainDeadEnd`).
    dead_ends: list[ChainDeadEnd] = Field(default_factory=list)
    #: Number of historic events the update recorded (``None``: no record).
    historic_events: int | None = None
    #: Digest the update recorded over them (:func:`historic_fingerprint`);
    #: ``None`` without a record. Shown so an operator can keep it outside
    #: the database: before the first anchor, whoever can rewrite the events
    #: can recompute this record too, and only an outside copy reveals it.
    historic_digest: str | None = None


def _anchored_branches(event: AuditEvent) -> set[int]:
    """The branch sequence numbers an anchor event names (malformed parts ignored)."""

    raw = event.detail.get("branches", "")
    return {int(part) for part in raw.split(",") if part.strip().isdigit()}


#: Detail key of an anchor that records the dead ends it saw. Anchors written
#: before this record existed lack it; for them only the named branches are
#: checked (each must still be a branch).
_DEAD_ENDS_KEY = "dead_ends"
_BRANCH_HASHES_KEY = "branch_hashes"


def _records_events(anchor: AuditEvent) -> bool:
    """Whether ``anchor`` records the hashes of what it anchored."""

    return _DEAD_ENDS_KEY in anchor.detail


def _recorded(anchor: AuditEvent, key: str) -> dict[int, str]:
    """The ``seq:hash`` pairs an anchor records under ``key``.

    :param anchor: the anchor event.
    :param key: ``dead_ends`` or ``branch_hashes``.
    :returns: hash by sequence number; malformed parts are ignored (an anchor
        is written only by :func:`anchor_record`, and a changed anchor fails
        its own hash anyway).
    """

    pairs: dict[int, str] = {}
    for part in anchor.detail.get(key, "").split(","):
        seq, sep, digest = part.strip().partition(":")
        if sep and seq.isdigit() and digest:
            pairs[int(seq)] = digest
    return pairs


def _seq_hash_list(pairs: Iterable[tuple[int, str]]) -> str:
    """``seq:hash`` pairs as an anchor stores them (comma separated)."""

    return ",".join(f"{seq}:{digest}" for seq, digest in pairs)


def anchor_record(events: Iterable[AuditEvent], report: ChainReport) -> dict[str, str]:
    """What a new anchor records: every open branch and dead end, with hashes.

    The anchor documents a human review of the historic branches. So that the
    reviewed state cannot be changed afterwards without a trace, it records:

    * ``branches`` -- the open branches' sequence numbers (as before);
    * ``branch_hashes`` -- ``seq:hash`` of each of them;
    * ``dead_ends`` -- ``seq:hash`` of every open dead end
      (:class:`ChainDeadEnd`): the events whose removal or rewriting no link
      would reveal;
    * ``historic_events``/``historic_digest`` -- the record of the historic
      region as the check saw it (only when the chain end has one). The
      record lives in the same database as the events; inside the anchor's
      hashed detail it is pinned, and a later change of it is
      ``historic-record-changed``.

    The anchor's own hash covers these values; :func:`verify_chain` then
    requires every recorded event to be present with the recorded hash and
    every named branch to be still a branch.

    :param events: the log as checked (to look up the branches' hashes).
    :param report: :func:`verify_chain` over exactly these events.
    :returns: the detail entries (without ``reason``/``actor``/``checked_through``).
    """

    by_seq = {event.seq: event for event in events}
    open_branches = [b.seq for b in report.branches if b.anchored_by is None]
    record = {
        "branches": ",".join(str(seq) for seq in open_branches),
        _BRANCH_HASHES_KEY: _seq_hash_list(
            (seq, by_seq[seq].entry_hash) for seq in open_branches if seq in by_seq
        ),
        _DEAD_ENDS_KEY: _seq_hash_list(
            (end.seq, end.entry_hash) for end in report.dead_ends if end.anchored_by is None
        ),
    }
    if report.historic_digest is not None:
        record[_HISTORIC_EVENTS_KEY] = str(report.historic_events)
        record[_HISTORIC_DIGEST_KEY] = report.historic_digest
    return record


#: Detail keys of an anchor that records the historic region's record.
_HISTORIC_EVENTS_KEY = "historic_events"
_HISTORIC_DIGEST_KEY = "historic_digest"


def _check_record_against_anchors(
    anchors: list[AuditEvent], state: ChainState, defects: list[ChainDefect]
) -> None:
    """Hold the record of the historic region to what anchors recorded.

    Part of :func:`verify_chain`. Every (intact) anchor that carries
    ``historic_digest`` pinned the record as it was then; the chain end must
    still carry exactly that count and digest. A record rewritten together
    with the events, or removed, after an anchor is ``historic-record-changed``
    at that anchor. Before the first anchor nothing pins the record -- only a
    copy kept outside the database (the digest is in the report) does.

    :param anchors: the intact anchor events, in sequence order.
    :param state: the chain end with its record.
    :param defects: extended in place (one defect per disagreeing anchor).
    """

    current = (
        None if state.historic_events is None else str(state.historic_events),
        state.historic_digest,
    )
    for anchor in anchors:
        digest = anchor.detail.get(_HISTORIC_DIGEST_KEY)
        if digest is None:
            continue
        recorded = (anchor.detail.get(_HISTORIC_EVENTS_KEY), digest)
        if recorded != current:
            defects.append(
                ChainDefect(
                    kind="historic-record-changed",
                    seq=anchor.seq,
                    timestamp=anchor.timestamp,
                    message=(
                        f"the record of the history ({current[0]} events) is not what "
                        f"anchor {anchor.seq} recorded ({recorded[0]} events): it was "
                        "changed or removed after the anchor"
                    ),
                )
            )


def verify_chain(events: Iterable[AuditEvent], state: ChainState) -> ChainReport:
    """Check the hash chain of the whole log; tell historic branches from defects.

    Walks the events in sequence order. For each event:

    1. its hash is recomputed -- a mismatch is ``entry-altered``;
    2. its ``prev_hash`` must be the hash of the immediately preceding event.
       If it names an *earlier* event instead (or the chain start ""), the
       chain branched there: up to ``state.serialized_since`` that is a
       historic branch, after it ``unserialized-branch``. If it names no
       earlier event at all, a predecessor was removed or changed:
       ``link-missing``.

    Then every historic branch is matched with the anchor events after it: one
    that names it closes it (``anchored_by``); an anchor after it that does not
    name it makes it ``branch-not-anchored`` (an anchor names every branch open
    when it is written, so a branch it does not know appeared later). Anchors
    whose own hash fails do not count. Finally the newest event must be the
    end the log recorded (``end-mismatch`` otherwise).

    What an anchor names must stay as it was (append-only: nothing is ever
    excused, nothing rewritten):

    * every branch an anchor names must still be a historic branch -- else
      ``anchored-branch-missing`` (removing the dead end before a branch can
      make the branch look linear);
    * an anchor that records hashes (:func:`anchor_record`) holds every
      recorded branch and dead end to them: gone is
      ``anchored-branch-missing``, another hash ``anchored-branch-altered``;
    * every historic dead end (a chained event up to ``serialized_since``
      that no later event links to, the newest event excepted) must be named
      by a recording anchor after it -- else ``branch-not-anchored``. Without
      any recording anchor dead ends are only listed.

    The historic region itself is fixed from the update on: when the chain
    end carries a record (count and digest up to ``serialized_since``,
    :func:`historic_fingerprint`), the stored events must still match it --
    else ``historic-range-changed``, also before any anchor.

    Tampering is never reported as a historic branch: a changed event fails
    its own hash, a changed hash or a removed event leaves a link no earlier
    event explains, and a branch slipped in before an anchor is not named by
    it. Only rewriting every later event *and* the recorded end goes
    unnoticed -- the limit of any self-contained hash chain.

    Events without hashes (written before the chain existed) are accepted only
    as a prefix and counted in ``unchained``.

    :param events: the whole log (any order; sorted by ``seq`` here).
    :param state: the end the log recorded (:meth:`AuditLog.chain_state`).
    :returns: the report; see :class:`ChainReport` for the status values.
    """

    ordered = sorted(events, key=lambda event: event.seq)
    defects: list[ChainDefect] = []
    candidates: list[ChainBranch] = []
    anchors: list[AuditEvent] = []
    #: Position of every chained event so far, by its stored hash; "" is the
    #: start of the chain (0).
    seen: dict[str, int] = {"": 0}
    previous_hash = ""
    chained = False
    unchained = 0

    def defect(kind: str, event: AuditEvent, message: str) -> None:
        defects.append(
            ChainDefect(kind=kind, seq=event.seq, timestamp=event.timestamp, message=message)
        )

    for event in ordered:
        if not event.entry_hash and not event.prev_hash:
            if chained:
                defect(
                    "unchained-after-chain",
                    event,
                    "event without hash after the chain had begun",
                )
            else:
                unchained += 1
            continue
        intact = _hash_matches(event)
        if not intact:
            defect(
                "entry-altered",
                event,
                "the stored event no longer matches its hash (event changed)",
            )
        if event.prev_hash != previous_hash:
            target = seen.get(event.prev_hash)
            if target is None:
                defect(
                    "link-missing",
                    event,
                    "links to a hash no earlier event carries (predecessor removed or changed)",
                )
            elif event.seq <= state.serialized_since:
                candidates.append(
                    ChainBranch(seq=event.seq, timestamp=event.timestamp, links_to=target)
                )
            else:
                defect(
                    "unserialized-branch",
                    event,
                    f"branches off event {target} although appending is serialised",
                )
        if intact and event.event_type is EventType.CHAIN_ANCHOR:
            anchors.append(event)
        seen.setdefault(event.entry_hash, event.seq)
        previous_hash = event.entry_hash
        chained = True

    branches: list[ChainBranch] = []
    for branch in candidates:
        later = [anchor for anchor in anchors if anchor.seq > branch.seq]
        naming = [anchor for anchor in later if branch.seq in _anchored_branches(anchor)]
        if naming:
            branches.append(branch.model_copy(update={"anchored_by": naming[0].seq}))
        elif later:
            defects.append(
                ChainDefect(
                    kind="branch-not-anchored",
                    seq=branch.seq,
                    timestamp=branch.timestamp,
                    message=f"branch before anchor {later[0].seq} that the anchor does not name",
                )
            )
        else:
            branches.append(branch)

    dead_ends = _check_anchored_events(ordered, state, candidates, anchors, defects)
    _check_historic_record(ordered, state, defects)
    _check_record_against_anchors(anchors, state, defects)

    newest = ordered[-1] if ordered else None
    newest_seq = newest.seq if newest is not None else 0
    newest_hash = newest.entry_hash if newest is not None else ""
    if (newest_seq, newest_hash) != (state.head_seq, state.head_hash):
        defects.append(
            ChainDefect(
                kind="end-mismatch",
                seq=state.head_seq,
                timestamp=newest.timestamp if newest is not None else None,
                message=(
                    f"the log recorded its end at event {state.head_seq}, "
                    f"the newest stored event is {newest_seq}"
                ),
            )
        )

    defects.sort(key=lambda found: found.seq)
    if defects:
        status = "defective"
    elif any(branch.anchored_by is None for branch in branches):
        status = "open"
    else:
        status = "intact"
    return ChainReport(
        status=status,
        events=len(ordered),
        unchained=unchained,
        head_seq=state.head_seq,
        serialized_since=state.serialized_since,
        branches=branches,
        defects=defects,
        anchors=[anchor.seq for anchor in anchors],
        dead_ends=dead_ends,
        historic_events=state.historic_events,
        historic_digest=state.historic_digest,
    )


def _check_historic_record(
    ordered: list[AuditEvent], state: ChainState, defects: list[ChainDefect]
) -> None:
    """Hold the historic region to what the update recorded.

    Part of :func:`verify_chain`. Recomputes :func:`historic_fingerprint` over
    the stored events up to ``state.serialized_since`` and compares it with
    the record of the chain end. Independent of anchors: it holds from the
    update on, so a dead end removed or rewritten before the first anchor is
    a defect, and the anchor is refused instead of recording the forgery.

    Without a record (``state.historic_digest is None``) nothing is checked.

    :param ordered: all events in sequence order.
    :param state: the recorded end with its record.
    :param defects: the defects so far (extended in place by at most one
        ``historic-range-changed`` at ``serialized_since``).
    """

    if state.historic_digest is None:
        return
    count, digest = historic_fingerprint(
        (event.seq, event.entry_hash)
        for event in ordered
        if event.seq <= state.serialized_since
    )
    if (count, digest) == (state.historic_events, state.historic_digest):
        return
    defects.append(
        ChainDefect(
            kind="historic-range-changed",
            seq=state.serialized_since,
            message=(
                f"the events up to {state.serialized_since} no longer match the record "
                f"of the update ({state.historic_events} events, now {count}): "
                "an event was removed, rewritten together with its hash or slipped in"
            ),
        )
    )


def _check_anchored_events(
    ordered: list[AuditEvent],
    state: ChainState,
    candidates: list[ChainBranch],
    anchors: list[AuditEvent],
    defects: list[ChainDefect],
) -> list[ChainDeadEnd]:
    """Hold the log to what its anchors name; find the historic dead ends.

    Part of :func:`verify_chain` (see there for the rules); appends its
    findings to ``defects``, each ``(kind, seq)`` at most once.

    :param ordered: all events in sequence order.
    :param state: the recorded end (for ``serialized_since``).
    :param candidates: the historic branches found.
    :param anchors: the anchor events whose own hash holds.
    :param defects: the defects so far (extended in place).
    :returns: the historic dead ends, with the recording anchor naming each.
    """

    by_seq = {event.seq: event for event in ordered}
    branch_seqs = {branch.seq for branch in candidates}
    reported = {(found.kind, found.seq) for found in defects}

    def defect(kind: str, seq: int, message: str) -> None:
        if (kind, seq) in reported:
            return
        reported.add((kind, seq))
        event = by_seq.get(seq)
        defects.append(
            ChainDefect(
                kind=kind,
                seq=seq,
                timestamp=event.timestamp if event is not None else None,
                message=message,
            )
        )

    for anchor in anchors:
        for seq in sorted(_anchored_branches(anchor)):
            if seq not in branch_seqs:
                defect(
                    "anchored-branch-missing",
                    seq,
                    f"anchor {anchor.seq} names branch {seq}, which is no longer a branch "
                    "(the event or one before it was removed)",
                )
        if not _records_events(anchor):
            continue
        recorded = {**_recorded(anchor, _BRANCH_HASHES_KEY), **_recorded(anchor, _DEAD_ENDS_KEY)}
        for seq, digest in sorted(recorded.items()):
            event = by_seq.get(seq)
            if event is None:
                defect(
                    "anchored-branch-missing",
                    seq,
                    f"anchor {anchor.seq} names event {seq}, which was removed",
                )
            elif event.entry_hash != digest:
                defect(
                    "anchored-branch-altered",
                    seq,
                    f"event {seq} no longer carries the hash anchor {anchor.seq} recorded",
                )

    linked = {event.prev_hash for event in ordered if event.entry_hash or event.prev_hash}
    newest_seq = ordered[-1].seq if ordered else 0
    recording = [anchor for anchor in anchors if _records_events(anchor)]
    dead_ends: list[ChainDeadEnd] = []
    for event in ordered:
        if (
            not event.entry_hash
            or event.seq > state.serialized_since
            or event.seq == newest_seq
            or event.entry_hash in linked
        ):
            continue
        later = [anchor for anchor in recording if anchor.seq > event.seq]
        naming = [a for a in later if event.seq in _recorded(a, _DEAD_ENDS_KEY)]
        if not naming and later:
            defect(
                "branch-not-anchored",
                event.seq,
                f"end of a branch before anchor {later[0].seq} that the anchor does not name",
            )
        dead_ends.append(
            ChainDeadEnd(
                seq=event.seq,
                timestamp=event.timestamp,
                entry_hash=event.entry_hash,
                anchored_by=naming[0].seq if naming else None,
            )
        )
    return dead_ends


def check_chain(log: AuditLog, *, attempts: int = 3) -> ChainReport:
    """Read the whole log and its recorded end, and check the chain.

    Events and end are two reads. An append that commits between them moves
    the end past the newest event read -- that one check would see an
    ``end-mismatch`` that is not stored anywhere. So the pair is read again
    (up to ``attempts`` times) while the end lies *beyond* the events read.
    An end that stays beyond them is reported as it is (the newest events
    were removed). The opposite -- events beyond the recorded end -- is never
    a race (an append moves the end in its own transaction) and is reported
    straight away.

    :param log: the audit log.
    :param attempts: how often the pair is read at most (at least once).
    :returns: :func:`verify_chain` of the last pair read.
    """

    events, state = read_chain(log, attempts=attempts)
    return verify_chain(events, state)


def read_chain(log: AuditLog, *, attempts: int = 3) -> tuple[list[AuditEvent], ChainState]:
    """Read the events and the recorded end as one consistent pair.

    The reading rule of :func:`check_chain` (read again while the end lies
    beyond the events read), for callers that need the events as well -- the
    anchor looks up the hashes it records (:func:`anchor_record`).

    :param log: the audit log.
    :param attempts: how often the pair is read at most (at least once).
    :returns: the events and the end of the last read.
    """

    events: list[AuditEvent] = []
    state = ChainState(head_hash="", head_seq=0, serialized_since=0)
    for _ in range(max(1, attempts)):
        events = log.list_all()
        state = log.chain_state()
        newest = max((event.seq for event in events), default=0)
        if state.head_seq <= newest:
            break
    return events, state


class AuditLog(Protocol):
    """Minimal append-only interface for the event history.

    ``append`` may defer chaining to the end of the running unit of work (the
    database log does, see :class:`procworks.db.SqlAlchemyAuditLog`); the
    returned event is then provisional (``seq`` 0, no hashes). A caller that
    needs the stored hash passes ``immediate=True``.
    """

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
        immediate: bool = False,
    ) -> AuditEvent: ...

    def append_checked(
        self,
        decide: Callable[[list[AuditEvent], ChainState], dict[str, str]],
        event_type: EventType,
        instance_id: str,
        schema_id: str,
    ) -> AuditEvent: ...

    def list_all(self) -> list[AuditEvent]: ...

    def for_instance(self, instance_id: str) -> list[AuditEvent]: ...

    def instance_ids_for_agent(self, agent_id: str) -> set[str]: ...

    def revision(self) -> int: ...

    def head_hash(self) -> str: ...

    def chain_state(self) -> ChainState: ...

    def max_event_time(self) -> float: ...

    def clear(self) -> None: ...


class InMemoryAuditLog:
    """A trivial list-backed event log with a monotonic sequence counter.

    Appending is serialised by a lock: reading the end of the chain, hashing
    and recording the new end happen as one step. Without it, two threads
    (a request and a background delivery, or two requests) read the same end
    and both linked to it -- the chain branched and got duplicate numbers.
    """

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._seq = 0
        self._head = ""
        #: Reentrant: :meth:`append_checked` holds it across its own reads
        #: and the append.
        self._lock = threading.RLock()

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
        immediate: bool = False,
    ) -> AuditEvent:
        """Record one event at the end of the chain (under the lock).

        The time stamp is taken after the lock is held, so the stamps follow
        the sequence numbers; an explicit ``at`` (seeds) is kept as given.
        Always chained at once: in memory there is no transaction to wait for
        (the units of one process run one after another), so ``immediate``
        changes nothing here.
        """

        del immediate  # always immediate in memory

        detail = detail or {}
        with self._lock:
            seq = self._seq + 1
            timestamp = at or datetime.now(UTC)
            entry_hash = chain_hash(
                self._head,
                seq=seq,
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
                seq=seq,
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
            self._seq = seq
            self._head = entry_hash
            self._events.append(event)
            return event

    def append_checked(
        self,
        decide: Callable[[list[AuditEvent], ChainState], dict[str, str]],
        event_type: EventType,
        instance_id: str,
        schema_id: str,
    ) -> AuditEvent:
        """Read the log, let ``decide`` judge it, append -- as one step.

        For an event whose content depends on the whole chain (the anchor):
        two such appends at the same moment would both judge the same state
        and both append. Under the lock the second one sees the first.

        :param decide: gets every event and the chain end, returns the
            ``detail`` of the new event -- or raises, and nothing is appended.
        :param event_type: type of the new event.
        :param instance_id: its instance id (a pseudo id for the anchor).
        :param schema_id: its schema id.
        :returns: the appended event.
        """

        with self._lock:
            detail = decide(self.list_all(), self.chain_state())
            return self.append(event_type, instance_id, schema_id, detail=detail)

    def list_all(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def for_instance(self, instance_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.instance_id == instance_id]

    def instance_ids_for_agent(self, agent_id: str) -> set[str]:
        """The instances in which ``agent_id`` acted (any event naming it).

        Backs the reading right of a limited operator (it reads the cases it
        took part in) without handing the whole history to the caller.

        :param agent_id: the agent.
        :returns: the distinct ``instance_id`` of every event whose
            ``agent_id`` is ``agent_id``; empty if there is none.
        """

        with self._lock:
            return {e.instance_id for e in self._events if e.agent_id == agent_id}

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

    def chain_state(self) -> ChainState:
        """Return where the chain ends (see :class:`ChainState`).

        In memory, appending was serialised from the first event on, so
        ``serialized_since`` is 0: any branch is a defect.
        """

        with self._lock:
            return ChainState(head_hash=self._head, head_seq=self._seq, serialized_since=0)

    def max_event_time(self) -> float:
        """Return the newest recorded timestamp as epoch seconds (0.0 if empty).

        A monotone lower bound on real time for the licensing ratchet: an
        append-only log never moves this backwards.
        """

        if not self._events:
            return 0.0
        return max(e.timestamp.timestamp() for e in self._events)

    def clear(self) -> None:
        with self._lock:
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


#: Event types that mark an instance as having used an ad-hoc change.
_ADHOC_EVENT_TYPES = (
    EventType.ADHOC_INSERTED,
    EventType.ADHOC_DELETED,
    EventType.ADHOC_RENAMED,
)


def _first_event(
    entries: list[AuditEvent], event_type: EventType
) -> AuditEvent | None:
    """The first event of ``event_type`` in one instance's history.

    Args:
        entries: One instance's events, sorted by ``seq``.
        event_type: The type to look for.

    Returns:
        The earliest matching event, or ``None`` when the history has none
        (e.g. a running instance has no ``INSTANCE_COMPLETED``).
    """

    return next((e for e in entries if e.event_type is event_type), None)


def _used_adhoc_change(entries: list[AuditEvent]) -> bool:
    """Whether an instance applied at least one ad-hoc change.

    Args:
        entries: One instance's events.

    Returns:
        ``True`` if any event is an ad-hoc insert, delete or rename -- the
        flexibility proxy of :class:`KpiReport`; ``False`` for an empty list.
    """

    return any(e.event_type in _ADHOC_EVENT_TYPES for e in entries)


@dataclass
class _ActivityTimes:
    """Accumulates the per-activity figures of :func:`compute_kpis`.

    Fed one instance history at a time via :meth:`record_instance`, then
    turned into :class:`ActivityStat` rows by :meth:`stats`. The three
    duration lists follow the definitions on :class:`ActivityStat`
    (processing, waiting, lead time). Never persisted.
    """

    #: Completion count per node id, in order of first completion.
    completions: dict[str, int] = field(default_factory=dict)
    #: Label of the most recent completion per node id.
    labels: dict[str, str | None] = field(default_factory=dict)
    #: Processing times (started -> completed) per node id.
    durations: dict[str, list[float]] = field(default_factory=dict)
    #: Waiting times (ready -> started) per node id.
    waits: dict[str, list[float]] = field(default_factory=dict)
    #: Lead times (ready -> completed) per node id.
    totals: dict[str, list[float]] = field(default_factory=dict)

    def record_instance(self, entries: list[AuditEvent]) -> None:
        """Add the activity figures of one instance's history.

        Args:
            entries: One instance's events, sorted by ``seq``.

        Edge cases: events without a ``node_id`` are skipped. Only the first
        ``ACTIVITY_STARTED`` before a completion counts as its start, and a
        completion consumes it -- so a step executed again in a later loop
        round needs a fresh start. A completion without a start contributes
        no processing or waiting time; one without (parsable) ``ready_at``
        contributes no lead or waiting time. Waiting and lead times are
        clamped at ``0.0`` so a skewed clock cannot produce negative figures.
        """

        starts: dict[str, datetime] = {}
        for event in entries:
            if event.node_id is None:
                continue
            if event.event_type is EventType.ACTIVITY_STARTED:
                starts.setdefault(event.node_id, event.timestamp)
            elif event.event_type is EventType.ACTIVITY_COMPLETED:
                start = starts.pop(event.node_id, None)
                self._record_completion(event, event.node_id, start)

    def _record_completion(
        self, event: AuditEvent, node_id: str, start: datetime | None
    ) -> None:
        """Count one ``ACTIVITY_COMPLETED`` event and its durations.

        Args:
            event: The completion event.
            node_id: Its node id (passed separately because it is known to be
                set, which ``event.node_id`` does not express).
            start: The matching ``ACTIVITY_STARTED`` timestamp, or ``None``
                when the step was completed without being started.
        """

        self.completions[node_id] = self.completions.get(node_id, 0) + 1
        self.labels[node_id] = event.label
        if start is not None:
            self.durations.setdefault(node_id, []).append(
                (event.timestamp - start).total_seconds()
            )
        ready = _ready_at(event)
        if ready is not None:
            self.totals.setdefault(node_id, []).append(
                max(0.0, (event.timestamp - ready).total_seconds())
            )
            if start is not None:
                self.waits.setdefault(node_id, []).append(
                    max(0.0, (start - ready).total_seconds())
                )

    def stats(self) -> list[ActivityStat]:
        """The accumulated figures as :class:`ActivityStat` rows.

        Returns:
            One row per completed node, most completions first; ties keep
            the order of first completion (stable sort). Averages without
            any measurement are ``None``.
        """

        return [
            ActivityStat(
                node_id=node_id,
                label=self.labels.get(node_id),
                completed=count,
                avg_duration_seconds=_mean(self.durations.get(node_id)),
                avg_wait_seconds=_mean(self.waits.get(node_id)),
                avg_total_seconds=_mean(self.totals.get(node_id)),
            )
            for node_id, count in sorted(
                self.completions.items(), key=lambda kv: kv[1], reverse=True
            )
        ]


def compute_kpis(
    events: Iterable[AuditEvent], schema_id: str | None = None
) -> KpiReport:
    """Derive instance counts, average cycle time and per-activity figures.

    Args:
        events: The recorded history (any order; ``TIME_ANCHOR`` checkpoints
            are ignored).
        schema_id: Restrict the report to instances of this schema; ``None``
            reports over all schemas.

    Returns:
        A :class:`KpiReport`. An instance counts as completed when it has an
        ``INSTANCE_COMPLETED`` event, otherwise as running; its cycle time is
        only known when the ``INSTANCE_CREATED`` event is present, too.
        Activity figures are ordered by completion count, most frequent first
        (ties keep the order of first completion). Without any instance all
        averages and the ad-hoc ratio are ``None``.
    """

    grouped = _by_instance(events, schema_id)

    running = 0
    completed = 0
    cycle_times: list[float] = []
    activity_times = _ActivityTimes()
    adhoc_instances = 0

    for entries in grouped.values():
        created = _first_event(entries, EventType.INSTANCE_CREATED)
        done = _first_event(entries, EventType.INSTANCE_COMPLETED)
        if done is not None:
            completed += 1
            if created is not None:
                cycle_times.append((done.timestamp - created.timestamp).total_seconds())
        else:
            running += 1

        if _used_adhoc_change(entries):
            adhoc_instances += 1

        activity_times.record_instance(entries)

    return KpiReport(
        schema_id=schema_id,
        total_instances=len(grouped),
        running=running,
        completed=completed,
        avg_cycle_seconds=_mean(cycle_times),
        activity_stats=activity_times.stats(),
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

    loop_start_of = _loop_start_by_end(schema)
    steps = [n.id for n in schema.nodes.values() if n.type in _STEP_TYPES]
    allowed = {(a, b) for a in steps for b in _next_steps(schema, loop_start_of, a)}
    allowed |= _parallel_interleavings(schema)
    return allowed


def _loop_start_by_end(schema: ProcessSchema) -> dict[str, str]:
    """Map every ``LOOP_END`` id to the id of its ``LOOP_START``.

    The loop back edge is implicit (never stored as an edge), so the
    directly-follows walk needs this map to continue at the loop start.

    Args:
        schema: The schema to inspect.

    Returns:
        ``loop end id -> loop start id``. A ``LOOP_START`` whose block cannot
        be resolved is skipped instead of failing the whole conformance view.
    """

    loop_start_of: dict[str, str] = {}
    for node in schema.nodes.values():
        if node.type is NodeType.LOOP_START:
            try:
                end, _ = loop_block(schema, node.id)
            except ValueError:
                continue
            loop_start_of[end] = node.id
    return loop_start_of


def _next_steps(
    schema: ProcessSchema, loop_start_of: dict[str, str], node_id: str
) -> set[str]:
    """Steps reachable from ``node_id`` without passing another step.

    Follows control edges through non-step nodes (splits, joins, loop
    delimiters) and stops at the first ACTIVITY/SUBPROCESS on each path. At a
    ``LOOP_END`` the walk also continues behind the matching ``LOOP_START``
    (the implicit back jump).

    Args:
        schema: The schema to walk.
        loop_start_of: Map from :func:`_loop_start_by_end`.
        node_id: The step to start from (its own type is not checked).

    Returns:
        The set of steps that may directly follow ``node_id``. Edge targets
        missing from the schema are ignored; every node is visited at most
        once, so cycles terminate. ``node_id`` itself is included when a loop
        leads back to it without another step in between.
    """

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


def _parallel_interleavings(schema: ProcessSchema) -> set[tuple[str, str]]:
    """Step pairs allowed because they lie in different parallel branches.

    Steps in different branches of the same ``AND_SPLIT`` may complete in any
    order, so every cross-branch pair is allowed in both directions.

    Args:
        schema: The schema to inspect.

    Returns:
        The set of ``(a, b)`` pairs over all ``AND_SPLIT`` blocks; pairs within
        one branch are not added here (sequence covers them). An
        ``AND_SPLIT`` whose block cannot be resolved is skipped.
    """

    allowed: set[tuple[str, str]] = set()
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

