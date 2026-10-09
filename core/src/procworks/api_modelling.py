# SPDX-License-Identifier: BUSL-1.1
"""Modelling endpoints for organisation details and node attributes.

Agents, deputies and mailboxes of shared organisation models; the
organisation of a schema (linked model, roles, units, agents); services,
automation and staff rules; value classes, priorities, mail bindings, time
constraints, deadlines and escalation policies; sync edges, simulation,
sub-processes, follow-up links and release. Like every modelling path they
go through the change operations and the validator (validate-before-commit).

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

from fastapi import APIRouter

from procworks import api, simulation
from procworks import operations as ops
from procworks import org as org_ops
from procworks.api import (
    AddActivityTemplateRequest,
    AddAgentRequest,
    AddOrgUnitRequest,
    AddRoleRequest,
    AssignServiceRequest,
    AssignStaffRuleRequest,
    ConvertToSubprocessRequest,
    InsertBetweenRequest,
    InsertSubprocessRequest,
    LibraryDataElement,
    LibraryFlagRequest,
    LinkFollowUpRequest,
    LinkOrgModelRequest,
    SetAutomationRequest,
    SetDeadlineRequest,
    SetDeputyRequest,
    SetEscalationPolicyRequest,
    SetMailBindingRequest,
    SetMailboxRequest,
    SetManagerRequest,
    SetParentRequest,
    SetPriorityRequest,
    SetSubprocessBindingRequest,
    SetTimeConstraintRequest,
    SetValueClassRequest,
    SimulateRequest,
    SubprocessLibraryEntry,
    SubprocessMappingRequest,
    SyncEdgeRequest,
    UpdateAgentRequest,
    _admin,
    _model,
    _read,
)
from procworks.model import LifecycleState, OrgModel, ProcessSchema

router: APIRouter = APIRouter()


@router.patch(
    "/org-models/{org_id}/agents/{agent_id}",
    response_model=OrgModel,
    dependencies=[_admin],
)
def patch_org_update_agent(org_id: str, agent_id: str, req: UpdateAgentRequest) -> OrgModel:
    org = api._get_org_or_404(org_id)
    org_unit = req.org_unit_id if "org_unit_id" in req.model_fields_set else org_ops.KEEP
    email = req.email if "email" in req.model_fields_set else org_ops.KEEP
    return api._commit_org_or_422(
        lambda: org_ops.org_update_agent(
            org,
            agent_id,
            name=req.name,
            role_ids=req.role_ids,
            org_unit_id=org_unit,
            email=email,
        )
    )


@router.post(
    "/org-models/{org_id}/agents/{agent_id}/deputy",
    response_model=OrgModel,
    dependencies=[_admin],
)
def post_org_set_deputy(org_id: str, agent_id: str, req: SetDeputyRequest) -> OrgModel:
    org = api._get_org_or_404(org_id)
    return api._commit_org_or_422(lambda: org_ops.org_set_deputy(org, agent_id, req.deputy_id))


@router.put(
    "/org-models/{org_id}/roles/{role_id}/mailbox",
    response_model=OrgModel,
    dependencies=[_admin],
)
def put_org_role_mailbox(org_id: str, role_id: str, req: SetMailboxRequest) -> OrgModel:
    """Set (or clear) a role's shared group mailbox (rule group N).

    A malformed address is rejected (N1, HTTP 422). Re-validates every schema
    referencing this org so removing a mailbox that a group notification needs
    (N3) is refused rather than silently breaking a released process.
    """

    org = api._get_org_or_404(org_id)
    return api._commit_org_or_422(
        lambda: org_ops.org_set_role_mailbox(org, role_id, req.mailbox)
    )


@router.put(
    "/org-models/{org_id}/units/{org_unit_id}/mailbox",
    response_model=OrgModel,
    dependencies=[_admin],
)
def put_org_unit_mailbox(
    org_id: str, org_unit_id: str, req: SetMailboxRequest
) -> OrgModel:
    """Set (or clear) an org unit's department mailbox (rule group N)."""

    org = api._get_org_or_404(org_id)
    return api._commit_org_or_422(
        lambda: org_ops.org_set_unit_mailbox(org, org_unit_id, req.mailbox)
    )


@router.post(
    "/schemas/{schema_id}/org-model",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_link_org_model(schema_id: str, req: LinkOrgModelRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    org = api._get_org_or_404(req.org_model_id)
    return api._commit_or_422(lambda: ops.link_org_model(schema, req.org_model_id, org))


@router.delete(
    "/schemas/{schema_id}/org-model",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_unlink_org_model(schema_id: str) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.unlink_org_model(schema))


@router.post("/schemas/{schema_id}/roles", response_model=ProcessSchema, dependencies=[_model])
def post_add_role(schema_id: str, req: AddRoleRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.add_role(schema, req.name, req.role_id))


@router.post(
    "/schemas/{schema_id}/org-units", response_model=ProcessSchema, dependencies=[_model]
)
def post_add_org_unit(schema_id: str, req: AddOrgUnitRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.add_org_unit(
            schema, req.name, req.parent_id, req.org_unit_id, req.manager_id
        )
    )


@router.post(
    "/schemas/{schema_id}/org-units/{org_unit_id}/manager",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_org_unit_manager(
    schema_id: str, org_unit_id: str, req: SetManagerRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_org_unit_manager(schema, org_unit_id, req.manager_id)
    )


@router.post(
    "/schemas/{schema_id}/org-units/{org_unit_id}/parent",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_org_unit_parent(
    schema_id: str, org_unit_id: str, req: SetParentRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_org_unit_parent(schema, org_unit_id, req.parent_id)
    )


@router.put(
    "/schemas/{schema_id}/roles/{role_id}/mailbox",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def put_role_mailbox(
    schema_id: str, role_id: str, req: SetMailboxRequest
) -> ProcessSchema:
    """Set (or clear) a role's group mailbox on the schema's embedded org (N)."""

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_role_mailbox(schema, role_id, req.mailbox)
    )


@router.put(
    "/schemas/{schema_id}/org-units/{org_unit_id}/mailbox",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def put_unit_mailbox(
    schema_id: str, org_unit_id: str, req: SetMailboxRequest
) -> ProcessSchema:
    """Set (or clear) an org unit's mailbox on the schema's embedded org (N)."""

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_unit_mailbox(schema, org_unit_id, req.mailbox)
    )


@router.post("/schemas/{schema_id}/agents", response_model=ProcessSchema, dependencies=[_model])
def post_add_agent(schema_id: str, req: AddAgentRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.add_agent(
            schema,
            req.name,
            req.role_ids,
            req.org_unit_id,
            req.agent_id,
            req.deputy_id,
            email=req.email,
        )
    )


@router.patch(
    "/schemas/{schema_id}/agents/{agent_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def patch_update_agent(
    schema_id: str, agent_id: str, req: UpdateAgentRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    # Distinguish "org_unit_id omitted" (keep) from "org_unit_id: null" (detach);
    # same for the e-mail address.
    org_unit = req.org_unit_id if "org_unit_id" in req.model_fields_set else ops.KEEP
    email = req.email if "email" in req.model_fields_set else ops.KEEP
    return api._commit_or_422(
        lambda: ops.update_agent(
            schema,
            agent_id,
            name=req.name,
            role_ids=req.role_ids,
            org_unit_id=org_unit,
            email=email,
        )
    )


@router.post(
    "/schemas/{schema_id}/agents/{agent_id}/deputy",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_agent_deputy(
    schema_id: str, agent_id: str, req: SetDeputyRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_agent_deputy(schema, agent_id, req.deputy_id)
    )


@router.post(
    "/schemas/{schema_id}/activity-templates",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_add_activity_template(
    schema_id: str, req: AddActivityTemplateRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.add_activity_template(
            schema,
            req.name,
            req.executor,
            inputs=req.inputs,
            outputs=req.outputs,
            template_id=req.template_id,
        )
    )


@router.post("/schemas/{schema_id}/service", response_model=ProcessSchema, dependencies=[_model])
def post_assign_service(schema_id: str, req: AssignServiceRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.assign_service(
            schema,
            req.node_id,
            req.name,
            automatic=req.automatic,
            template_id=req.template_id,
            parameter_mapping=req.parameter_mapping,
        )
    )


@router.delete(
    "/schemas/{schema_id}/service/{node_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_service(schema_id: str, node_id: str) -> ProcessSchema:
    """Remove the executing service (and its automation config) from ``node_id``.

    Validated like every other change (No-Bypass); a step without a service is
    well-formed in the draft (B1 is enforced at release). Returns HTTP 422 should
    the removal ever leave the model incorrect, otherwise the updated schema.
    """
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.unassign_service(schema, node_id))


@router.post(
    "/schemas/{schema_id}/automation", response_model=ProcessSchema, dependencies=[_model]
)
def post_set_automation(schema_id: str, req: SetAutomationRequest) -> ProcessSchema:
    """Configure how an automatic ACTIVITY is driven (E11: external task / push)."""

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_automation(
            schema,
            req.node_id,
            req.automation,
            topic=req.topic,
            endpoint_ref=req.endpoint_ref,
            retry_max=req.retry_max,
            retry_backoff_ms=req.retry_backoff_ms,
            request_timeout_ms=req.request_timeout_ms,
        )
    )


@router.post("/schemas/{schema_id}/staff-rule", response_model=ProcessSchema, dependencies=[_model])
def post_assign_staff_rule(schema_id: str, req: AssignStaffRuleRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.assign_staff_rule(schema, req.node_id, req.rule))


@router.delete(
    "/schemas/{schema_id}/staff-rule/{node_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_staff_rule(schema_id: str, node_id: str) -> ProcessSchema:
    """Remove the staff-assignment rule (BZR) from ``node_id``.

    Validated like every other change (No-Bypass); returns HTTP 422 should the
    removal ever leave the model incorrect, otherwise the updated schema.
    """
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.clear_staff_rule(schema, node_id))


@router.post(
    "/schemas/{schema_id}/value-class",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_value_class(schema_id: str, req: SetValueClassRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_value_class(schema, req.node_id, req.value_class)
    )


@router.post(
    "/schemas/{schema_id}/priority",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_priority(schema_id: str, req: SetPriorityRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_node_priority(schema, req.node_id, req.priority)
    )


@router.post(
    "/schemas/{schema_id}/mail-binding",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_mail_binding(schema_id: str, req: SetMailBindingRequest) -> ProcessSchema:
    """Attach (or clear with ``binding: null``) a modelled e-mail notification.

    Validated like every other change (No-Bypass): the mail rules N1-N4 run
    before commit, so a notification is only accepted when every possible
    recipient has an address and every template placeholder resolves; otherwise
    HTTP 422. It never sends a mail -- it only models when one is sent.
    """

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_mail_binding(schema, req.node_id, req.binding)
    )


@router.post(
    "/schemas/{schema_id}/time-constraint",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_time_constraint(
    schema_id: str, req: SetTimeConstraintRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_time_constraint(schema, req.node_id, req.constraint)
    )


@router.post(
    "/schemas/{schema_id}/deadline",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_deadline(schema_id: str, req: SetDeadlineRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.set_deadline(schema, req.deadline_seconds))


@router.post(
    "/schemas/{schema_id}/sync-edge",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_add_sync_edge(schema_id: str, req: SyncEdgeRequest) -> ProcessSchema:
    """Add a K4 sync edge (target waits for the source's resolution).

    Validate-before-commit: endpoints outside different branches of one AND
    block, or an ordering cycle, are rejected with HTTP 422.
    """

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.add_sync_edge(schema, req.source_id, req.target_id)
    )


@router.post(
    "/schemas/{schema_id}/sync-edge/remove",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_remove_sync_edge(schema_id: str, req: SyncEdgeRequest) -> ProcessSchema:
    """Remove a K4 sync edge (inverse of the add endpoint)."""

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.remove_sync_edge(schema, req.source_id, req.target_id)
    )


@router.post(
    "/schemas/{schema_id}/insert-between",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_insert_between(schema_id: str, req: InsertBetweenRequest) -> ProcessSchema:
    """Insert an activity between two node sets (ADEPT, K4 sync wiring)."""

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.insert_between_node_sets(
            schema, req.label, req.source_ids, req.target_ids
        )
    )


@router.post(
    "/schemas/{schema_id}/simulate",
    response_model=simulation.SimulationResult,
    dependencies=[_read],
)
def post_simulate(schema_id: str, req: SimulateRequest) -> simulation.SimulationResult:
    """Run a side-effect-free what-if walk over the schema (E6).

    Pure and read-only: the walk uses the operational engine semantics on a
    throw-away instance that never touches a store -- no audit, no mail, no
    tasks, nothing persisted. Drafts are allowed (semantic validation happens
    while modelling). The seed values pass the same boundary check as real
    instance data (D3, 422: unknown element, wrong type, non-finite FLOAT) --
    otherwise "vielleicht" in a BOOLEAN element would silently take a branch
    and the what-if result would mislead.
    """

    schema = api._get_or_404(schema_id)
    findings = api._validate_data_values(schema, req.data)
    if findings:
        raise api._findings_422(findings)
    return simulation.simulate(schema, req.data)


@router.post(
    "/schemas/{schema_id}/escalation-policy",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_escalation_policy(
    schema_id: str, req: SetEscalationPolicyRequest
) -> ProcessSchema:
    """Set or clear a node's modelled overdue reaction (T3/E9).

    Validate-before-commit: an undecidable escalation (no target time,
    unordered stages, unresolvable or node-referencing targets) is rejected
    with HTTP 422 and the schema stays unchanged.
    """

    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_escalation_policy(schema, req.node_id, req.policy)
    )


@router.post("/schemas/{schema_id}/subprocess", response_model=ProcessSchema, dependencies=[_model])
def post_insert_subprocess(
    schema_id: str, req: InsertSubprocessRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.insert_subprocess(
            schema,
            req.after_node_id,
            req.target_schema_id,
            req.target_version,
            label=req.label,
            input_mapping=req.input_mapping,
            output_mapping=req.output_mapping,
            resolver=api._resolver,
        )
    )


@router.post(
    "/schemas/{schema_id}/subprocess-mapping",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_subprocess_mapping(
    schema_id: str, req: SubprocessMappingRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_subprocess_mapping(
            schema,
            req.node_id,
            req.input_mapping,
            req.output_mapping,
            resolver=api._resolver,
        )
    )


@router.post(
    "/schemas/{schema_id}/convert-to-subprocess",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_convert_to_subprocess(
    schema_id: str, req: ConvertToSubprocessRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.convert_activity_to_subprocess(
            schema,
            req.node_id,
            req.target_schema_id,
            req.target_version,
            input_mapping=req.input_mapping,
            output_mapping=req.output_mapping,
            resolver=api._resolver,
        )
    )


@router.post(
    "/schemas/{schema_id}/subprocess-binding",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_subprocess_binding(
    schema_id: str, req: SetSubprocessBindingRequest
) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.set_subprocess_binding(
            schema,
            req.node_id,
            req.target_schema_id,
            req.target_version,
            input_mapping=req.input_mapping,
            output_mapping=req.output_mapping,
            resolver=api._resolver,
        )
    )


@router.post(
    "/schemas/{schema_id}/library-flag",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_library_flag(schema_id: str, req: LibraryFlagRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.set_library_subprocess(schema, req.is_library))


@router.get(
    "/subprocess-library",
    response_model=list[SubprocessLibraryEntry],
    dependencies=[_read],
)
def get_subprocess_library() -> list[SubprocessLibraryEntry]:
    entries: list[SubprocessLibraryEntry] = []
    for sid in api._store.list_ids():
        schema = api._store.get(sid)
        if schema is None:
            continue
        if not schema.is_library_subprocess:
            continue
        if schema.lifecycle_state is not LifecycleState.RELEASED:
            continue
        entries.append(
            SubprocessLibraryEntry(
                id=schema.id,
                name=schema.name,
                version=schema.version,
                data_elements=[
                    LibraryDataElement(
                        id=el.id, name=el.name, data_type=el.data_type.value
                    )
                    for el in schema.data_elements.values()
                ],
            )
        )
    return entries


@router.post("/schemas/{schema_id}/follow-up", response_model=ProcessSchema, dependencies=[_model])
def post_link_follow_up(schema_id: str, req: LinkFollowUpRequest) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(
        lambda: ops.link_follow_up(
            schema,
            req.target_schema_id,
            target_version=req.target_version,
            trigger=req.trigger,
            condition=req.condition,
            handover_mapping=req.handover_mapping,
            mode=req.mode,
            resolver=api._resolver,
        )
    )


@router.delete(
    "/schemas/{schema_id}/follow-up/{link_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_follow_up(schema_id: str, link_id: str) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.unlink_follow_up(schema, link_id))


@router.post("/schemas/{schema_id}/release", response_model=ProcessSchema, dependencies=[_model])
def post_release(schema_id: str) -> ProcessSchema:
    schema = api._get_or_404(schema_id)
    return api._commit_or_422(lambda: ops.release(schema, api._resolver))
