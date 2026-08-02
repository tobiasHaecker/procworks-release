# SPDX-License-Identifier: BUSL-1.1
"""Tests for the Stufe-B release-readiness check (rule B2).

Stufe A (K/D/Z/…) is an *invariant*: it holds after every single operation, so a
draft can never be structurally broken. Stufe B is a different kind of statement
-- "this schema is not merely correct, it is also runnable" -- and a half-built
draft is allowed to fail it (concept §1.1.1, §3.4).

The rule implemented today is **B2**: every interactive step carries a staff rule
(BZR). Without one the step *is* activated at runtime, but
:func:`procworks.assignment.open_tasks` skips it, so it appears in nobody's
worklist -- the process looks started and stuck. These tests pin both halves:
what the check flags, and what it deliberately leaves alone.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from procworks import (
    AccessMode,
    DataType,
    NodeType,
    StaffRule,
    StaffRuleKind,
    add_agent,
    add_data_element,
    add_role,
    assign_service,
    assign_staff_rule,
    check_executable,
    connect_data,
    create_empty_schema,
    insert_subprocess,
    release,
    serial_insert,
    set_automation,
    validate,
)
from procworks.api import app
from procworks.demo import load_demo
from procworks.demo_o2c import load_o2c
from procworks.model import AutomationKind, LifecycleState, ProcessSchema
from procworks.templates import builtin_templates


def _nid(schema: ProcessSchema, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _with_actor(schema: ProcessSchema) -> ProcessSchema:
    """Add a role plus one bearer, so a ROLE staff rule is satisfiable (Z2)."""

    schema = add_role(schema, "Team", role_id="r-team")
    return add_agent(schema, "Alex Test", role_ids=["r-team"], agent_id="a-alex")


def _role_rule() -> StaffRule:
    return StaffRule(kind=StaffRuleKind.ROLE, ref="r-team")


# --- what B2 flags ---------------------------------------------------------


def test_interactive_step_without_staff_rule_is_not_releasable() -> None:
    """The core case: a step nobody is assigned to would stall the instance."""

    schema = serial_insert(create_empty_schema("Ohne BZR"), "Prüfen", "start")

    findings = check_executable(schema)

    assert [f.rule for f in findings] == ["B2"]
    assert findings[0].node_id == _nid(schema, "Prüfen")
    assert "Prüfen" in findings[0].message


def test_every_unstaffed_step_is_reported_individually() -> None:
    """Each missing assignment is localized, so the editor can point at it."""

    schema = serial_insert(create_empty_schema("Zwei"), "B", "start")
    schema = serial_insert(schema, "A", "start")

    findings = check_executable(schema)

    assert {f.node_id for f in findings} == {_nid(schema, "A"), _nid(schema, "B")}
    assert all(f.rule == "B2" for f in findings)


def test_staffed_step_is_releasable() -> None:
    schema = serial_insert(create_empty_schema("Mit BZR"), "Prüfen", "start")
    schema = _with_actor(schema)
    schema = assign_staff_rule(schema, _nid(schema, "Prüfen"), _role_rule())

    assert check_executable(schema) == []


def test_empty_process_is_releasable() -> None:
    """START -> END carries no interactive step, so there is nothing to staff."""

    assert check_executable(create_empty_schema("Leer")) == []


# --- what B2 deliberately leaves alone -------------------------------------


def test_automatic_step_needs_no_staff_rule() -> None:
    """An automated step has no performer -- Z4 even forbids a BZR on it."""

    schema = serial_insert(create_empty_schema("Automatisch"), "Buchen", "start")
    node = _nid(schema, "Buchen")
    schema = assign_service(schema, node, "Buchungsdienst", automatic=True)

    assert check_executable(schema) == []


def test_external_task_step_needs_no_staff_rule() -> None:
    """A step driven by an outside worker (E11) is automatic in the same sense."""

    schema = serial_insert(create_empty_schema("External"), "Bonität", "start")
    node = _nid(schema, "Bonität")
    schema = assign_service(schema, node, "Scoring", automatic=True)
    schema = set_automation(
        schema, node, AutomationKind.EXTERNAL_TASK, topic="scoring"
    )

    assert check_executable(schema) == []


def test_interactive_service_binding_still_needs_a_staff_rule() -> None:
    """A *non*-automatic service is an interactive step and must be assigned."""

    schema = serial_insert(create_empty_schema("Interaktiv"), "Prüfen", "start")
    node = _nid(schema, "Prüfen")
    schema = assign_service(schema, node, "Maske", automatic=False)

    findings = check_executable(schema)

    assert [f.node_id for f in findings] == [node]


def test_subprocess_node_needs_no_staff_rule() -> None:
    """A SUBPROCESS delegates to its child; the child's steps carry the BZRs."""

    child = serial_insert(create_empty_schema("Kind", schema_id="kind"), "Tun", "start")
    child = _with_actor(child)
    child = assign_staff_rule(child, _nid(child, "Tun"), _role_rule())
    child = release(child)

    def resolver(schema_id: str, version: int | None) -> ProcessSchema | None:
        return child if schema_id == "kind" else None

    parent = insert_subprocess(
        create_empty_schema("Eltern"), "start", "kind", child.version, resolver=resolver
    )

    assert any(n.type is NodeType.SUBPROCESS for n in parent.nodes.values())
    assert check_executable(parent) == []


# --- Stufe A and Stufe B are independent statements ------------------------


def test_a_correct_schema_can_be_unreleasable() -> None:
    """The whole point of the split: structurally perfect, not yet runnable.

    A draft may sit here indefinitely -- that is the "Angebotsmodell", not an
    error state. Only the release is gated (staged; see the concept §3.4).
    """

    schema = serial_insert(create_empty_schema("Halbfertig"), "Prüfen", "start")
    schema = add_data_element(schema, "Betrag", DataType.INTEGER, element_id="betrag")
    schema = connect_data(schema, _nid(schema, "Prüfen"), "betrag", AccessMode.WRITE)

    assert validate(schema) == []          # Stufe A: correct
    assert check_executable(schema) != []  # Stufe B: not yet runnable


def test_release_readiness_never_blocks_editing() -> None:
    """An unreleasable draft stays fully editable -- B2 is not an operation gate."""

    schema = serial_insert(create_empty_schema("Weiterbauen"), "A", "start")
    assert check_executable(schema) != []

    schema = serial_insert(schema, "B", _nid(schema, "A"))

    assert schema.lifecycle_state is LifecycleState.ENTWURF
    assert validate(schema) == []


# --- the shipped corpus must stay releasable -------------------------------


def test_every_built_in_template_is_releasable() -> None:
    """A customer instantiates a template and releases it -- that must work.

    Guards the built-in library against a blueprint that would walk a customer
    into a stalled process on their first try.
    """

    for template in builtin_templates():
        findings = check_executable(template.blueprint)
        assert findings == [], f"template '{template.id}': {findings}"


@pytest.mark.parametrize("loader", [load_demo, load_o2c], ids=["basis", "o2c"])
def test_every_demo_schema_is_releasable(loader: object) -> None:
    """The example data is the product's shop window -- it must clear Stufe B.

    Both cosmoses ship released, so this is a regression guard: it keeps the
    corpus B2-clean while the gate in ``operations.release`` is still staged, and
    it is what proved the gate can be switched on without touching the product's
    own processes.
    """

    from procworks.audit import InMemoryAuditLog
    from procworks.store import (
        InMemoryInstanceStore,
        InMemoryOrgStore,
        InMemorySchemaStore,
        hydrate_org,
        make_org_resolver,
    )

    schemas = InMemorySchemaStore()
    orgs = InMemoryOrgStore()
    loader(  # type: ignore[operator]
        schema_store=schemas,
        instance_store=InMemoryInstanceStore(),
        org_store=orgs,
        audit_log=InMemoryAuditLog(),
    )
    resolve_org = make_org_resolver(orgs)

    for schema_id in schemas.list_ids():
        stored = schemas.get(schema_id)
        assert stored is not None
        findings = check_executable(hydrate_org(stored, resolve_org))
        assert findings == [], f"demo schema '{schema_id}': {findings}"


# --- the API reports both levels separately --------------------------------


def test_validation_endpoint_reports_release_readiness() -> None:
    """``GET /schemas/{id}/validation`` carries Stufe A and Stufe B side by side."""

    client = TestClient(app)
    schema = client.post("/schemas", json={"name": "Freigabe-Reife"}).json()
    client.post(
        f"/schemas/{schema['id']}/serial-insert",
        json={"label": "Prüfen", "after_node_id": "start"},
    )

    report = client.get(f"/schemas/{schema['id']}/validation").json()

    assert report["correct"] is True          # Stufe A holds …
    assert report["releasable"] is False      # … Stufe B does not (yet)
    assert [f["rule"] for f in report["release_findings"]] == ["B2"]
    assert report["findings"] == []


def test_validation_endpoint_reports_releasable_once_staffed() -> None:
    client = TestClient(app)
    schema = client.post("/schemas", json={"name": "Vollständig"}).json()
    sid = schema["id"]
    node = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Prüfen", "after_node_id": "start"},
    ).json()
    node_id = next(
        n["id"] for n in node["nodes"].values() if n.get("label") == "Prüfen"
    )
    client.post(f"/schemas/{sid}/roles", json={"name": "Team", "role_id": "r-team"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Alex", "role_ids": ["r-team"], "agent_id": "a-alex"},
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": node_id, "rule": {"kind": "ROLE", "ref": "r-team"}},
    )

    report = client.get(f"/schemas/{sid}/validation").json()

    assert report["correct"] is True
    assert report["releasable"] is True
    assert report["release_findings"] == []
