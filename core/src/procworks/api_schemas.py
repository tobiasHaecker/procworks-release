# SPDX-License-Identifier: BUSL-1.1
"""Modelling endpoints: process schemas, templates, structural change
operations, data elements, forms, connectors and BPMN import/export.

Every mutation goes through a change operation and the validator before it
is stored (validate-before-commit); a rejected change answers 422.

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

from fastapi import APIRouter, HTTPException, Response

from procworks import api, metrics
from procworks import bpmn as bpmn_io
from procworks import operations as ops
from procworks import templates as builtin_templates_mod
from procworks.api import (
    AddDataElementRequest,
    BindExternalDataRequest,
    ConditionalInsertRequest,
    ConnectDataRequest,
    CreateSchemaRequest,
    ImportBpmnRequest,
    InstantiateTemplateRequest,
    LoopDecisionRequest,
    LoopInsertRequest,
    MoveNodeRequest,
    ParallelInsertRequest,
    RegisterConnectorRequest,
    RenameNodeRequest,
    SaveTemplateRequest,
    SerialInsertRequest,
    SetFormRequest,
    SqlSelectRequest,
    SqlWriteRequest,
    TemplateRoleInfo,
    TemplateSummary,
    UpdateDataElementRequest,
    ValidationReport,
    _model,
    _read,
)
from procworks.bpmn import BpmnError
from procworks.metrics import ModelReport
from procworks.model import (
    AccessMode,
    NodeType,
    OrderBy,
    ProcessSchema,
    ProcessTemplate,
    QueryFilter,
    TemplateOrigin,
)
from procworks.validator import CorrectnessError, check_executable, validate

router: APIRouter = APIRouter()


@router.get("/schemas", dependencies=[_read])
def list_schemas() -> list[str]:
    return api._store.list_ids()


@router.post(
    "/schemas", response_model=ProcessSchema, status_code=201, dependencies=[_model]
)
def create_schema(req: CreateSchemaRequest) -> ProcessSchema:
    return api._commit_or_422(lambda: ops.create_empty_schema(req.name))


@router.get("/templates", response_model=list[TemplateSummary], dependencies=[_read])
def list_templates() -> list[TemplateSummary]:
    """List all process templates (built-in library + saved user templates).

    Returns lightweight summaries; fetch a single template or instantiate it to
    obtain the full blueprint. Built-in templates come first, user templates
    after, each block sorted by name for a stable gallery order.
    """

    templates = list(api._all_templates().values())
    templates.sort(key=lambda t: (t.origin is not TemplateOrigin.BUILTIN, t.name.lower()))
    return [
        TemplateSummary(
            id=t.id,
            name=t.name,
            description=t.description,
            category=t.category,
            origin=t.origin,
            step_count=sum(
                1
                for n in t.blueprint.nodes.values()
                if n.type in (NodeType.ACTIVITY, NodeType.SUBPROCESS)
            ),
            roles=[
                TemplateRoleInfo(name=name, steps=steps)
                for name, steps in builtin_templates_mod.template_roles(t.blueprint)
            ],
        )
        for t in templates
    ]


@router.get("/templates/{template_id}", response_model=ProcessTemplate, dependencies=[_read])
def get_template(template_id: str) -> ProcessTemplate:
    """Fetch a single template including its full schema blueprint."""

    return api._get_template_or_404(template_id)


@router.post(
    "/templates", response_model=ProcessTemplate, status_code=201, dependencies=[_model]
)
def save_template(req: SaveTemplateRequest) -> ProcessTemplate:
    """Save an existing schema as a reusable *user* template (modeller/admin).

    The source schema is captured as a self-contained, validated blueprint (its
    org master data is embedded and it is reset to a clean draft). Only correct
    schemas can be captured -- the operation runs the full validation, so a
    broken blueprint is rejected with HTTP 422 (validate-before-commit).
    """

    schema = api._get_or_404(req.schema_id)
    try:
        template = ops.save_as_template(
            schema,
            name=req.name,
            description=req.description,
            category=req.category,
            origin=TemplateOrigin.USER,
        )
    except CorrectnessError as exc:
        raise api._findings_422(exc.findings) from exc
    return api._template_store.put(template)


@router.post(
    "/templates/{template_id}/instantiate",
    response_model=ProcessSchema,
    status_code=201,
    dependencies=[_model],
)
def instantiate_template(
    template_id: str, req: InstantiateTemplateRequest
) -> ProcessSchema:
    """Create a fresh, editable draft schema from a template (modeller/admin).

    Deep-copies the template blueprint into a new schema with a fresh id and
    ``ENTWURF`` state; the modeller edits and releases it like any other draft.
    """

    template = api._get_template_or_404(template_id)
    return api._commit_or_422(lambda: ops.instantiate_template(template, name=req.name))


@router.delete("/templates/{template_id}", status_code=204, dependencies=[_model])
def delete_template(template_id: str) -> Response:
    """Delete a *user* template (modeller/admin). Built-in templates are code.

    Deleting a built-in template is rejected with HTTP 422 -- it ships with the
    product and cannot be removed. A missing template yields HTTP 404.
    """

    if api._template_store.get(template_id) is None:
        # Not a user template: either a built-in (refuse) or unknown (404).
        api._get_template_or_404(template_id)  # raises 404 if truly unknown
        raise HTTPException(
            status_code=422, detail="built-in templates cannot be deleted"
        )
    api._template_store.delete(template_id)
    return Response(status_code=204)


@router.get("/schemas/{schema_id}", response_model=ProcessSchema, dependencies=[_read])
def get_schema(schema_id: str) -> ProcessSchema:
    return api._get_or_404(schema_id)


@router.get(
    "/schemas/{schema_id}/validation",
    response_model=ValidationReport,
    dependencies=[_read],
)
def get_validation(schema_id: str) -> ValidationReport:
    """Structural correctness (Stufe A) **and** release readiness (Stufe B).

    The two are reported separately because they mean different things: ``correct``
    is an invariant that holds after every operation, while ``releasable`` is a
    bar a draft only has to clear when it is released. A modeller therefore sees
    "still missing an assignee" as a *completeness* state, not as an error.
    """

    schema = api._get_or_404(schema_id)
    findings = validate(schema, api._resolver)
    release_findings = check_executable(schema)
    return ValidationReport(
        correct=not findings,
        findings=findings,
        releasable=not release_findings,
        release_findings=release_findings,
    )


@router.get(
    "/schemas/{schema_id}/metrics",
    response_model=ModelReport,
    dependencies=[_read],
)
def get_metrics(schema_id: str) -> ModelReport:
    """Read-only model metrics, 7PMG hints and value-class mix (roadmap E7/E3).

    These figures are advisory only and never affect Stufe-A/B correctness.
    """

    schema = api._get_or_404(schema_id)
    return metrics.model_report(schema)


@router.post(
    "/schemas/{schema_id}/serial-insert",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_serial_insert(schema_id: str, req: SerialInsertRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.serial_insert(schema, req.label, req.after_node_id, req.before_node_id)
    )


@router.post(
    "/schemas/{schema_id}/parallel-insert",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_parallel_insert(schema_id: str, req: ParallelInsertRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.parallel_insert(schema, req.branch_labels, req.after_node_id)
    )


@router.post(
    "/schemas/{schema_id}/conditional-insert",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_conditional_insert(schema_id: str, req: ConditionalInsertRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    branches = [
        ops.BranchSpec(
            label=b.label,
            upper=b.upper,
            bool_value=b.bool_value,
            values=tuple(b.values),
            is_else=b.is_else,
        )
        for b in req.branches
    ]
    return api._commit_or_422(
        lambda: ops.conditional_insert(
            schema, req.after_node_id, discriminator=req.discriminator, branches=branches
        )
    )


@router.post(
    "/schemas/{schema_id}/loop-insert",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_loop_insert(schema_id: str, req: LoopInsertRequest) -> ProcessSchema:
    """Insert a REPEAT-UNTIL loop block (K6) after the anchor node.

    The body runs at least once; at the LOOP_END the BOOLEAN discriminator
    decides deterministically whether it repeats. Validate-before-commit as
    always: an undecidable or ill-placed loop is rejected with HTTP 422.
    """

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.insert_loop(
            schema,
            req.after_node_id,
            req.label,
            discriminator=req.discriminator,
            repeat_value=req.repeat_value,
            cells=req.cells,
            max_iterations=req.max_iterations,
        )
    )


@router.post(
    "/schemas/{schema_id}/loop-decision",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_loop_decision(schema_id: str, req: LoopDecisionRequest) -> ProcessSchema:
    """Replace the exit condition of an existing loop (K6b).

    Retargets the decision to another discriminator, flips the boolean
    shorthand or switches to/from an S3 repeat/exit partition. The body must
    (still) write the discriminator on every path (K6c) -- otherwise HTTP 422
    and the schema stays unchanged.
    """

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_loop_decision(
            schema,
            req.node_id,
            discriminator=req.discriminator,
            repeat_value=req.repeat_value,
            cells=req.cells,
            max_iterations=req.max_iterations,
        )
    )


@router.patch(
    "/schemas/{schema_id}/nodes/{node_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def patch_rename_node(
    schema_id: str, node_id: str, req: RenameNodeRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.rename_node(schema, node_id, req.label))


@router.delete(
    "/schemas/{schema_id}/nodes/{node_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_schema_node(schema_id: str, node_id: str) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.delete_node(schema, node_id))


@router.post(
    "/schemas/{schema_id}/nodes/{node_id}/move",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_move_node(schema_id: str, node_id: str, req: MoveNodeRequest) -> ProcessSchema:
    """Move a step to a new serial position, keeping all its bindings.

    The node is spliced out of its current position and re-inserted directly
    after ``after_node_id`` (validate-before-commit: an invalid move -- e.g. a
    reader ahead of its writer, D1 -- is rejected with HTTP 422 and the schema
    stays unchanged).
    """

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.move_node(schema, node_id, req.after_node_id))


@router.post(
    "/schemas/{schema_id}/nodes/{node_id}/remove-empty-branch",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_remove_empty_branch(schema_id: str, node_id: str) -> ProcessSchema:
    """Remove the empty branch of the XOR split ``node_id`` (validate-before-commit).

    ``node_id`` is the XOR_SPLIT that currently carries one empty ``split ->
    join`` branch. Dissolves the whole gateway when only the non-empty branch
    would remain, otherwise drops just the empty cell; an invalid result (e.g. a
    lost catch-all) is rejected with HTTP 422.
    """

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.remove_empty_branch(schema, node_id))


@router.post(
    "/schemas/{schema_id}/data-elements",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_add_data_element(schema_id: str, req: AddDataElementRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.add_data_element(schema, req.name, req.data_type, req.element_id)
    )


@router.patch(
    "/schemas/{schema_id}/data-elements/{element_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def patch_data_element(
    schema_id: str, element_id: str, req: UpdateDataElementRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.update_data_element(
            schema, element_id, name=req.name, data_type=req.data_type
        )
    )


@router.delete(
    "/schemas/{schema_id}/data-elements/{element_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_schema_data_element(schema_id: str, element_id: str) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.delete_data_element(schema, element_id))


@router.post(
    "/schemas/{schema_id}/data-elements/{element_id}/reset-source",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_reset_data_element_source(schema_id: str, element_id: str) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.reset_data_element_source(schema, element_id))


@router.post(
    "/schemas/{schema_id}/data-access",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_connect_data(schema_id: str, req: ConnectDataRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.connect_data(
            schema,
            req.node_id,
            req.element_id,
            req.mode,
            mandatory=req.mandatory,
            param_type=req.param_type,
        )
    )


@router.delete(
    "/schemas/{schema_id}/data-access/{node_id}/{element_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_data_access(
    schema_id: str, node_id: str, element_id: str, mode: AccessMode | None = None
) -> ProcessSchema:
    """Remove a data binding of ``element_id`` from ``node_id``.

    Without ``mode`` every access of the element on that node is removed; with
    ``mode`` only that direction. The core re-checks D1-D4 (e.g. removing the
    sole writer behind a mandatory read elsewhere is rejected with HTTP 422).
    """
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.disconnect_data(schema, node_id, element_id, mode)
    )


@router.post(
    "/schemas/{schema_id}/nodes/{node_id}/form",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_form(
    schema_id: str, node_id: str, req: SetFormRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    specs = [
        ops.FormFieldSpec(
            element_id=f.element_id,
            widget=f.widget,
            label=f.label,
            mode=f.mode,
            required=f.required,
            options=tuple(f.options),
            help_text=f.help_text,
            group=f.group,
            min_value=f.min_value,
            max_value=f.max_value,
            pattern=f.pattern,
            max_length=f.max_length,
        )
        for f in req.fields
    ]
    return api._commit_or_422(
        lambda: ops.set_form(
            schema, node_id, title=req.title, fields=specs, columns=req.columns
        )
    )


@router.delete(
    "/schemas/{schema_id}/nodes/{node_id}/form",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_schema_form(schema_id: str, node_id: str) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.delete_form(schema, node_id))


@router.post(
    "/schemas/{schema_id}/connectors",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_register_connector(
    schema_id: str, req: RegisterConnectorRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.register_connector(
            schema, req.name, req.kind, connector_id=req.connector_id
        )
    )


@router.post(
    "/schemas/{schema_id}/data-elements/{element_id}/external",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_bind_external_data(
    schema_id: str, element_id: str, req: BindExternalDataRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.bind_external_data(
            schema,
            element_id,
            connector_id=req.connector_id,
            entity=req.entity,
            key_element_id=req.key_element_id,
        )
    )


@router.post(
    "/schemas/{schema_id}/data-elements/{element_id}/sql-select",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_bind_sql_select(
    schema_id: str, element_id: str, req: SqlSelectRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    filters = [
        QueryFilter(
            column=f.column,
            column_type=f.column_type,
            operator=f.operator,
            key_element_id=f.key_element_id,
        )
        for f in req.filters
    ]
    order_by = [OrderBy(column=o.column, descending=o.descending) for o in req.order_by]
    return api._commit_or_422(
        lambda: ops.bind_sql_select(
            schema,
            element_id,
            connector_id=req.connector_id,
            entity=req.entity,
            column=req.column,
            column_type=req.column_type,
            aggregate=req.aggregate,
            filters=filters,
            cardinality=req.cardinality,
            order_by=order_by,
            unique_column=req.unique_column,
        )
    )


@router.post(
    "/schemas/{schema_id}/data-elements/{element_id}/sql-write",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_bind_sql_write(
    schema_id: str, element_id: str, req: SqlWriteRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    filters = [
        QueryFilter(
            column=f.column,
            column_type=f.column_type,
            operator=f.operator,
            key_element_id=f.key_element_id,
        )
        for f in req.filters
    ]
    return api._commit_or_422(
        lambda: ops.bind_sql_write(
            schema,
            element_id,
            connector_id=req.connector_id,
            entity=req.entity,
            column=req.column,
            column_type=req.column_type,
            filters=filters,
            unique_column=req.unique_column,
        )
    )


@router.get("/schemas/{schema_id}/bpmn", dependencies=[_read])
def get_export_bpmn(schema_id: str) -> Response:
    schema = api._get_or_404(schema_id)
    try:
        xml = bpmn_io.export_bpmn(schema)
    except bpmn_io.BpmnError as exc:
        # e.g. loops (K6, stage S1): not representable in the validated BPMN
        # subset yet -- a clear 422 beats a file that cannot round-trip.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(content=xml, media_type="application/xml")


@router.post(
    "/bpmn-import",
    response_model=ProcessSchema,
    status_code=201,
    dependencies=[_model],
)
def post_import_bpmn(req: ImportBpmnRequest) -> ProcessSchema:
    """Import a BPMN document as a **new** draft (never over an existing model).

    The id is the one weak spot of this entry point: ``import_bpmn`` falls back
    to the ``<process id>`` of the *foreign* document, and a ProcWorks export
    carries the id of the schema it came from. Re-importing an own export
    therefore landed on that very id -- and ``_store.put`` replaced the stored
    model with the freshly imported draft. Found while running the demo through
    on 2026-09-23: a re-import of the leave-request export turned the
    **released** ``urlaubsantrag`` into an unrelated draft, with running
    instances pointing at it. No rule was broken on the way in (the import
    validates), but R0 -- a released schema is immutable -- was circumvented by
    *replacing* instead of editing.

    Therefore: a colliding id is not honoured. Without an explicit
    ``schema_id`` the import moves to a fresh id (an import creates a model, it
    does not update one); with an explicit one the request is refused (409), so
    a deliberate choice never silently lands somewhere else. The final object is
    the validated one -- the import runs again for the new id rather than having
    its id patched afterwards.
    """

    if req.schema_id is not None and api._store.get(req.schema_id) is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"schema '{req.schema_id}' already exists; an import creates a "
                    "new model and never replaces a stored one"
                ),
                "code": "OP.already-exists",
                "params": {"kind": "schema", "id": req.schema_id},
            },
        )

    def run(schema_id: str | None) -> ProcessSchema:
        try:
            return bpmn_io.import_bpmn(
                req.xml, schema_id=schema_id, name=req.name, resolver=api._resolver
            )
        except BpmnError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": str(exc), "code": exc.code, "params": exc.params},
            ) from exc
        except CorrectnessError as exc:
            raise api._findings_422(exc.findings) from exc

    schema = run(req.schema_id)
    if req.schema_id is None and api._store.get(schema.id) is not None:
        schema = run(ops.new_schema_id())
    return api._store.put(schema)
