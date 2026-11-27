# SPDX-License-Identifier: BUSL-1.1
"""Execution endpoints: starting and reading instances, worklists, absences
with deputy substitution, and the per-step actions (claim, return, suspend,
resume, fail, reset, start, complete).

Single instances are always loaded through the shared readable-instance
helper, so a caller without access sees 404 for every action alike.

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

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response

from procworks import api, assignment
from procworks import execution as exe
from procworks import operations as ops
from procworks.api import (
    ActivityDetailRequest,
    ClaimActivityRequest,
    CompleteActivityRequest,
    CreateAbsenceRequest,
    DisplayFieldsRequest,
    FailActivityRequest,
    ReturnActivityRequest,
    StartActivityRequest,
    SuspendActivityRequest,
    WorklistReport,
    _model,
    _read,
    get_principal,
    require_role,
)
from procworks.assignment import OpenTask
from procworks.audit import EventType
from procworks.auth import Principal
from procworks.licensing import LicenseError
from procworks.mail_runtime import completion_baseline
from procworks.migration import draft_test_findings
from procworks.model import (
    AbsenceEntry,
    LifecycleState,
    ProcessInstance,
    ProcessSchema,
)
from procworks.validator import completion_value_findings, performer_reference_candidates

router: APIRouter = APIRouter(route_class=api.TransactionalRoute)


@router.get(
    "/schemas/{schema_id}/nodes/{node_id}/performer-candidates",
    response_model=list[str],
    dependencies=[_read],
)
def get_performer_candidates(schema_id: str, node_id: str) -> list[str]:
    """Steps whose performer a rule on ``node_id`` may name (Z3)."""

    schema = api._get_or_404(schema_id)
    if node_id not in schema.nodes:
        raise HTTPException(status_code=404, detail="node not found")
    return performer_reference_candidates(schema, node_id)


@router.post(
    "/schemas/{schema_id}/display-fields",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_display_fields(schema_id: str, req: DisplayFieldsRequest) -> ProcessSchema:
    """Choose up to two data elements that name instances and tasks.

    Validated like every change (U5: existing INSTANCE elements, at most two).
    """

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.set_display_fields(schema, req.element_ids))


@router.get(
    "/instance-titles",
    response_model=dict[str, list[assignment.DisplayValue]],
)
def get_instance_titles(
    principal: Principal = Depends(get_principal),
) -> dict[str, list[assignment.DisplayValue]]:
    """The naming values of every readable instance.

    One call for the monitoring list instead of ``instance_14``. Readability
    follows the instance list (a limited operator only sees its own);
    instances without display fields are left out. The cases come from one
    collective read, each schema is loaded once (it used to load case and
    schema again for every case).
    """

    api._read_gate(principal)
    effective = api._schema_memo()
    titles: dict[str, list[assignment.DisplayValue]] = {}
    for instance in api._readable_instances(principal):
        values = assignment.display_values(effective(instance), instance)
        if values:
            titles[instance.id] = values
    return titles


@router.get("/instances", dependencies=[_read])
def list_instances(principal: Principal = Depends(get_principal)) -> list[str]:
    """All instance ids -- for a limited operator only its own.

    Test instances are listed only for modellers and administrators
    (:func:`procworks.api._may_see_test_instances`), matching who may read
    them; for anybody else the cases are read collectively once to leave
    them out (:func:`procworks.api._readable_instances`) -- no longer one
    store access per case.
    """

    if api._reads_only_own_instances(principal) or not api._may_see_test_instances(principal):
        return [instance.id for instance in api._readable_instances(principal)]
    return api._instances.list_ids()


@router.post(
    "/schemas/{schema_id}/instances",
    response_model=ProcessInstance,
    status_code=201,
)
def post_instantiate(
    schema_id: str, principal: Principal = Depends(get_principal)
) -> ProcessInstance:
    """Start an instance of a schema.

    A RELEASED schema may be instantiated for real by operator/modeler/admin.
    A non-released (draft) schema may only be started as a throw-away *test*
    instance, and only by a modeller/admin -- it is flagged ``is_test``. A test
    instance records *no* audit events for its whole lifecycle (creation, step
    start/complete, ad-hoc changes, completion) and triggers no webhooks or
    external pushes, so it never pollutes the monitoring KPIs or the audit log.
    A draft with an old finding the runtime does not guard against refuses
    the test instance (422 ``LC.test-incorrect``,
    :func:`procworks.migration.draft_test_findings`).
    """

    schema = api._get_or_404(schema_id)
    released = schema.lifecycle_state is LifecycleState.RELEASED
    if released:
        if not principal.roles.intersection({"operator", "modeler", "admin"}):
            raise HTTPException(status_code=403, detail="forbidden")
    elif not principal.roles.intersection({"modeler", "admin"}):
        raise HTTPException(
            status_code=403,
            detail="only modellers/admins may start a test instance of a draft",
        )
    if released:
        # Licensing guard (dormant unless enforced): block *new* instances of a
        # schema that references an expired/uncovered agent. Running instances
        # are never touched. No effect while licensing is off. Test/draft starts
        # (throw-away, no real work) are deliberately exempt.
        try:
            api._license.assert_agents_licensed(api._required_agent_ids(schema))
        except LicenseError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    is_test = not released
    if is_test:
        # A draft may carry old findings (repair path); a test instance runs on
        # the real runtime, so only those the runtime guards against may stay.
        unsafe = draft_test_findings(schema, api._resolver)
        if unsafe:
            raise api._findings_422(unsafe)
    instance = api._run_or_409(
        lambda: exe.instantiate(
            schema, context=api._context, allow_unreleased=not released, is_test=is_test
        )
    )
    if is_test:
        # Test instances stay out of the audit log (and therefore the KPIs).
        return instance
    # Same record as /v1: the starter (agent and login) is named and counts
    # as involved, so an operator reads the cases it started.
    api._record_instance_start(schema, instance, principal)
    return instance


@router.get("/instances/{instance_id}", response_model=ProcessInstance, dependencies=[_read])
def get_instance(
    instance_id: str, principal: Principal = Depends(get_principal)
) -> ProcessInstance:
    return api._readable_instance_or_404(instance_id, principal)


@router.get(
    "/instances/{instance_id}/worklist",
    response_model=WorklistReport,
    dependencies=[_read],
)
def get_worklist(
    instance_id: str, principal: Principal = Depends(get_principal)
) -> WorklistReport:
    instance = api._readable_instance_or_404(instance_id, principal)
    schema = api._effective_schema_for(instance)
    return WorklistReport(
        state=instance.state.value,
        ready_activities=exe.worklist(instance, schema),
        pending_decisions=exe.pending_decisions(instance, schema),
    )


@router.get(
    "/instances/{instance_id}/tasks",
    response_model=list[OpenTask],
    dependencies=[_read],
)
def get_instance_tasks(
    instance_id: str, principal: Principal = Depends(get_principal)
) -> list[OpenTask]:
    # A pure read: due escalation stages are fired by the background timer.
    instance = api._readable_instance_or_404(instance_id, principal)
    schema = api._effective_schema_for(instance)
    return assignment.open_tasks(
        schema,
        instance,
        api._time_context(instance, schema),
        absent_agents=api._current_absent_agents(),
    )


@router.get("/me/tasks", response_model=list[OpenTask])
def get_my_tasks(
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> list[OpenTask]:
    """The worklist of the logged-in agent (the bound principal's own tasks)."""

    if principal.agent_id is None:
        # Open dev mode: no bound agent -> use /agents/{id}/tasks with a picker.
        return []
    return api._tasks_for_agent(principal.agent_id)


@router.get("/agents/{agent_id}/tasks", response_model=list[OpenTask])
def get_agent_tasks(
    agent_id: str,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> list[OpenTask]:
    api._require_agent_self_or_supervisor(principal, agent_id)
    return api._tasks_for_agent(agent_id)


@router.get("/agents/{agent_id}/absences", response_model=list[AbsenceEntry])
def get_agent_absences(
    agent_id: str,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> list[AbsenceEntry]:
    """List an agent's absence windows (self, or any agent for admin/modeler)."""

    api._require_agent_self_or_supervisor(principal, agent_id)
    return api._list_absences(agent_id)


@router.get("/me/absences", response_model=list[AbsenceEntry])
def get_my_absences(
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> list[AbsenceEntry]:
    """The logged-in agent's own absence windows (empty when no bound agent)."""

    if principal.agent_id is None:
        return []
    return api._list_absences(principal.agent_id)


@router.post("/agents/{agent_id}/absences", response_model=AbsenceEntry, status_code=201)
def post_agent_absence(
    agent_id: str,
    req: CreateAbsenceRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> AbsenceEntry:
    """Record an absence window for an agent (deputy stands in during it).

    Self-service or supervisory. The window must be non-empty (``end_at >=
    start_at``) and the agent must be known. The absence never removes the agent
    from any worklist -- it only adds the deputy in parallel -- so it cannot stall
    an instance even if no deputy is registered.

    Both bounds must carry a time zone (``Z`` or an offset such as
    ``+02:00``): a bound without one is refused with 422, ``detail.code``
    ``ABS.no-time-zone`` and ``params.field`` (the first such bound). Read
    silently as UTC, it shifted the window by the user's offset; compared with
    an aware bound, it even raised. The offset given is kept as sent.
    """

    api._require_agent_self_or_supervisor(principal, agent_id)
    if agent_id not in api._known_agent_ids():
        raise HTTPException(status_code=404, detail=f"unknown agent '{agent_id}'")
    for field, moment in (("start_at", req.start_at), ("end_at", req.end_at)):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        f"{field} needs a time zone (e.g. 'Z' or '+02:00'); "
                        "a time without one is ambiguous"
                    ),
                    "code": "ABS.no-time-zone",
                    "params": {"field": field},
                },
            )
    if req.end_at < req.start_at:
        raise HTTPException(
            status_code=422, detail="end_at must not be before start_at"
        )
    entry = AbsenceEntry(
        id=f"abs_{uuid.uuid4().hex}",
        agent_id=agent_id,
        start_at=req.start_at,
        end_at=req.end_at,
        note=req.note,
    )
    return api._absence_store.put_entry(entry)


@router.delete("/agents/{agent_id}/absences/{absence_id}", status_code=204)
def delete_agent_absence(
    agent_id: str,
    absence_id: str,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> Response:
    """Remove an absence window (self, or any agent for admin/modeler)."""

    api._require_agent_self_or_supervisor(principal, agent_id)
    entry = api._absence_store.get_entry(absence_id)
    if entry is None or entry.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="absence not found")
    api._absence_store.delete_entry(absence_id)
    return Response(status_code=204)


@router.post("/instances/{instance_id}/claim", response_model=ProcessInstance)
def post_claim_activity(
    instance_id: str,
    req: ClaimActivityRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    """Claim an offered task for one agent (E1, worklist state machine).

    On success the step leaves every other eligible agent's personal list
    (Withdrawn view) until it is returned or completed. Conflicts (already
    claimed, not eligible, not activated) come back as HTTP 409 -- the
    instance is untouched. The claim instant is stamped at this boundary
    (the engine stays clock-free), idempotently for a re-claim by the owner.
    """

    instance = api._readable_instance_or_404(instance_id, principal)
    schema = api._effective_schema_for(instance)
    acting = api._require_acting_agent(principal, req.agent_id, instance)

    def _claim_and_stamp() -> ProcessInstance:
        after = exe.claim_activity(
            instance,
            schema,
            req.node_id,
            acting,
            absent_agents=api._current_absent_agents(),
        )
        after.node_claimed_at.setdefault(req.node_id, datetime.now(UTC))
        return after

    after = api._run_or_409(_claim_and_stamp)
    api._detail_audit(
        EventType.ACTIVITY_CLAIMED,
        instance,
        after,
        schema,
        req.node_id,
        acting,
        principal=principal,
    )
    return after


@router.post("/instances/{instance_id}/return", response_model=ProcessInstance)
def post_return_activity(
    instance_id: str,
    req: ReturnActivityRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    """Return a claimed task to the open offer (E1, W3).

    The owner may always return their own claim; for someone else's claim the
    same supervisory authority applies as for the worklist/absence endpoints
    (admin/modeler roles, or an unbound machine identity / open dev mode --
    never a personal login without an agent binding). A RUNNING step falls back to
    ACTIVATED, and the task reappears in every eligible agent's list.
    """

    instance = api._readable_instance_or_404(instance_id, principal)
    schema = api._effective_schema_for(instance)
    acting = api._resolve_acting_agent(principal, req.agent_id, instance)
    force = api._force_foreign_claim(principal, instance, req.node_id, acting)
    after = api._run_or_409(
        lambda: exe.return_activity(
            instance, schema, req.node_id, acting or "", force=force
        )
    )
    api._detail_audit(
        EventType.ACTIVITY_RETURNED,
        instance,
        after,
        schema,
        req.node_id,
        acting,
        principal=principal,
    )
    return after


@router.post("/instances/{instance_id}/suspend", response_model=ProcessInstance)
def post_suspend_activity(
    instance_id: str,
    req: SuspendActivityRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    """Pause a started activity (E2, V1) -- owner-only, base marking stays.

    The pause is transparency, not a deadline stop: clocks and escalation
    keep running, so a pause can never quietly defer a deadline. A visible
    reason is required (422 ``REQ.reason-required``, ``what`` ``suspend``,
    :func:`procworks.api._require_suspend_reason`); it is checked after the
    read check (a foreign case still answers 404) and before the engine runs,
    and recorded at the ``ACTIVITY_SUSPENDED`` event. An idempotent
    re-suspend records its reason again. A step that was only offered or
    claimed is claimed and started on the way; those transitions are
    recorded first as ``ACTIVITY_CLAIMED``/``ACTIVITY_STARTED`` with
    ``{"implicit": "suspend"}`` (:func:`procworks.api._implicit_start_audit`).
    """

    instance = api._readable_instance_or_404(instance_id, principal)
    reason = api._require_suspend_reason(req.reason)
    schema = api._effective_schema_for(instance)
    acting = api._require_acting_agent(principal, req.agent_id, instance)

    def _suspend_and_stamp() -> ProcessInstance:
        after = exe.suspend_activity(
            instance,
            schema,
            req.node_id,
            acting,
            absent_agents=api._current_absent_agents(),
        )
        # Net-time bookkeeping (E2 Stufe C): stamp the pause start at this
        # boundary (the engine stays clock-free). ``setdefault`` keeps an
        # idempotent re-suspend from restarting the ongoing pause.
        after.node_suspended_at.setdefault(req.node_id, datetime.now(UTC))
        # An offered step was claimed on the way: stamp the claim instant as
        # the explicit claim/start does (kept when it was claimed before).
        after.node_claimed_at.setdefault(req.node_id, datetime.now(UTC))
        return after

    after = api._run_or_409(_suspend_and_stamp)
    api._implicit_start_audit(
        "suspend", instance, after, schema, req.node_id, acting, principal=principal
    )
    api._detail_audit(
        EventType.ACTIVITY_SUSPENDED,
        instance,
        after,
        schema,
        req.node_id,
        acting,
        detail={"reason": reason},
        principal=principal,
    )
    return after


@router.post("/instances/{instance_id}/resume", response_model=ProcessInstance)
def post_resume_activity(
    instance_id: str,
    req: ActivityDetailRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    """Continue a suspended activity (E2, V2) -- owner-only."""

    instance = api._readable_instance_or_404(instance_id, principal)
    schema = api._effective_schema_for(instance)
    acting = api._require_acting_agent(principal, req.agent_id, instance)

    def _resume_and_book() -> ProcessInstance:
        after = exe.resume_activity(instance, schema, req.node_id, acting)
        # Net-time bookkeeping (E2 Stufe C): close the pause interval opened
        # by suspend -- the elapsed span moves into the accumulated credit.
        # Booked unconditionally (cheap, additive); whether it *counts* is
        # decided at read time by the constraint's ``pause_stops_clock``.
        opened = after.node_suspended_at.pop(req.node_id, None)
        if opened is not None:
            span = (datetime.now(UTC) - opened).total_seconds()
            if span > 0:
                after.node_paused_seconds[req.node_id] = (
                    after.node_paused_seconds.get(req.node_id, 0.0) + span
                )
        return after

    after = api._run_or_409(_resume_and_book)
    api._detail_audit(
        EventType.ACTIVITY_RESUMED,
        instance,
        after,
        schema,
        req.node_id,
        acting,
        principal=principal,
    )
    return after


@router.post("/instances/{instance_id}/fail", response_model=ProcessInstance)
def post_fail_activity(
    instance_id: str,
    req: FailActivityRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    """Report a started activity as failed (E2, V3) -- with a reason.

    The step freezes (not completable, not workable) and waits for its
    recovery reset -- the instance is never in an undefined state. A reason
    without a visible character is refused with 422 before anything changes
    (:func:`procworks.api._require_fail_reason`). An implicit claim/start is
    recorded before ``ACTIVITY_FAILED`` (``{"implicit": "fail"}``, as for
    suspend).
    """

    instance = api._readable_instance_or_404(instance_id, principal)
    reason = api._require_fail_reason(req.reason)
    schema = api._effective_schema_for(instance)
    acting = api._require_acting_agent(principal, req.agent_id, instance)

    def _fail_and_stamp() -> ProcessInstance:
        after = exe.fail_activity(
            instance,
            schema,
            req.node_id,
            acting,
            reason,
            absent_agents=api._current_absent_agents(),
        )
        # Like suspend: an implicit claim gets its claim instant.
        after.node_claimed_at.setdefault(req.node_id, datetime.now(UTC))
        return after

    after = api._run_or_409(_fail_and_stamp)
    api._implicit_start_audit(
        "fail", instance, after, schema, req.node_id, acting, principal=principal
    )
    api._detail_audit(
        EventType.ACTIVITY_FAILED,
        instance,
        after,
        schema,
        req.node_id,
        acting,
        detail={"reason": reason},
        principal=principal,
    )
    return after


@router.post("/instances/{instance_id}/reset", response_model=ProcessInstance)
def post_reset_activity(
    instance_id: str,
    req: ActivityDetailRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    """Recover a failed activity (E2, V4): fresh offer, fresh clocks.

    The owner may reset their own failure; for someone else's the same
    supervisory authority applies as for the E1 return. The activation clock
    is re-stamped at this boundary, so the second attempt gets a fresh
    deadline and a fresh escalation ladder.
    """

    instance = api._readable_instance_or_404(instance_id, principal)
    schema = api._effective_schema_for(instance)
    acting = api._resolve_acting_agent(principal, req.agent_id, instance)
    force = api._force_foreign_claim(principal, instance, req.node_id, acting)

    def _reset_and_stamp() -> ProcessInstance:
        after = exe.reset_activity(
            instance, schema, req.node_id, acting or "", force=force
        )
        after.node_activated_at[req.node_id] = datetime.now(UTC)
        return after

    after = api._run_or_409(_reset_and_stamp)
    api._detail_audit(
        EventType.ACTIVITY_RESET,
        instance,
        after,
        schema,
        req.node_id,
        acting,
        principal=principal,
    )
    return after


@router.post("/instances/{instance_id}/start", response_model=ProcessInstance)
def post_start_activity(
    instance_id: str,
    req: StartActivityRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    """Move an activity into RUNNING ("Started", E1).

    Starting presupposes ownership (W4): an unclaimed step is claimed
    implicitly for the acting agent, a step claimed by someone else is a 409.
    """

    instance = api._readable_instance_or_404(instance_id, principal)
    schema = api._effective_schema_for(instance)
    acting = api._require_acting_agent(principal, req.agent_id, instance)

    def _start_and_stamp() -> ProcessInstance:
        after = exe.start_activity(
            instance,
            schema,
            req.node_id,
            acting,
            absent_agents=api._current_absent_agents(),
        )
        after.node_claimed_at.setdefault(req.node_id, datetime.now(UTC))
        return after

    after = api._run_or_409(_start_and_stamp)
    api._detail_audit(
        EventType.ACTIVITY_STARTED,
        instance,
        after,
        schema,
        req.node_id,
        acting,
        principal=principal,
    )
    return after


@router.post("/instances/{instance_id}/complete", response_model=ProcessInstance)
def post_complete_activity(
    instance_id: str,
    req: CompleteActivityRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    before = api._readable_instance_or_404(instance_id, principal)
    schema = api._effective_schema_for(before)
    before_states = dict(before.node_states)
    acting_agent = api._resolve_acting_agent(principal, req.agent_id, before)
    # A step an outside system works (external task, push) is finished by hand
    # only as supervision with a reason; checked first, so an operator gets the
    # same 409 as for claiming it.
    automatic_reason = api._require_automatic_supervision(
        principal, before, schema, req.node_id, req.supervision_reason, acting_agent
    )
    supervision = automatic_reason or api._require_supervision_reason(
        principal, acting_agent, before, schema, req.node_id, req.supervision_reason
    )
    # D6: a step's completion may only set what the step writes. Before, any
    # key was taken over -- an approval step that only READS the amount could
    # overwrite it (four-eyes manipulation, no audit), and unknown keys piled up
    # as junk in the instance data. The external-task path has refused
    # non-writable outputs all along; interactive completion now does the same.
    unwritable = api._unwritable_completion_keys(schema, req.node_id, req.data)
    if unwritable:
        raise api._findings_422(unwritable)
    # D3 at runtime: a completed step's values must fit their element's type.
    # Otherwise "vielleicht" would land in a BOOLEAN element, and an XOR
    # decision on it would silently take the "true" branch.
    # Only wrong types of *known* elements are refused here; unknown keys keep
    # their previous behaviour so integrations sending extra fields do not break.
    type_findings = [
        f for f in api._validate_data_values(schema, req.data) if f.code == "D3.wrong-type"
    ]
    if type_findings:
        raise api._findings_422(type_findings)
    # D1 at runtime, then U4: every mandatory write of the step is supplied --
    # sent now or stored (once the step itself ran before, in a loop: set via
    # PUT …/data during this activation, never a value of an earlier round)
    # -- and the value the step ends up with passes the step's mask. For every
    # caller (web, /v1, supervision): supervision overrides the staff rule,
    # not the data rules.
    value_findings = completion_value_findings(schema, req.node_id, before, req.data)
    if value_findings:
        raise api._findings_422(value_findings)
    after = api._run_or_409(
        lambda: exe.complete_activity(
            before,
            schema,
            req.node_id,
            req.data,
            agent_id=acting_agent,
            context=api._context,
            absent_agents=api._current_absent_agents(),
        )
    )
    if api._is_externally_driven(schema, req.node_id):
        # The step is done: its task must not stay open for a worker (it would
        # be handed out again once its lock runs out). Before the advance below
        # materialises a task for a loop's next round.
        api._external_runtime().close_step(
            after.id, req.node_id, announce=not before.is_test
        )
    if not before.is_test:
        # A throw-away test instance stays out of the audit log / KPIs and
        # triggers no external side effects (mirrors instance creation).
        # The record is shared with the external-task/push completion.
        api._record_step_completion(
            before,
            after,
            schema,
            req.node_id,
            agent_id=acting_agent,
            detail=api._completion_detail(before, req.node_id, principal, acting_agent),
            supervision=(
                {"actor": principal.subject, "reason": supervision}
                if supervision is not None
                else None
            ),
        )
        # The completed step passed through COMPLETED even when a loop reset
        # has already re-offered it (ACTIVATED before and after): the baseline
        # says so, so every round is stamped and notified afresh.
        api._after_advance(
            schema, completion_baseline(before_states, req.node_id), after
        )
    elif after.unaudited_decisions:
        # A test instance records no decisions either; empty the hand-over so
        # it does not pile up in the stored document.
        api._record_branch_decisions(after, schema)
        api._instances.put(after)
    return after
