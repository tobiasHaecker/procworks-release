# SPDX-License-Identifier: BUSL-1.1
"""Change endpoints for running work: ad-hoc changes of a single instance,
new schema revisions with instance migration, and the bulk migration
assistant.

Every change is validated before it is stored; ad-hoc changes require a
reason and are recorded in the audit log.

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

from fastapi import APIRouter, Depends, HTTPException

from procworks import adhoc, api, migration
from procworks import operations as ops
from procworks.api import (
    AdhocDeleteRequest,
    AdhocInsertRequest,
    AdhocRenameRequest,
    BulkMigrateReport,
    BulkMigrateRequest,
    BulkMigrateResult,
    MigrateRequest,
    MigrationAssistantReport,
    MigrationCandidate,
    MigrationReport,
    MigrationTarget,
    RevisionRequest,
    _model,
    _read,
    _run,
    get_principal,
    require_role,
)
from procworks.audit import EventType
from procworks.auth import Principal
from procworks.model import ProcessInstance, ProcessSchema
from procworks.validator import ValidationFinding

router: APIRouter = APIRouter()


@router.post(
    "/instances/{instance_id}/adhoc/insert",
    response_model=ProcessInstance,
)
def post_adhoc_insert(
    instance_id: str,
    req: AdhocInsertRequest,
    principal: Principal = Depends(require_role("modeler", "admin")),
) -> ProcessInstance:
    """Insert a serial step into one running instance (R1/R2, plus B2).

    ``staff_rule`` is required by the core for the new step (422 with
    ``B2.no-staff`` otherwise) -- a step without one would stand in nobody's
    worklist. Only modeller/admin may change a running case, and a
    real instance needs a ``reason``; both travel into ``ADHOC_INSERTED``
    together with the actor (see :func:`_adhoc_audit_detail`).
    """

    instance = api._get_instance_or_404(instance_id)
    detail = api._adhoc_audit_detail(instance, principal, req.reason)
    schema = api._effective_schema_for(instance)
    before_states = dict(instance.node_states)
    after = api._commit_instance_or_422(
        lambda: adhoc.adhoc_insert_activity(
            instance,
            schema,
            req.after_node_id,
            req.label,
            resolver=api._resolver,
            staff_rule=req.staff_rule,
        )
    )
    if not instance.is_test:
        # Test instances record no audit events (see instance creation).
        detail = {"label": req.label, **detail}
        api._audit.append(
            EventType.ADHOC_INSERTED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
            node_id=req.after_node_id,
            label=api._label_of(schema, req.after_node_id),
            detail=detail,
        )
        # An ad-hoc insert can make a mail-bound activity ready -> notify.
        api._after_advance(api._effective_schema_for(after), before_states, after)
    return after


@router.post(
    "/instances/{instance_id}/adhoc/delete",
    response_model=ProcessInstance,
)
def post_adhoc_delete(
    instance_id: str,
    req: AdhocDeleteRequest,
    principal: Principal = Depends(require_role("modeler", "admin")),
) -> ProcessInstance:
    """Remove a not yet reached serial step from one running instance (R1/R2).

    Modeller/admin only, with a reason for a real instance; the actor and the
    reason are recorded in ``ADHOC_DELETED``.
    """

    instance = api._get_instance_or_404(instance_id)
    detail = api._adhoc_audit_detail(instance, principal, req.reason)
    schema = api._effective_schema_for(instance)
    before_states = dict(instance.node_states)
    after = api._commit_instance_or_422(
        lambda: adhoc.adhoc_delete_node(
            instance, schema, req.node_id, resolver=api._resolver
        )
    )
    if not instance.is_test:
        # Test instances record no audit events (see instance creation).
        api._audit.append(
            EventType.ADHOC_DELETED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
            node_id=req.node_id,
            label=api._label_of(schema, req.node_id),
            detail=detail,
        )
        # Deleting a node can hand control to a mail-bound successor -> notify.
        api._after_advance(api._effective_schema_for(after), before_states, after)
    return after


@router.post(
    "/instances/{instance_id}/adhoc/rename",
    response_model=ProcessInstance,
)
def post_adhoc_rename(
    instance_id: str,
    req: AdhocRenameRequest,
    principal: Principal = Depends(require_role("modeler", "admin")),
) -> ProcessInstance:
    """Rename a not yet reached step of one running instance (R1/R2).

    Modeller/admin only, with a reason for a real instance; the actor and the
    reason are recorded in ``ADHOC_RENAMED`` next to the new label.
    """

    instance = api._get_instance_or_404(instance_id)
    detail = api._adhoc_audit_detail(instance, principal, req.reason)
    schema = api._effective_schema_for(instance)
    after = api._commit_instance_or_422(
        lambda: adhoc.adhoc_rename_activity(
            instance, schema, req.node_id, req.label, resolver=api._resolver
        )
    )
    if not instance.is_test:
        # Test instances record no audit events (see instance creation).
        api._audit.append(
            EventType.ADHOC_RENAMED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
            node_id=req.node_id,
            label=api._label_of(schema, req.node_id),
            detail={"label": req.label, **detail},
        )
    return after


@router.post("/schemas/{schema_id}/revision", response_model=ProcessSchema, dependencies=[_model])
def post_new_revision(schema_id: str, req: RevisionRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.new_revision(schema, new_schema_id=req.new_schema_id)
    )


def _start_values(
    instance: ProcessInstance, target: ProcessSchema, data_mapping: dict[str, object]
) -> dict[str, object] | None:
    """Start values a single-instance migration may actually apply.

    Same rules as the bulk migration, so one instance and many move alike:

    * **Checked first (D3, 422):** every key must be an element of the target
      (``D3.unknown-element``) and its value must fit the element's type
      (``D3.wrong-type``, which includes a non-finite FLOAT). Before, unknown
      keys and mistyped values were stored in the instance unchecked.
    * **Gaps only:** of the valid values, only those the instance still lacks
      as mandatory input of the target are used (:func:`procworks.api._mapping_for`).
      A migration never overwrites recorded data -- changing a value is a data
      correction with reason and audit entry (``PUT …/data``), not a side
      effect of moving the instance.

    :param instance: the instance to move.
    :param target: the schema it would move onto.
    :param data_mapping: the start values sent with the request.
    :returns: the values to apply, or ``None`` when none is needed.
    :raises HTTPException: 422 with the D3 findings when a key or type is wrong.
    """

    type_findings = api._validate_data_values(target, data_mapping)
    if type_findings:
        raise api._findings_422(type_findings)
    return api._mapping_for(instance, target, data_mapping)


@router.post(
    "/instances/{instance_id}/migration-check",
    response_model=MigrationReport,
    dependencies=[_run],
)
def post_migration_check(
    instance_id: str, req: MigrateRequest, principal: Principal = Depends(get_principal)
) -> MigrationReport:
    """Dry-run check whether one instance could move onto ``target_schema_id``.

    A limited operator sees only instances it is involved in; any other is 404
    like a missing one (the involvement rule of :func:`_is_involved`). The
    start values are checked and narrowed exactly like in :func:`post_migrate`,
    so the dry run predicts what the real move does.
    """

    instance = api._readable_instance_or_404(instance_id, principal)
    source = api._get_or_404(instance.schema_id)
    target = api._get_or_404(req.target_schema_id)
    findings = migration.check_migration(
        instance,
        source,
        target,
        resolver=api._resolver,
        data_mapping=_start_values(instance, target, req.data_mapping),
    )
    return MigrationReport(migratable=not findings, findings=findings)


@router.post("/instances/{instance_id}/migrate", response_model=ProcessInstance)
def post_migrate(
    instance_id: str,
    req: MigrateRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    """Migrate one instance onto a newer revision (M1-M5 via the core).

    Operators may migrate -- the instance view offers it to them -- but a
    limited operator only an instance it is involved in; a foreign one is 404
    like a missing one. Start values follow the rules of the bulk migration
    (:func:`_start_values`): checked first, then used only to fill gaps.
    """

    instance = api._readable_instance_or_404(instance_id, principal)
    target = api._get_or_404(req.target_schema_id)
    mapping = _start_values(instance, target, req.data_mapping)
    return api._migrate_and_record(instance, target, mapping, principal)


@router.get(
    "/schemas/{schema_id}/migration-report",
    response_model=MigrationAssistantReport,
    dependencies=[_read],
)
def get_migration_report(
    schema_id: str, principal: Principal = Depends(get_principal)
) -> MigrationAssistantReport:
    """Which running instances of earlier revisions could move to this one?

    Read-only. For a draft target every candidate reports M1 (not released) --
    the assistant is meant for released revisions, but the answer stays honest.
    A limited operator sees only the instances it is involved in.
    """

    target = api._get_or_404(schema_id)
    candidates = []
    for inst in api._visible_migration_candidates(target, principal):
        findings, missing = api._assess(inst, target, None)
        candidates.append(
            MigrationCandidate(
                instance_id=inst.id,
                schema_id=inst.schema_id,
                schema_version=inst.schema_version,
                migratable=not findings,
                findings=findings,
                missing_data=missing,
                started_at=inst.started_at,
            )
        )
    return MigrationAssistantReport(
        target_schema_id=target.id, target_version=target.version, candidates=candidates
    )


@router.post(
    "/schemas/{schema_id}/migrate-instances",
    response_model=BulkMigrateReport,
)
def post_migrate_instances(
    schema_id: str,
    req: BulkMigrateRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> BulkMigrateReport:
    """Bulk migration onto ``schema_id`` -- a dry run unless ``execute`` is set.

    Every instance goes through the same core check and the same
    :func:`_migrate_and_record` as the single endpoint; there is no bulk
    shortcut. Instances are handled one by one, each atomically: a refused one
    stays exactly as it was, the others still move (partial success is
    reported per instance). Start values are type-checked against the target
    first (D3); a type error rejects the whole request before anything moves.
    Requested ids that are not candidates are reported as not migrated --
    for a limited operator that includes every instance it is not involved in
    -- the answer is the same as for an id that does not exist.
    """

    target = api._get_or_404(schema_id)
    type_findings = api._validate_data_values(target, req.data_mapping)
    if type_findings:
        raise api._findings_422(type_findings)
    candidates = {i.id: i for i in api._visible_migration_candidates(target, principal)}
    wanted = req.instance_ids if req.instance_ids is not None else list(candidates)
    results: list[BulkMigrateResult] = []
    for iid in wanted:
        inst = candidates.get(iid)
        if inst is None:
            results.append(
                BulkMigrateResult(
                    instance_id=iid,
                    migrated=False,
                    findings=[
                        ValidationFinding(
                            rule="M0",
                            message=(
                                f"instance '{iid}' is not a running instance of an "
                                f"earlier revision of '{target.id}'"
                            ),
                            code="M0.not-candidate",
                        )
                    ],
                )
            )
            continue
        mapping = api._mapping_for(inst, target, req.data_mapping)
        findings, _ = api._assess(inst, target, mapping)
        if findings or not req.execute:
            results.append(
                BulkMigrateResult(instance_id=iid, migrated=False, findings=findings)
            )
            continue
        try:
            api._migrate_and_record(inst, target, mapping, principal)
        except HTTPException as exc:
            # A race with a concurrent change: report it, keep going.
            detail: dict[str, object] = exc.detail if isinstance(exc.detail, dict) else {}
            raw = detail.get("findings", [])
            if not isinstance(raw, list):
                raw = []
            results.append(
                BulkMigrateResult(
                    instance_id=iid,
                    migrated=False,
                    findings=[ValidationFinding(**f) for f in raw],
                )
            )
            continue
        results.append(BulkMigrateResult(instance_id=iid, migrated=True, findings=[]))
    return BulkMigrateReport(
        target_schema_id=target.id, executed=req.execute, results=results
    )


@router.get(
    "/instances/{instance_id}/migration-target",
    response_model=MigrationTarget,
    dependencies=[_read],
)
def get_migration_target(
    instance_id: str, principal: Principal = Depends(get_principal)
) -> MigrationTarget:
    """The newest released revision this instance's schema has (if any)."""

    inst = api._readable_instance_or_404(instance_id, principal)
    schemas = [s for sid in api._store.list_ids() if (s := api._store.get(sid)) is not None]
    best = migration.latest_successor(inst.schema_id, schemas)
    if best is None:
        return MigrationTarget()
    return MigrationTarget(schema_id=best.id, version=best.version, name=best.name)
