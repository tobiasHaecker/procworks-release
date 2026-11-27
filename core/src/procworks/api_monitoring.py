# SPDX-License-Identifier: BUSL-1.1
"""Monitoring and audit endpoints: audit trail, instance timeline, KPIs,
conformance, process map, the monitoring revision and the collective case and
task reads of the monitoring overview. All read-only.

Shared state (stores, audit log, auth backend, licensing, runtimes) and the
shared helpers live in :mod:`procworks.api` and are read through the module
at call time (``api._store``, ``api._get_or_404(...)``), so tests and
configuration can replace them. Only what FastAPI needs when the route is
declared -- request/response models, role gates and dependencies in the
signatures -- is imported by name. :mod:`procworks.api` includes ``router``
into the app after all of its own definitions, in a fixed order that is
the route order.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from procworks import api, assignment
from procworks.api import (
    MonitoringRevision,
    _read,
    get_principal,
    require_role,
)
from procworks.assignment import OpenTask
from procworks.audit import (
    AuditEvent,
    ConformanceReport,
    KpiReport,
    ProcessMap,
    compute_kpis,
    conformance,
    discover_process_map,
    instance_timeline,
)
from procworks.auth import Principal
from procworks.model import ProcessInstance

router: APIRouter = APIRouter(route_class=api.TransactionalRoute)


@router.get(
    "/instances/{instance_id}/audit",
    response_model=list[AuditEvent],
    dependencies=[_read],
)
def get_instance_audit(
    instance_id: str, principal: Principal = Depends(get_principal)
) -> list[AuditEvent]:
    """History of one instance; a limited operator only for its own.

    Reads only this instance's events (one query), not the whole history.
    """

    api._readable_instance_or_404(instance_id, principal)
    return instance_timeline(api._audit.for_instance(instance_id), instance_id)


@router.get(
    "/audit",
    response_model=list[AuditEvent],
    dependencies=[Depends(require_role("viewer", "modeler", "admin"))],
)
def get_audit(
    schema_id: str | None = None, instance_id: str | None = None
) -> list[AuditEvent]:
    events = api._audit.list_all()
    if schema_id is not None:
        events = [e for e in events if e.schema_id == schema_id]
    if instance_id is not None:
        events = [e for e in events if e.instance_id == instance_id]
    return events


@router.get("/monitoring/kpis", response_model=KpiReport, dependencies=[_read])
def get_kpis(schema_id: str | None = None) -> KpiReport:
    return compute_kpis(api._audit.list_all(), schema_id)


@router.get(
    "/monitoring/unstaffed",
    response_model=list[assignment.UnstaffedStep],
    dependencies=[_read],
)
def get_unstaffed_steps(
    principal: Principal = Depends(get_principal),
) -> list[assignment.UnstaffedStep]:
    """Open human steps across all running instances that nobody may work.

    The monitoring's "Niemand zuständig" figure and filter (reasons
    ``no_rule``/``nobody``/``no_login``) and the weaker "Nur Abwesende
    zuständig" (``only_absent``). Test instances are
    left out (throw-away, not operations). Without it a stalled instance would
    show "overdue 0, escalated 0" and be visible only in its own detail view.
    See :func:`assignment.unstaffed_steps`.
    """

    absent = api._current_absent_agents()
    with_login = api._agents_with_login()
    own = (
        api._involved_instance_ids(principal)
        if api._reads_only_own_instances(principal)
        else None
    )
    # Only running cases can have an open step; one collective read, each
    # schema loaded once (it used to load case and schema per case).
    effective = api._schema_memo()
    found: list[assignment.UnstaffedStep] = []
    for instance in api._instances.list_instances(running_only=True):
        if own is not None and instance.id not in own:
            continue  # a limited operator sees only its own instances
        if instance.is_test:
            continue
        found += assignment.unstaffed_steps(
            effective(instance),
            instance,
            absent_agents=absent,
            agents_with_login=with_login,
        )
    return found


@router.get(
    "/monitoring/instances",
    response_model=list[ProcessInstance],
    dependencies=[_read],
)
def get_monitoring_instances(
    principal: Principal = Depends(get_principal),
) -> list[ProcessInstance]:
    """Every readable case in one answer -- the monitoring's instance list.

    The same cases as ``GET /instances`` (a limited operator only its own,
    test instances only for modellers/administrators), each exactly as
    ``GET /instances/{id}`` returns it. The monitoring used to fetch the list
    and then every case on its own -- one request per case.
    """

    return api._readable_instances(principal)


@router.get(
    "/monitoring/tasks",
    response_model=list[OpenTask],
    dependencies=[_read],
)
def get_monitoring_tasks(
    principal: Principal = Depends(get_principal),
) -> list[OpenTask]:
    """The open tasks of every readable running case in one answer.

    Feeds the monitoring's overdue and escalation figures and its
    escalation panel. Each task is exactly what ``GET /instances/{id}/tasks``
    lists for its case (time band from the one due computation of
    :mod:`procworks.worklist_priority`, fired escalation stage, owner).
    Test instances are left out -- they never count in the monitoring. A
    limited operator gets only the tasks of the cases it is involved in. The
    monitoring used to ask every running case for its tasks separately, and
    each of those reads swept every case for escalations first: its load
    grew with the square of the stock.

    A pure read; due stages are fired by the background timer.
    """

    own = (
        api._involved_instance_ids(principal)
        if api._reads_only_own_instances(principal)
        else None
    )
    return api._open_tasks_of_running_cases(own)


@router.get("/monitoring/process-map", response_model=ProcessMap, dependencies=[_read])
def get_process_map(schema_id: str | None = None) -> ProcessMap:
    return discover_process_map(api._audit.list_all(), schema_id)


@router.get(
    "/schemas/{schema_id}/conformance",
    response_model=ConformanceReport,
    dependencies=[_read],
)
def get_conformance(schema_id: str) -> ConformanceReport:
    """Target/actual comparison: how the recorded history fits the model.

    Read-only (``audit.conformance``): per step how often it completed and how
    long it took, observed transitions the model does not allow, and completed
    steps the model does not contain (ad-hoc).
    """

    return conformance(api._get_or_404(schema_id), api._audit.list_all())


@router.get(
    "/monitoring/revision",
    response_model=MonitoringRevision,
    dependencies=[_read],
)
def get_monitoring_revision() -> MonitoringRevision:
    """Return the current runtime-event revision for cheap client polling.

    The revision is a monotonic counter that increases whenever a runtime event
    (instance/activity progress) is recorded. The web client polls this endpoint
    and re-renders the live views (tasks, monitoring, the running instance) when
    the value changes, so progress made by others appears automatically.
    """

    return MonitoringRevision(revision=api._audit.revision())
