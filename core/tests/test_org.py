# SPDX-License-Identifier: BUSL-1.1
"""Shared, cross-schema organisation models (rule Z1 master data + reuse)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from procworks import (
    assign_staff_rule,
    create_empty_schema,
    create_org_model,
    link_org_model,
    org_add_agent,
    org_add_role,
    org_add_unit,
    org_set_parent,
    release,
    serial_insert,
    unlink_org_model,
    validate,
    validate_org,
)
from procworks.api import app
from procworks.assignment import eligible_agents
from procworks.execution import instantiate
from procworks.model import StaffRule, StaffRuleKind
from procworks.validator import CorrectnessError


def _activity_ids(schema, label):
    return [n.id for n in schema.nodes.values() if n.label == label]


# --- core: standalone org operations & validation ------------------------


def test_org_operations_build_consistent_model() -> None:
    org = create_org_model("Stadtverwaltung", org_id="org1")
    org = org_add_role(org, "Sachbearbeiter", role_id="sb")
    org = org_add_unit(org, "Einkauf", org_unit_id="einkauf")
    org = org_add_agent(org, "Erika", role_ids=["sb"], org_unit_id="einkauf", agent_id="a1")
    assert validate_org(org) == []
    assert org.agents["a1"].role_ids == ["sb"]


def test_org_add_agent_unknown_role_rejected() -> None:
    org = create_org_model("Org", org_id="org1")
    with pytest.raises(CorrectnessError):
        org_add_agent(org, "Erika", role_ids=["ghost"], agent_id="a1")


def test_org_set_parent_cycle_rejected() -> None:
    org = create_org_model("Org", org_id="org1")
    org = org_add_unit(org, "A", org_unit_id="a")
    org = org_add_unit(org, "B", parent_id="a", org_unit_id="b")
    with pytest.raises(CorrectnessError):
        org_set_parent(org, "a", "b")


# --- core: linking a schema to a shared org ------------------------------


def _shared_org():
    org = create_org_model("Org", org_id="org1")
    org = org_add_role(org, "Sachbearbeiter", role_id="sb")
    org = org_add_agent(org, "Erika", role_ids=["sb"], agent_id="a1")
    return org


def _linked_schema(org, schema_id="s1"):
    schema = create_empty_schema("Antrag", schema_id=schema_id)
    schema = serial_insert(schema, "Bearbeiten", after_node_id="start")
    schema = link_org_model(schema, org.id, org)
    act = _activity_ids(schema, "Bearbeiten")[0]
    schema = assign_staff_rule(schema, act, StaffRule(kind=StaffRuleKind.ROLE, ref="sb"))
    return schema, act


def test_linked_schema_resolves_against_shared_org() -> None:
    org = _shared_org()
    schema, act = _linked_schema(org)
    schema = release(schema)
    instance = instantiate(schema)
    assert "a1" in eligible_agents(schema, act, instance)


def test_link_requires_entwurf() -> None:
    org = _shared_org()
    schema, _ = _linked_schema(org)
    schema = release(schema)
    with pytest.raises(CorrectnessError):
        link_org_model(schema, org.id, org)


def test_editing_linked_schema_org_in_place_rejected() -> None:
    from procworks import add_role

    org = _shared_org()
    schema, _ = _linked_schema(org)
    with pytest.raises(CorrectnessError):
        add_role(schema, "Neu")


def test_unlink_keeps_local_org_copy() -> None:
    org = _shared_org()
    schema, act = _linked_schema(org)
    schema = unlink_org_model(schema)
    assert schema.org_model_id is None
    assert "sb" in schema.org_model.roles
    assert validate(schema) == []


# --- API: shared org reused across two schemas, live edits ---------------


client = TestClient(app)


def _make_act(sid: str, label: str) -> str:
    schema = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": label, "after_node_id": "start"},
    ).json()
    return next(n["id"] for n in schema["nodes"].values() if n["label"] == label)


def test_shared_org_used_in_two_schemas_via_api() -> None:
    org_id = "org_api1"
    r = client.post("/org-models", json={"name": "Verwaltung", "org_model_id": org_id})
    assert r.status_code == 201
    client.post(f"/org-models/{org_id}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(
        f"/org-models/{org_id}/agents",
        json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"},
    )

    sids = []
    for name in ("Antrag A", "Antrag B"):
        sid = client.post("/schemas", json={"name": name}).json()["id"]
        act = _make_act(sid, "Bearbeiten")
        link = client.post(f"/schemas/{sid}/org-model", json={"org_model_id": org_id})
        assert link.status_code == 200
        r = client.post(
            f"/schemas/{sid}/staff-rule",
            json={"node_id": act, "rule": {"kind": "ROLE", "ref": "sb"}},
        )
        assert r.status_code == 200
        sids.append(sid)

    # A live edit on the shared org (add a second agent with the role) is
    # immediately visible to both schemas.
    client.post(
        f"/org-models/{org_id}/agents",
        json={"name": "Max", "role_ids": ["sb"], "agent_id": "a2"},
    )
    for sid in sids:
        schema = client.get(f"/schemas/{sid}").json()
        assert set(schema["org_model"]["agents"]) == {"a1", "a2"}


def test_org_edit_breaking_referencing_schema_is_rejected() -> None:
    org_id = "org_api2"
    client.post("/org-models", json={"name": "Org", "org_model_id": org_id})
    client.post(f"/org-models/{org_id}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(
        f"/org-models/{org_id}/agents",
        json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"},
    )

    sid = client.post("/schemas", json={"name": "Antrag"}).json()["id"]
    act = _make_act(sid, "Bearbeiten")
    client.post(f"/schemas/{sid}/org-model", json={"org_model_id": org_id})
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": act, "rule": {"kind": "ROLE", "ref": "sb"}},
    )
    client.post(f"/schemas/{sid}/release", json={})

    # Removing the only sb-agent's role would make the staff rule unresolvable
    # (Z2) in the referencing schema -> rejected, org unchanged.
    r = client.patch(f"/org-models/{org_id}/agents/a1", json={"role_ids": []})
    assert r.status_code == 422
    assert client.get(f"/org-models/{org_id}").json()["agents"]["a1"]["role_ids"] == ["sb"]
