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

from collections.abc import Mapping

from procworks import ids
from procworks.model import Agent, OrgModel, OrgUnit, Role

# Dieselbe Markierung wie in den Schema-Operationen: Die Teil-Updates prüfen
# per Identität (``is KEEP``), zwei getrennte Objekte würden ein vertauschtes
# ``ops.KEEP``/``org_ops.KEEP`` still als neuen Wert übernehmen.
from procworks.operations import KEEP as KEEP  # Re-Export: api.py nutzt org_ops.KEEP
from procworks.operations import _KeepSentinel

# N1 prüft ein geteiltes Organisationsmodell genauso wie die Org-Daten eines
# Schemas; eine einzige Umsetzung hält Codes, Texte und Reihenfolge auf beiden
# Wegen gleich.
from procworks.validator import (
    CorrectnessError,
    ValidationFinding,
    clean_label,
    org_address_findings,
)


def _new_id(prefix: str) -> str:
    """Return a fresh ``<prefix>_<n>`` id from the shared org sequence.

    Replaces a process-local counter that restarted at 1; ``org`` ids
    are additionally checked against the org store, see :mod:`procworks.ids`.
    """

    return ids.ORG_IDS.new(prefix)


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
    findings += org_address_findings(org)
    for agent in org.agents.values():
        for role_id in agent.role_ids:
            if role_id not in org.roles:
                findings.append(
                    ValidationFinding(
                        rule="Z1",
                        message=f"agent '{agent.id}' references unknown role '{role_id}'",
                        code="Z1.agent-unknown-role",
                        params={"agent": str(agent.id), "role": str(role_id)},
                    )
                )
        if agent.org_unit_id is not None and agent.org_unit_id not in org.org_units:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    message=f"agent '{agent.id}' references unknown org unit '{agent.org_unit_id}'",
                    code="Z1.agent-unknown-unit",
                    params={"agent": str(agent.id), "unit_ref": str(agent.org_unit_id)},
                )
            )
        if agent.deputy_id is not None:
            if agent.deputy_id == agent.id:
                findings.append(
                    ValidationFinding(
                        rule="Z1",
                        message=f"agent '{agent.id}' cannot be its own deputy",
                        code="Z1.own-deputy",
                        params={"agent": str(agent.id)},
                    )
                )
            elif agent.deputy_id not in org.agents:
                findings.append(
                    ValidationFinding(
                        rule="Z1",
                        message=f"agent '{agent.id}' has unknown deputy '{agent.deputy_id}'",
                        code="Z1.unknown-deputy",
                        params={"agent": str(agent.id), "deputy": str(agent.deputy_id)},
                    )
                )
    for unit in org.org_units.values():
        if unit.manager_id is not None and unit.manager_id not in org.agents:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    message=f"org unit '{unit.id}' has unknown manager '{unit.manager_id}'",
                    code="Z1.unknown-manager",
                    params={"unit": str(unit.id), "manager": str(unit.manager_id)},
                )
            )
        if unit.parent_id is not None and unit.parent_id not in org.org_units:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    message=f"org unit '{unit.id}' has unknown parent '{unit.parent_id}'",
                    code="Z1.unknown-parent",
                    params={"unit": str(unit.id), "parent": str(unit.parent_id)},
                )
            )
    findings += _check_hierarchy_acyclic(org)
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
                        code="Z1.unit-cycle",
                        params={"unit": str(current)},
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


def _fail(
    message: str, *, code: str, params: dict[str, str] | None = None
) -> CorrectnessError:
    """OP rejection of an org operation, with a ``code`` the client words.

    Same codes as ``operations.py`` (``OP.not-found``/``OP.already-exists`` with
    ``kind`` and ``name``), so one catalogue entry covers both.
    """

    return CorrectnessError(
        [ValidationFinding(rule="OP", message=message, code=code, params=params or {})]
    )


def _not_found(label: str, kind: str, ident: str) -> CorrectnessError:
    """``OP.not-found``-Ablehnung für eine fehlende Org-Referenz.

    :param label: Wort für die Meldung, z. B. ``"role"``, ``"parent org unit"``
        oder ``"deputy"`` -- ergibt ``"<label> '<ident>' does not exist"``.
    :param kind: Art für den Katalogtext (``"role"``, ``"org_unit"``, ``"agent"``).
    :param ident: die nicht gefundene Kennung.
    :returns: die zu werfende ``CorrectnessError`` (Meldung, Code und Parameter
        genau wie zuvor an jeder einzelnen Stelle ausgeschrieben).
    """

    return _fail(
        f"{label} '{ident}' does not exist",
        code="OP.not-found",
        params={"kind": kind, "name": str(ident)},
    )


def _already_exists(label: str, kind: str, ident: str) -> CorrectnessError:
    """``OP.already-exists``-Ablehnung, wenn eine explizit genannte Kennung belegt ist.

    :param label: Wort für die Meldung (``"role"``, ``"org unit"``, ``"agent"``)
        -- ergibt ``"<label> '<ident>' already exists"``.
    :param kind: Art für den Katalogtext (``"role"``, ``"org_unit"``, ``"agent"``).
    :param ident: die bereits vergebene Kennung.
    :returns: die zu werfende ``CorrectnessError``.
    """

    return _fail(
        f"{label} '{ident}' already exists",
        code="OP.already-exists",
        params={"kind": kind, "name": str(ident)},
    )


def _require_ref(
    ident: str | None, existing: Mapping[str, object], *, label: str, kind: str
) -> None:
    """Lehnt eine gesetzte Referenz ab, die im Org-Modell nicht existiert.

    ``None`` bedeutet „keine Referenz“ und ist immer zulässig -- genau die
    Form ``if x is not None and x not in ...: raise`` aller Operationen.

    :param ident: die optionale Kennung (Rolle, Einheit, Agent).
    :param existing: das Verzeichnis, in dem sie stehen muss.
    :param label: Wort für die Meldung, siehe :func:`_not_found`.
    :param kind: Art für den Katalogtext, siehe :func:`_not_found`.
    :raises CorrectnessError: ``OP.not-found``, wenn die Kennung fehlt.
    """

    if ident is not None and ident not in existing:
        raise _not_found(label, kind, ident)


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
def create_org_model(name: str, *, org_id: str | None = None) -> OrgModel:
    """Create a new, empty shared org model with a stable id."""

    return OrgModel(id=org_id or _new_id("org"), name=clean_label(name, what="org_model"))


def org_add_role(org: OrgModel, name: str, *, role_id: str | None = None) -> OrgModel:
    """Fügt eine Rolle hinzu (Validate-before-Commit).

    :param org: das unveränderte Ausgangsmodell (wird nie mutiert).
    :param name: Anzeigename, über ``clean_label`` bereinigt.
    :param role_id: explizite Kennung; sonst eine neue aus ``ORG_IDS``.
    :returns: die geprüfte Kopie mit der neuen Rolle.
    :raises CorrectnessError: ``OP.already-exists`` bei belegter Kennung.
    """

    candidate = org.model_copy(deep=True)
    rid = role_id or _new_id("role")
    if rid in candidate.roles:
        raise _already_exists("role", "role", rid)
    candidate.roles[rid] = Role(id=rid, name=clean_label(name, what="role"))
    return raise_if_invalid_org(candidate)


def org_add_unit(
    org: OrgModel,
    name: str,
    *,
    parent_id: str | None = None,
    org_unit_id: str | None = None,
    manager_id: str | None = None,
) -> OrgModel:
    """Fügt eine Organisationseinheit hinzu (Validate-before-Commit).

    :param org: das unveränderte Ausgangsmodell.
    :param name: Anzeigename, über ``clean_label`` bereinigt.
    :param parent_id: optionale übergeordnete Einheit (muss existieren).
    :param org_unit_id: explizite Kennung; sonst eine neue aus ``ORG_IDS``.
    :param manager_id: optionale:r Vorgesetzte:r (muss als Agent existieren).
    :returns: die geprüfte Kopie mit der neuen Einheit.
    :raises CorrectnessError: ``OP.already-exists`` bzw. ``OP.not-found`` in
        dieser Reihenfolge: Kennung, Elterneinheit, Vorgesetzte:r.
    """

    candidate = org.model_copy(deep=True)
    uid = org_unit_id or _new_id("unit")
    if uid in candidate.org_units:
        raise _already_exists("org unit", "org_unit", uid)
    _require_ref(parent_id, candidate.org_units, label="parent org unit", kind="org_unit")
    _require_ref(manager_id, candidate.agents, label="manager", kind="agent")
    candidate.org_units[uid] = OrgUnit(
        id=uid,
        name=clean_label(name, what="org_unit"),
        parent_id=parent_id,
        manager_id=manager_id,
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
    """Fügt einen Agenten (Person) hinzu (Validate-before-Commit).

    :param org: das unveränderte Ausgangsmodell.
    :param name: Anzeigename, über ``clean_label`` bereinigt.
    :param role_ids: Rollen der Person (jede muss existieren).
    :param org_unit_id: optionale Einheit (muss existieren).
    :param agent_id: explizite Kennung; sonst eine neue aus ``ORG_IDS``.
    :param deputy_id: optionale Vertretung (muss als Agent existieren).
    :param email: optionale Adresse; die Syntax prüft N1 vor dem Commit.
    :returns: die geprüfte Kopie mit dem neuen Agenten.
    :raises CorrectnessError: ``OP.already-exists`` bzw. ``OP.not-found`` in
        dieser Reihenfolge: Kennung, Rollen, Einheit, Vertretung.
    """

    candidate = org.model_copy(deep=True)
    aid = agent_id or _new_id("agent")
    if aid in candidate.agents:
        raise _already_exists("agent", "agent", aid)
    for role_id in role_ids or []:
        _require_ref(role_id, candidate.roles, label="role", kind="role")
    _require_ref(org_unit_id, candidate.org_units, label="org unit", kind="org_unit")
    _require_ref(deputy_id, candidate.agents, label="deputy", kind="agent")
    candidate.agents[aid] = Agent(
        id=aid,
        name=clean_label(name, what="agent"),
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
    """Ändert einen bestehenden Agenten teilweise (Validate-before-Commit).

    ``name``/``role_ids`` bleiben bei ``None`` unverändert; ``org_unit_id`` und
    ``email`` unterscheiden ``KEEP`` (unverändert) von ``None`` (entfernen).

    :param org: das unveränderte Ausgangsmodell.
    :param agent_id: der zu ändernde Agent (muss existieren).
    :returns: die geprüfte Kopie.
    :raises CorrectnessError: ``OP.not-found`` für Agent, Rolle oder Einheit.
    """

    candidate = org.model_copy(deep=True)
    agent = candidate.agents.get(agent_id)
    if agent is None:
        raise _not_found("agent", "agent", agent_id)
    if name is not None:
        agent.name = clean_label(name, what="agent")
    if role_ids is not None:
        for role_id in role_ids:
            _require_ref(role_id, candidate.roles, label="role", kind="role")
        agent.role_ids = list(role_ids)
    if not isinstance(org_unit_id, _KeepSentinel):
        _require_ref(org_unit_id, candidate.org_units, label="org unit", kind="org_unit")
        agent.org_unit_id = org_unit_id
    if not isinstance(email, _KeepSentinel):
        # ``None`` clears the address; a value is checked for well-formedness by
        # the validator (N1) before commit.
        agent.email = email
    return raise_if_invalid_org(candidate)


def org_set_manager(org: OrgModel, org_unit_id: str, manager_id: str | None) -> OrgModel:
    """Setzt (oder entfernt mit ``None``) die:den Vorgesetzte:n einer Einheit.

    :raises CorrectnessError: ``OP.not-found`` für Einheit oder Agent.
    """

    candidate = org.model_copy(deep=True)
    unit = candidate.org_units.get(org_unit_id)
    if unit is None:
        raise _not_found("org unit", "org_unit", org_unit_id)
    _require_ref(manager_id, candidate.agents, label="manager", kind="agent")
    unit.manager_id = manager_id
    return raise_if_invalid_org(candidate)


def org_set_parent(org: OrgModel, org_unit_id: str, parent_id: str | None) -> OrgModel:
    """Hängt eine Einheit um (oder mit ``None`` an die Wurzel).

    Vor dem Commit wird die Elternkette von ``parent_id`` aufwärts abgelaufen:
    Trifft sie ``org_unit_id``, entstünde ein Zyklus (``OP.org-cycle``).

    :raises CorrectnessError: ``OP.not-found`` für Einheit oder Elterneinheit,
        ``OP.org-cycle`` für Selbstbezug oder Zyklus.
    """

    candidate = org.model_copy(deep=True)
    unit = candidate.org_units.get(org_unit_id)
    if unit is None:
        raise _not_found("org unit", "org_unit", org_unit_id)
    if parent_id is not None:
        _require_ref(parent_id, candidate.org_units, label="parent org unit", kind="org_unit")
        if parent_id == org_unit_id:
            raise _fail("an org unit cannot be its own parent", code="OP.org-cycle")
        walker: str | None = parent_id
        while walker is not None:
            if walker == org_unit_id:
                raise _fail(
                    "setting this parent would create a cycle in the org hierarchy",
                    code="OP.org-cycle",
                )
            walker = candidate.org_units[walker].parent_id
    unit.parent_id = parent_id
    return raise_if_invalid_org(candidate)


def org_set_deputy(org: OrgModel, agent_id: str, deputy_id: str | None) -> OrgModel:
    """Setzt (oder entfernt mit ``None``) die Vertretung eines Agenten.

    :raises CorrectnessError: ``OP.not-found`` für Agent oder Vertretung,
        ``OP.own-deputy``, wenn die Person sich selbst vertreten soll (geprüft
        vor dem Vorhandensein der Vertretung).
    """

    candidate = org.model_copy(deep=True)
    agent = candidate.agents.get(agent_id)
    if agent is None:
        raise _not_found("agent", "agent", agent_id)
    if deputy_id is not None:
        if deputy_id == agent_id:
            raise _fail("an agent cannot be its own deputy", code="OP.own-deputy")
        _require_ref(deputy_id, candidate.agents, label="deputy", kind="agent")
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
        raise _not_found("role", "role", role_id)
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
        raise _not_found("org unit", "org_unit", org_unit_id)
    unit.mailbox = mailbox
    return raise_if_invalid_org(candidate)
