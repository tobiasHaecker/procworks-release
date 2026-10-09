# SPDX-License-Identifier: BUSL-1.1
"""Shared, cross-schema organisation models and the agent directory.

An organisation model is shared by every schema linked to it; its changes
are checked against those schemas before they are stored.

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

from fastapi import APIRouter, HTTPException

from procworks import api
from procworks import org as org_ops
from procworks.api import (
    AddAgentRequest,
    AddOrgUnitRequest,
    AddRoleRequest,
    CreateOrgModelRequest,
    DirectoryAgent,
    SetManagerRequest,
    SetParentRequest,
    _admin,
    _read,
)
from procworks.licensing import LicenseError
from procworks.model import OrgModel

router: APIRouter = APIRouter()


@router.get("/org-models", response_model=list[OrgModel], dependencies=[_read])
def get_org_models() -> list[OrgModel]:
    return [
        org for oid in api._org_store.list_ids() if (org := api._org_store.get(oid)) is not None
    ]


@router.get("/directory/agents", response_model=list[DirectoryAgent], dependencies=[_read])
def get_directory_agents() -> list[DirectoryAgent]:
    """All known agents across shared and model-local organisations, by name.

    A worklist spans **every** process model, so the people in it cannot be
    resolved against the one model a client currently has open: that would show
    a clerk internal ids (``a-erika``) whenever another process was selected,
    and hide her tasks behind a modeller hint when that process had no agents at
    all. This directory is the model-independent
    answer: it lists the agents of the shared org registry first, then those of
    schemas that carry their own embedded organisation.

    Duplicate ids keep their first (shared) entry -- the shared registry is the
    master data, an embedded copy only exists for unlinked schemas. The result
    is display data; it never decides who may work a step.
    """

    seen: dict[str, DirectoryAgent] = {}
    for org_id in api._org_store.list_ids():
        org = api._org_store.get(org_id)
        if org is None:
            continue
        for agent in org.agents.values():
            seen.setdefault(
                agent.id,
                DirectoryAgent(
                    agent_id=agent.id,
                    name=agent.name,
                    email=agent.email,
                    deputy_id=agent.deputy_id,
                    org_model_id=org_id,
                ),
            )
    for schema_id in api._store.list_ids():
        schema = api._store.get(schema_id)
        if schema is None or schema.org_model_id is not None:
            continue  # linked schemas resolve through the shared registry above
        for agent in schema.org_model.agents.values():
            seen.setdefault(
                agent.id,
                DirectoryAgent(
                    agent_id=agent.id,
                    name=agent.name,
                    email=agent.email,
                    deputy_id=agent.deputy_id,
                    schema_id=schema_id,
                ),
            )
    return sorted(seen.values(), key=lambda a: (a.name.casefold(), a.agent_id))


@router.post(
    "/org-models", response_model=OrgModel, status_code=201, dependencies=[_admin]
)
def post_create_org_model(req: CreateOrgModelRequest) -> OrgModel:
    org = org_ops.create_org_model(req.name, org_id=req.org_model_id)
    return api._org_store.put(org)


@router.get("/org-models/{org_id}", response_model=OrgModel, dependencies=[_read])
def get_org_model(org_id: str) -> OrgModel:
    return api._get_org_or_404(org_id)


@router.post("/org-models/{org_id}/roles", response_model=OrgModel, dependencies=[_admin])
def post_org_add_role(org_id: str, req: AddRoleRequest) -> OrgModel:
    org = api._get_org_or_404(org_id)
    return api._commit_org_or_422(lambda: org_ops.org_add_role(org, req.name, role_id=req.role_id))


@router.post(
    "/org-models/{org_id}/org-units", response_model=OrgModel, dependencies=[_admin]
)
def post_org_add_unit(org_id: str, req: AddOrgUnitRequest) -> OrgModel:
    org = api._get_org_or_404(org_id)
    return api._commit_org_or_422(
        lambda: org_ops.org_add_unit(
            org,
            req.name,
            parent_id=req.parent_id,
            org_unit_id=req.org_unit_id,
            manager_id=req.manager_id,
        )
    )


@router.post(
    "/org-models/{org_id}/org-units/{org_unit_id}/manager",
    response_model=OrgModel,
    dependencies=[_admin],
)
def post_org_set_manager(org_id: str, org_unit_id: str, req: SetManagerRequest) -> OrgModel:
    org = api._get_org_or_404(org_id)
    return api._commit_org_or_422(lambda: org_ops.org_set_manager(org, org_unit_id, req.manager_id))


@router.post(
    "/org-models/{org_id}/org-units/{org_unit_id}/parent",
    response_model=OrgModel,
    dependencies=[_admin],
)
def post_org_set_parent(org_id: str, org_unit_id: str, req: SetParentRequest) -> OrgModel:
    org = api._get_org_or_404(org_id)
    return api._commit_org_or_422(lambda: org_ops.org_set_parent(org, org_unit_id, req.parent_id))


@router.post("/org-models/{org_id}/agents", response_model=OrgModel, dependencies=[_admin])
def post_org_add_agent(org_id: str, req: AddAgentRequest) -> OrgModel:
    org = api._get_org_or_404(org_id)
    # Licensing guard (dormant unless enforced): creating an agent beyond the
    # covered contingent is a purchase offer, not an error (HTTP 402). No effect
    # while licensing is off; then no quota applies and no binding is written.
    before_ids = api._all_agent_ids()
    if api._license.enforced and not api._license.can_create_agent(before_ids):
        raise HTTPException(
            status_code=402,
            detail=(
                "Agenten-Kontingent ausgeschöpft – bitte ein Agenten-Paket "
                "(+5 Agenten / 1 Jahr) hinzubuchen."
            ),
        )
    updated = api._commit_org_or_422(
        lambda: org_ops.org_add_agent(
            org,
            req.name,
            role_ids=req.role_ids,
            org_unit_id=req.org_unit_id,
            agent_id=req.agent_id,
            deputy_id=req.deputy_id,
            email=req.email,
        )
    )
    if api._license.enforced:
        # Bind exactly the agent(s) just added (set difference vs. the earlier
        # universe) to spare capacity, so the new agent counts against a slot.
        all_ids = api._all_agent_ids()
        for new_id in set(updated.agents) - before_ids:
            try:
                api._license.auto_bind_new_agent(new_id, all_ids)
            except LicenseError:
                # Quota was checked above; a race here should not fail the write.
                break
    return updated
