# SPDX-License-Identifier: BUSL-1.1
"""Versioned integration API (``/v1``): inbound control by external tools,
external tasks, incidents, push endpoints, data connectors and webhooks.

The endpoints mirror the runtime endpoints under a stable ``/v1`` prefix
and gate them with integration scopes; they add no domain logic of their
own. ``instance_data_router`` carries the unversioned
``PUT /instances/{instance_id}/data``, which shares its write path with
the ``/v1`` variant and is included right before ``router``.

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

from fastapi import APIRouter, Depends, Header, HTTPException

from procworks import api, api_execution, assignment
from procworks import execution as exe
from procworks.api import (
    BpmnErrorRequest,
    ColumnInfo,
    CompleteActivityRequest,
    CompleteTaskRequest,
    ConnectorInfo,
    ConnectorTestResult,
    ExtendLockRequest,
    FailureRequest,
    FetchAndLockRequest,
    SampleReadRequest,
    SetDataRequest,
    V1CompleteRequest,
    WebhookCreateRequest,
    WebhookPreviewRequest,
    WebhookPreviewResponse,
    WorkerRequest,
    require_role,
    require_scope,
)
from procworks.assignment import OpenTask
from procworks.audit import EventType
from procworks.auth import (
    SCOPE_DATA_READ,
    SCOPE_DATA_WRITE,
    SCOPE_EVENTS_SUBSCRIBE,
    SCOPE_INSTANCES_START,
    SCOPE_TASKS_COMPLETE,
    SCOPE_TASKS_FETCH,
    Principal,
)
from procworks.dal import DataAccessError, UnsafeIdentifierError
from procworks.licensing import LicenseError
from procworks.model import (
    ExternalTask,
    Incident,
    InstanceState,
    LifecycleState,
    ProcessInstance,
    WebhookDelivery,
    WebhookSubscription,
)
from procworks.outbox import WEBHOOK_EVENTS, preview_delivery

router: APIRouter = APIRouter(prefix="/v1", tags=["integration"])
instance_data_router: APIRouter = APIRouter()


# --- versioned integration API (/v1) — inbound control by external tools -
#
# These endpoints mirror the existing runtime endpoints under a stable,
# versioned ``/v1`` prefix and gate them with integration *scopes* (so a service
# token is confined to least privilege), while remaining fully usable by human/
# open principals via their roles. Mutating calls honour an ``Idempotency-Key``
# header. The endpoints add no new domain logic: they reuse the same
# validate-before-commit core path as the GUI (Section 5.4, API-first).


@instance_data_router.put(
    "/instances/{instance_id}/data",
    response_model=dict[str, object],
)
def put_instance_data(
    instance_id: str,
    req: SetDataRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> dict[str, object]:
    """Set process variable values directly on an instance (type-checked, D3).

    Lets a caller enter instance data outside an activity completion -- e.g.
    right after the start. Unknown elements or type mismatches are rejected
    with 422 (D3). Who may write what is decided by
    :func:`_authorize_data_write`: a bound operator only the elements
    of its own open steps, modeller/admin anything else only with a reason.
    Every change is audited as ``INSTANCE_DATA_SET``; that event type is not
    part of any KPI or mining aggregation, so the figures stay unchanged.
    """

    return api._set_instance_data(instance_id, req, principal)


@router.post(
    "/schemas/{schema_id}/instances",
    response_model=ProcessInstance,
    status_code=201,
)
def v1_start_instance(
    schema_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(
        require_scope(SCOPE_INSTANCES_START, "operator", "modeler", "admin")
    ),
) -> ProcessInstance:
    """Start an instance of a RELEASED schema (integration entry point).

    Unlike the legacy endpoint, this never starts a throw-away *test* instance
    of a draft: a service may only run released processes (409 otherwise).
    """

    def produce() -> ProcessInstance:
        schema = api._get_or_404(schema_id)
        if schema.lifecycle_state is not LifecycleState.RELEASED:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "only released schemas can be instantiated via /v1"
                },
            )
        # Licensing guard (dormant unless enforced): mirror the GUI entry point.
        try:
            api._license.assert_agents_licensed(api._required_agent_ids(schema))
        except LicenseError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        instance = api._run_or_409(lambda: exe.instantiate(schema, context=api._context))
        api._audit.append(
            EventType.INSTANCE_CREATED,
            instance.id,
            instance.schema_id,
            schema_version=instance.schema_version,
        )
        api._emit_event("instance.started", api._instance_event_payload(instance))
        if instance.state is InstanceState.COMPLETED:
            api._audit.append(
                EventType.INSTANCE_COMPLETED,
                instance.id,
                instance.schema_id,
                schema_version=instance.schema_version,
            )
            api._emit_event("instance.completed", api._instance_event_payload(instance))
        api._after_advance(schema, None, instance)
        return instance

    result = api._idempotent(principal, idempotency_key, produce)
    assert isinstance(result, ProcessInstance)
    return result


@router.get("/instances/{instance_id}", response_model=ProcessInstance)
def v1_get_instance(
    instance_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> ProcessInstance:
    return api._readable_instance_or_404(instance_id, principal)


@router.get("/instances/{instance_id}/tasks", response_model=list[OpenTask])
def v1_get_instance_tasks(
    instance_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> list[OpenTask]:
    instance = api._readable_instance_or_404(instance_id, principal)
    schema = api._effective_schema_for(instance)
    return assignment.open_tasks(
        schema, instance, absent_agents=api._current_absent_agents()
    )


@router.post(
    "/instances/{instance_id}/nodes/{node_id}/complete",
    response_model=ProcessInstance,
)
def v1_complete_task(
    instance_id: str,
    node_id: str,
    req: V1CompleteRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ProcessInstance:
    """Complete a task and hand over its data (mirror of ``/complete``)."""

    body = req or V1CompleteRequest()

    def produce() -> ProcessInstance:
        completion = CompleteActivityRequest(
            node_id=node_id,
            data=body.data,
            agent_id=body.agent_id,
            supervision_reason=body.supervision_reason,
        )
        return api_execution.post_complete_activity(instance_id, completion, principal)

    result = api._idempotent(principal, idempotency_key, produce)
    assert isinstance(result, ProcessInstance)
    return result


@router.get("/instances/{instance_id}/data", response_model=dict[str, object])
def v1_get_instance_data(
    instance_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> dict[str, object]:
    """Read all process variable values of an instance."""

    return dict(api._readable_instance_or_404(instance_id, principal).data_values)


@router.put("/instances/{instance_id}/data", response_model=dict[str, object])
def v1_put_instance_data(
    instance_id: str,
    req: SetDataRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_WRITE, "operator", "modeler", "admin")
    ),
) -> dict[str, object]:
    """Set process variable values, type-checked against the schema (D3).

    Same rules and audit as the internal endpoint (:func:`_set_instance_data`);
    a service token keeps its ``data:write`` path, and the event records it as
    the sender.
    """

    def produce() -> dict[str, object]:
        return api._set_instance_data(instance_id, req, principal)

    result = api._idempotent(principal, idempotency_key, produce)
    assert isinstance(result, dict)
    return result


@router.post("/external-tasks/fetch-and-lock", response_model=list[ExternalTask])
def v1_fetch_and_lock(
    req: FetchAndLockRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "operator", "modeler", "admin")
    ),
) -> list[ExternalTask]:
    """Claim automatic external-task work for the given topics (outbound pull)."""

    result = api._run_external(
        lambda: api._external_runtime().fetch_and_lock(
            req.worker_id,
            req.topics,
            lock_ms=req.lock_ms,
            max_tasks=req.max_tasks,
            use_priority=req.use_priority,
        )
    )
    assert isinstance(result, list)
    return result


@router.get("/external-tasks/{task_id}", response_model=ExternalTask)
def v1_get_external_task(
    task_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "viewer", "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    task = api._external_runtime().get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404, detail={"message": f"external task '{task_id}' not found"}
        )
    return task


@router.post("/external-tasks/{task_id}/complete", response_model=ExternalTask)
def v1_complete_external_task(
    task_id: str,
    req: CompleteTaskRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Report success: write outputs and advance the instance (exactly-once)."""

    def produce() -> ExternalTask:
        result = api._run_external(
            lambda: api._external_runtime().complete(
                task_id, req.worker_id, req.variables
            )
        )
        assert isinstance(result, ExternalTask)
        api._drive_pushes()
        return result

    outcome = api._idempotent(principal, idempotency_key, produce)
    assert isinstance(outcome, ExternalTask)
    return outcome


@router.post("/external-tasks/{task_id}/failure", response_model=ExternalTask)
def v1_fail_external_task(
    task_id: str,
    req: FailureRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Report a technical failure: re-queue with back-off or raise an incident."""

    result = api._run_external(
        lambda: api._external_runtime().failure(
            task_id,
            req.worker_id,
            req.error_message,
            retries=req.retries,
            retry_timeout_ms=req.retry_timeout_ms,
        )
    )
    assert isinstance(result, ExternalTask)
    return result


@router.post("/external-tasks/{task_id}/bpmn-error", response_model=ExternalTask)
def v1_bpmn_error_external_task(
    task_id: str,
    req: BpmnErrorRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Report a business (BPMN) error for the locked task."""

    result = api._run_external(
        lambda: api._external_runtime().bpmn_error(
            task_id, req.worker_id, req.error_code
        )
    )
    assert isinstance(result, ExternalTask)
    return result


@router.post("/external-tasks/{task_id}/extend-lock", response_model=ExternalTask)
def v1_extend_lock_external_task(
    task_id: str,
    req: ExtendLockRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Prolong the lock on a long-running task."""

    result = api._run_external(
        lambda: api._external_runtime().extend_lock(task_id, req.worker_id, req.lock_ms)
    )
    assert isinstance(result, ExternalTask)
    return result


@router.post("/external-tasks/{task_id}/unlock", response_model=ExternalTask)
def v1_unlock_external_task(
    task_id: str,
    req: WorkerRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Release the lock, returning the task to the queue immediately."""

    result = api._run_external(
        lambda: api._external_runtime().unlock(task_id, req.worker_id)
    )
    assert isinstance(result, ExternalTask)
    return result


@router.get("/incidents", response_model=list[Incident])
def v1_list_incidents(
    unresolved_only: bool = False,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "viewer", "operator", "modeler", "admin")
    ),
) -> list[Incident]:
    """List external-task incidents (optionally only the unresolved ones).

    A limited operator (see :func:`_reads_only_own_instances`) sees only the
    incidents of instances it is involved in, like every other instance view.
    """

    incidents = api._external_runtime().list_incidents(unresolved_only=unresolved_only)
    if not api._reads_only_own_instances(principal):
        return incidents
    visible = []
    for incident in incidents:
        instance = api._instances.get(incident.instance_id)
        if instance is not None and api._is_involved(principal, instance):
            visible.append(incident)
    return visible


@router.post("/incidents/{incident_id}/resolve", response_model=Incident)
def v1_resolve_incident(
    incident_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "admin")
    ),
) -> Incident:
    """Resolve an incident and re-queue its task for another attempt.

    A limited operator may resolve only incidents of instances it can read;
    any other is 404 like a missing one (the involvement rule).
    """

    if api._reads_only_own_instances(principal):
        known = {i.id: i for i in api._external_runtime().list_incidents(unresolved_only=False)}
        incident = known.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        api._readable_instance_or_404(incident.instance_id, principal)
    result = api._run_external(
        lambda: api._external_runtime().resolve_incident(incident_id)
    )
    assert isinstance(result, Incident)
    return result


@router.get("/push-endpoints", response_model=list[str])
def v1_list_push_endpoints(
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "viewer", "operator", "modeler", "admin")
    ),
) -> list[str]:
    """List configured ``HTTP_PUSH`` endpoint references (never URLs/secrets).

    A modeller binds an automatic step to one of these references; the concrete
    URL and signing secret stay server-side (``PROCWORKS_PUSH_ENDPOINTS``).
    """

    return api._push_endpoints.refs()


@router.post("/external-tasks/drive-push", response_model=list[ExternalTask])
def v1_drive_push(
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "operator", "admin")
    ),
) -> list[ExternalTask]:
    """Push activated ``HTTP_PUSH`` steps now (e.g. re-push after a back-off).

    Pushes happen automatically after every advance; this endpoint lets an
    operator force a drive so tasks waiting out a failure back-off are re-pushed
    without waiting for the next process event. Idempotent and side-effect-safe.
    """

    return api._external_runtime().drive_push()


@router.get("/connectors", response_model=list[ConnectorInfo])
def v1_list_connectors(
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> list[ConnectorInfo]:
    """List configured data connectors (metadata only -- never secrets)."""

    return [
        ConnectorInfo(connector_id=cfg.connector_id, kind=cfg.kind)
        for cfg in api._connections.configs()
    ]


@router.post("/connectors/{connector_id}/test", response_model=ConnectorTestResult)
def v1_test_connector(
    connector_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "operator", "modeler", "admin")
    ),
) -> ConnectorTestResult:
    """Run a read-only connection check without revealing any secret."""

    api._require_connector(connector_id)
    try:
        api._connections.test(connector_id)
    except DataAccessError as err:
        raise HTTPException(status_code=502, detail={"message": str(err)}) from err
    return ConnectorTestResult(connector_id=connector_id, ok=True)


@router.post("/connectors/{connector_id}/sample-read", response_model=list[dict[str, object]])
def v1_sample_read_connector(
    connector_id: str,
    req: SampleReadRequest,
    principal: Principal = Depends(require_scope(SCOPE_DATA_READ, "modeler", "admin")),
) -> list[dict[str, object]]:
    """Return a few sample records of an entity for GUI mapping help.

    A modelling aid, so only modeller/admin (and service tokens with
    ``data:read``) may call it -- an operator has no mapping to build and
    must not browse external tables. The entity must pass
    :func:`_check_sample_entity`. Status codes: an unsafe or unknown entity
    is the caller's error (422), a failing external system is 502.
    """

    api._require_connector(connector_id)
    api._check_sample_entity(connector_id, req.entity)
    try:
        rows = api._connections.sample_read(connector_id, req.entity, limit=req.limit)
    except UnsafeIdentifierError as err:
        raise HTTPException(status_code=422, detail={"message": str(err)}) from err
    except DataAccessError as err:
        raise HTTPException(status_code=502, detail={"message": str(err)}) from err
    return [dict(row) for row in rows]


@router.get("/connectors/{connector_id}/entities", response_model=list[str])
def v1_connector_entities(
    connector_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> list[str]:
    """List the entities (tables/views) a connector exposes, for the GUI offer.

    Pure catalogue metadata -- no row is read and no secret is revealed. The
    sample read and the select builder use it so a modeller picks a table
    instead of guessing its name. A connector that cannot introspect its
    catalogue yields an empty list (the manual entry keeps working).
    """

    api._require_connector(connector_id)
    try:
        return api._connections.entities(connector_id)
    except DataAccessError as err:
        raise HTTPException(status_code=502, detail={"message": str(err)}) from err


@router.get("/connectors/{connector_id}/columns", response_model=list[ColumnInfo])
def v1_connector_columns(
    connector_id: str,
    entity: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> list[ColumnInfo]:
    """Reflect an entity's columns + mapped data types for the GUI assistant."""

    api._require_connector(connector_id)
    try:
        columns = api._connections.columns(connector_id, entity)
    except DataAccessError as err:
        raise HTTPException(status_code=502, detail={"message": str(err)}) from err
    return [ColumnInfo.model_validate(column) for column in columns]


@router.get("/webhooks", response_model=list[WebhookSubscription])
def v1_list_webhooks(
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> list[WebhookSubscription]:
    """List webhook subscriptions (the secret itself is never returned)."""

    return api._outbox.list_subscriptions()


@router.post("/webhooks", response_model=WebhookSubscription, status_code=201)
def v1_create_webhook(
    req: WebhookCreateRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> WebhookSubscription:
    """Register a webhook subscription (validates events and the SSRF policy)."""

    result = api._run_webhook(
        lambda: api._outbox.subscribe(req.url, req.events, req.secret_ref)
    )
    assert isinstance(result, WebhookSubscription)
    return result


@router.post("/webhooks/preview", response_model=WebhookPreviewResponse)
def v1_preview_webhook(
    req: WebhookPreviewRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> WebhookPreviewResponse:
    """Show what a delivery to ``url`` would send and whether the SSRF rule allows it.

    Nothing is sent and nothing is stored -- a diagnostic that also works where
    outbound traffic is locked (the public demo). Same rights as creating a
    subscription, because it reveals how a target name resolves.
    """

    if req.event not in WEBHOOK_EVENTS and req.event != "webhook.test":
        raise HTTPException(status_code=422, detail=f"unknown webhook event '{req.event}'")
    p = preview_delivery(req.url, req.event, req.secret_ref)
    return WebhookPreviewResponse(**p.__dict__)


@router.delete("/webhooks/{subscription_id}", status_code=204)
def v1_delete_webhook(
    subscription_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> None:
    """Remove a webhook subscription."""

    api._run_webhook(lambda: api._outbox.unsubscribe(subscription_id))


@router.post("/webhooks/{subscription_id}/test", response_model=WebhookDelivery)
def v1_test_webhook(
    subscription_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> WebhookDelivery:
    """Send a synthetic ping to one subscription and return the attempt result."""

    result = api._run_webhook(lambda: api._outbox.test_delivery(subscription_id))
    assert isinstance(result, WebhookDelivery)
    return result


@router.get(
    "/webhooks/{subscription_id}/deliveries", response_model=list[WebhookDelivery]
)
def v1_webhook_deliveries(
    subscription_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> list[WebhookDelivery]:
    """Return the delivery log of one subscription (audit / debugging)."""

    api._run_webhook(
        lambda: api._outbox.get_subscription(subscription_id)
        or api._raise_webhook_404(subscription_id)
    )
    return api._outbox.deliveries(subscription_id)
