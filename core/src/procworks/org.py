# SPDX-License-Identifier: BUSL-1.1
"""Shared, standalone organisation models (cross-schema master data).

An :class:`~procworks.model.OrgModel` can be embedded in a single schema (the
default) or modelled **once** as a shared master-data entity that is reused by
several process schemas. This module provides the high-level operations that
mutate a *shared* org model together with the referential-integrity check
(:func:`validate_org`).

As with :mod:`procworks.operations`, every operation here is the *only* way to
mutate a shared org model: it deep-copies the model, applies the change,
validates it (validate-before-commit) and only then returns it. A shared org
model thus can never be persisted in an internally inconsistent state.

Org master data (managers, deputies, unit hierarchy, agent assignments) is
intentionally editable even while referencing schemas are released: people
move, units are reorganised, and those facts must be reflected live. The API
layer additionally re-validates every *referencing* schema before committing an
org edit, so a change can never silently break a released process's staffing
(Correctness by Construction across the org boundary).
"""

from __future__ import annotations

import itertools

from procworks.model import Agent, OrgModel, OrgUnit, Role, is_valid_email
from procworks.validator import CorrectnessError, ValidationFinding

_counter = itertools.count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{next(_counter)}"


class _KeepSentinel:
    """Marker meaning 'leave this field unchanged' in a partial update."""


KEEP = _KeepSentinel()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_org(org: OrgModel) -> list[ValidationFinding]:
    """Check the internal referential integrity of a shared org model.

    Mirrors the org master-data rules the schema validator applies (rules
    ``Z1`` and ``N1``): agents reference existing roles / units / deputies,
    units reference existing managers / parents, no agent is its own deputy, the
    unit hierarchy is acyclic, and every e-mail address / group mailbox is
    syntactically well-formed (N1) -- so a malformed address can never be stored
    on a shared org model either.
    """

    findings: list[ValidationFinding] = []
    findings += _check_addresses(org)
    for agent in org.agents.values():
        for role_id in agent.role_ids:
            if role_id not in org.roles:
                findings.append(
                    ValidationFinding(
                        rule="Z1",
                        message=f"agent '{agent.id}' references unknown role '{role_id}'",
                    )
                )
        if agent.org_unit_id is not None and agent.org_unit_id not in org.org_units:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    message=f"agent '{agent.id}' references unknown org unit '{agent.org_unit_id}'",
                )
            )
        if agent.deputy_id is not None:
            if agent.deputy_id == agent.id:
                findings.append(
                    ValidationFinding(
                        rule="Z1", message=f"agent '{agent.id}' cannot be its own deputy"
                    )
                )
            elif agent.deputy_id not in org.agents:
                findings.append(
                    ValidationFinding(
                        rule="Z1",
                        message=f"agent '{agent.id}' has unknown deputy '{agent.deputy_id}'",
                    )
                )
    for unit in org.org_units.values():
        if unit.manager_id is not None and unit.manager_id not in org.agents:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    message=f"org unit '{unit.id}' has unknown manager '{unit.manager_id}'",
                )
            )
        if unit.parent_id is not None and unit.parent_id not in org.org_units:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    message=f"org unit '{unit.id}' has unknown parent '{unit.parent_id}'",
                )
            )
    findings += _check_hierarchy_acyclic(org)
    return findings


def _check_addresses(org: OrgModel) -> list[ValidationFinding]:
    """N1: every e-mail address / group mailbox on the org model is well-formed."""

    findings: list[ValidationFinding] = []
    for agent in org.agents.values():
        if agent.email is not None and not is_valid_email(agent.email):
            findings.append(
                ValidationFinding(
                    rule="N1", message=f"agent '{agent.id}' has a malformed e-mail address"
                )
            )
    for role in org.roles.values():
        if role.mailbox is not None and not is_valid_email(role.mailbox):
            findings.append(
                ValidationFinding(
                    rule="N1", message=f"role '{role.id}' has a malformed group mailbox"
                )
            )
    for unit in org.org_units.values():
        if unit.mailbox is not None and not is_valid_email(unit.mailbox):
            findings.append(
                ValidationFinding(
                    rule="N1", message=f"org unit '{unit.id}' has a malformed mailbox"
                )
            )
    return findings


def _check_hierarchy_acyclic(org: OrgModel) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for start in org.org_units.values():
        seen: set[str] = set()
        current: str | None = start.id
        while current is not None:
            if current in seen:
                findings.append(
                    ValidationFinding(
                        rule="Z1",
                        message=f"org unit hierarchy contains a cycle at '{current}'",
                    )
                )
                break
            seen.add(current)
            unit = org.org_units.get(current)
            current = unit.parent_id if unit is not None else None
    return findings


def raise_if_invalid_org(org: OrgModel) -> OrgModel:
    """Return *org* if internally consistent, else raise ``CorrectnessError``."""

    findings = validate_org(org)
    if findings:
        raise CorrectnessError(findings)
    return org


def _fail(message: str) -> CorrectnessError:
    return CorrectnessError([ValidationFinding(rule="OP", message=message)])


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
def create_org_model(name: str, *, org_id: str | None = None) -> OrgModel:
    """Create a new, empty shared org model with a stable id."""

    return OrgModel(id=org_id or _new_id("org"), name=name)


def org_add_role(org: OrgModel, name: str, *, role_id: str | None = None) -> OrgModel:
    candidate = org.model_copy(deep=True)
    rid = role_id or _new_id("role")
    if rid in candidate.roles:
        raise _fail(f"role '{rid}' already exists")
    candidate.roles[rid] = Role(id=rid, name=name)
    return raise_if_invalid_org(candidate)


def org_add_unit(
    org: OrgModel,
    name: str,
    *,
    parent_id: str | None = None,
    org_unit_id: str | None = None,
    manager_id: str | None = None,
) -> OrgModel:
    candidate = org.model_copy(deep=True)
    uid = org_unit_id or _new_id("unit")
    if uid in candidate.org_units:
        raise _fail(f"org unit '{uid}' already exists")
    if parent_id is not None and parent_id not in candidate.org_units:
        raise _fail(f"parent org unit '{parent_id}' does not exist")
    if manager_id is not None and manager_id not in candidate.agents:
        raise _fail(f"manager '{manager_id}' does not exist")
    candidate.org_units[uid] = OrgUnit(
        id=uid, name=name, parent_id=parent_id, manager_id=manager_id
    )
    return raise_if_invalid_org(candidate)


def org_add_agent(
    org: OrgModel,
    name: str,
    *,
    role_ids: list[str] | None = None,
    org_unit_id: str | None = None,
    agent_id: str | None = None,
    deputy_id: str | None = None,
    email: str | None = None,
) -> OrgModel:
    candidate = org.model_copy(deep=True)
    aid = agent_id or _new_id("agent")
    if aid in candidate.agents:
        raise _fail(f"agent '{aid}' already exists")
    for role_id in role_ids or []:
        if role_id not in candidate.roles:
            raise _fail(f"role '{role_id}' does not exist")
    if org_unit_id is not None and org_unit_id not in candidate.org_units:
        raise _fail(f"org unit '{org_unit_id}' does not exist")
    if deputy_id is not None and deputy_id not in candidate.agents:
        raise _fail(f"deputy '{deputy_id}' does not exist")
    candidate.agents[aid] = Agent(
        id=aid,
        name=name,
        role_ids=list(role_ids or []),
        org_unit_id=org_unit_id,
        deputy_id=deputy_id,
        email=email,
    )
    return raise_if_invalid_org(candidate)


def org_update_agent(
    org: OrgModel,
    agent_id: str,
    *,
    name: str | None = None,
    role_ids: list[str] | None = None,
    org_unit_id: str | None | _KeepSentinel = KEEP,
    email: str | None | _KeepSentinel = KEEP,
) -> OrgModel:
    candidate = org.model_copy(deep=True)
    agent = candidate.agents.get(agent_id)
    if agent is None:
        raise _fail(f"agent '{agent_id}' does not exist")
    if name is not None:
        agent.name = name
    if role_ids is not None:
        for role_id in role_ids:
            if role_id not in candidate.roles:
                raise _fail(f"role '{role_id}' does not exist")
        agent.role_ids = list(role_ids)
    if not isinstance(org_unit_id, _KeepSentinel):
        if org_unit_id is not None and org_unit_id not in candidate.org_units:
            raise _fail(f"org unit '{org_unit_id}' does not exist")
        agent.org_unit_id = org_unit_id
    if not isinstance(email, _KeepSentinel):
        # ``None`` clears the address; a value is checked for well-formedness by
        # the validator (N1) before commit.
        agent.email = email
    return raise_if_invalid_org(candidate)


def org_set_manager(org: OrgModel, org_unit_id: str, manager_id: str | None) -> OrgModel:
    candidate = org.model_copy(deep=True)
    unit = candidate.org_units.get(org_unit_id)
    if unit is None:
        raise _fail(f"org unit '{org_unit_id}' does not exist")
    if manager_id is not None and manager_id not in candidate.agents:
        raise _fail(f"manager '{manager_id}' does not exist")
    unit.manager_id = manager_id
    return raise_if_invalid_org(candidate)


def org_set_parent(org: OrgModel, org_unit_id: str, parent_id: str | None) -> OrgModel:
    candidate = org.model_copy(deep=True)
    unit = candidate.org_units.get(org_unit_id)
    if unit is None:
        raise _fail(f"org unit '{org_unit_id}' does not exist")
    if parent_id is not None:
        if parent_id not in candidate.org_units:
            raise _fail(f"parent org unit '{parent_id}' does not exist")
        if parent_id == org_unit_id:
            raise _fail("an org unit cannot be its own parent")
        walker: str | None = parent_id
        while walker is not None:
            if walker == org_unit_id:
                raise _fail("setting this parent would create a cycle in the org hierarchy")
            walker = candidate.org_units[walker].parent_id
    unit.parent_id = parent_id
    return raise_if_invalid_org(candidate)


def org_set_deputy(org: OrgModel, agent_id: str, deputy_id: str | None) -> OrgModel:
    candidate = org.model_copy(deep=True)
    agent = candidate.agents.get(agent_id)
    if agent is None:
        raise _fail(f"agent '{agent_id}' does not exist")
    if deputy_id is not None:
        if deputy_id == agent_id:
            raise _fail("an agent cannot be its own deputy")
        if deputy_id not in candidate.agents:
            raise _fail(f"deputy '{deputy_id}' does not exist")
    agent.deputy_id = deputy_id
    return raise_if_invalid_org(candidate)


def org_set_role_mailbox(org: OrgModel, role_id: str, mailbox: str | None) -> OrgModel:
    """Set (or clear with ``None``) a role's shared group mailbox (rule group N).

    The address is checked for well-formedness (N1) before commit; a malformed
    address is rejected. Used as the target of a ``TO_GROUP_MAILBOX`` mail
    notification addressed to this role.
    """

    candidate = org.model_copy(deep=True)
    role = candidate.roles.get(role_id)
    if role is None:
        raise _fail(f"role '{role_id}' does not exist")
    role.mailbox = mailbox
    return raise_if_invalid_org(candidate)


def org_set_unit_mailbox(org: OrgModel, org_unit_id: str, mailbox: str | None) -> OrgModel:
    """Set (or clear with ``None``) an org unit's department mailbox (rule group N).

    The address is checked for well-formedness (N1) before commit. Used as the
    target of a ``TO_GROUP_MAILBOX`` mail notification addressed to this unit.
    """

    candidate = org.model_copy(deep=True)
    unit = candidate.org_units.get(org_unit_id)
    if unit is None:
        raise _fail(f"org unit '{org_unit_id}' does not exist")
    unit.mailbox = mailbox
    return raise_if_invalid_org(candidate)
