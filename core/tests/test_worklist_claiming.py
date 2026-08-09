# SPDX-License-Identifier: BUSL-1.1
"""E1: worklist state machine -- claim ("uebernehmen") and return
("zuruecklegen"), Arbeitslisten-Zustandsmaschine-Konzept.

The runtime guards W1-W4: at most one owner per activated step (W1), claiming
and completing a claimed step require eligibility/ownership (W2), returning is
owner-or-supervisory and reopens the offer (W3), and an ownership entry never
survives the activation it was made for -- completion and the loop-iteration
reset clear it, automatic steps cannot be claimed at all (W4). Claiming stays
optional: the one-click completion of an unclaimed step keeps working.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

from procworks import (
    DataType,
    InstanceState,
    NodeState,
    StaffRule,
    StaffRuleKind,
    add_agent,
    add_data_element,
    add_role,
    assign_service,
    assign_staff_rule,
    claim_activity,
    complete_activity,
    create_empty_schema,
    insert_loop,
    instantiate,
    open_tasks,
    release,
    return_activity,
    serial_insert,
    start_activity,
)
from procworks.api import app
from procworks.execution import ExecutionError

client = TestClient(app)

ANNA = "a-anna"
BERT = "a-bert"
CARL = "a-carl"  # carries no role -> never eligible


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _two_agent_schema():
    """start -> Pruefen (ROLE r-sb: Anna + Bert) -> end, released."""

    schema = create_empty_schema("Freigabe")
    schema = serial_insert(schema, "Pruefen", after_node_id="start")
    schema = add_role(schema, "Sachbearbeitung", role_id="r-sb")
    schema = add_role(schema, "Gast", role_id="r-gast")
    schema = add_agent(schema, "Anna", role_ids=["r-sb"], agent_id=ANNA)
    schema = add_agent(schema, "Bert", role_ids=["r-sb"], agent_id=BERT)
    schema = add_agent(schema, "Carl", role_ids=["r-gast"], agent_id=CARL)
    schema = assign_staff_rule(
        schema,
        _nid(schema, "Pruefen"),
        StaffRule(kind=StaffRuleKind.ROLE, ref="r-sb"),
    )
    return release(schema)


# --- engine: W1-W4 ---------------------------------------------------------


def test_claim_is_exclusive_and_idempotent_w1() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)

    inst = claim_activity(inst, schema, node, ANNA)
    assert inst.claimed_by[node] == ANNA

    # Re-claim by the owner is a no-op; a second claimant is refused.
    again = claim_activity(inst, schema, node, ANNA)
    assert again.claimed_by[node] == ANNA
    with pytest.raises(ExecutionError) as err:
        claim_activity(inst, schema, node, BERT)
    assert "W1" in str(err.value)


def test_claim_requires_eligibility_w2() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)

    with pytest.raises(ExecutionError) as err:
        claim_activity(inst, schema, node, CARL)
    assert "W2" in str(err.value)


def test_only_the_owner_completes_a_claimed_step_w2() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)
    inst = claim_activity(inst, schema, node, ANNA)

    with pytest.raises(ExecutionError):
        complete_activity(inst, schema, node, agent_id=BERT)
    with pytest.raises(ExecutionError):  # anonymous completion is refused too
        complete_activity(inst, schema, node)

    done = complete_activity(inst, schema, node, agent_id=ANNA)
    assert done.state is InstanceState.COMPLETED
    assert node not in done.claimed_by  # W4: completion clears the entry


def test_completing_an_unclaimed_step_stays_allowed() -> None:
    """Claiming is optional -- the one-click flow keeps working."""

    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)
    done = complete_activity(inst, schema, node, agent_id=BERT)
    assert done.state is InstanceState.COMPLETED


def test_return_reopens_the_offer_w3() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)
    inst = claim_activity(inst, schema, node, ANNA)

    with pytest.raises(ExecutionError) as err:  # a stranger cannot return
        return_activity(inst, schema, node, BERT)
    assert "W3" in str(err.value)

    back = return_activity(inst, schema, node, ANNA)
    assert node not in back.claimed_by
    # The supervisory override works regardless of the caller identity.
    inst = claim_activity(inst, schema, node, ANNA)
    forced = return_activity(inst, schema, node, BERT, force=True)
    assert node not in forced.claimed_by


def test_start_presupposes_ownership_and_return_resets_running_w4() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)

    started = start_activity(inst, schema, node, ANNA)  # implicit claim
    assert started.node_states[node] is NodeState.RUNNING
    assert started.claimed_by[node] == ANNA
    with pytest.raises(ExecutionError):  # claimed by Anna -> Bert cannot start
        start_activity(started, schema, node, BERT)

    back = return_activity(started, schema, node, ANNA)
    assert back.node_states[node] is NodeState.ACTIVATED  # Started -> Offered
    assert node not in back.claimed_by


def test_automatic_steps_cannot_be_claimed_w4() -> None:
    schema = create_empty_schema("Auto")
    schema = serial_insert(schema, "Roboter", after_node_id="start")
    schema = assign_service(schema, _nid(schema, "Roboter"), "Robot", automatic=True)
    schema = release(schema)
    inst = instantiate(schema)

    with pytest.raises(ExecutionError) as err:
        claim_activity(inst, schema, _nid(schema, "Roboter"), ANNA)
    assert "W4" in str(err.value)


def test_loop_iteration_reset_clears_the_claim_w4() -> None:
    """Every loop round is a fresh offer to all eligible agents."""

    schema = create_empty_schema("Schleife")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "na", DataType.BOOLEAN, element_id="na")
    schema = insert_loop(schema, _nid(schema, "Erfassen"), "Nacharbeit", discriminator="na")
    schema = add_role(schema, "Sachbearbeitung", role_id="r-sb")
    schema = add_agent(schema, "Anna", role_ids=["r-sb"], agent_id=ANNA)
    for label in ("Erfassen", "Nacharbeit"):
        schema = assign_staff_rule(
            schema, _nid(schema, label), StaffRule(kind=StaffRuleKind.ROLE, ref="r-sb")
        )
    schema = release(schema)
    body = _nid(schema, "Nacharbeit")

    inst = instantiate(schema)
    inst = complete_activity(inst, schema, _nid(schema, "Erfassen"), agent_id=ANNA)
    inst = claim_activity(inst, schema, body, ANNA)
    inst = complete_activity(inst, schema, body, {"na": True}, agent_id=ANNA)
    assert inst.node_states[body] is NodeState.ACTIVATED  # next round
    assert body not in inst.claimed_by  # the claim did not survive the reset


def test_open_tasks_carries_the_owner() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)
    assert open_tasks(schema, inst)[0].claimed_by is None

    inst = claim_activity(inst, schema, node, ANNA)
    task = open_tasks(schema, inst)[0]
    assert task.claimed_by == ANNA  # instance-wide view stays complete


# --- API boundary: endpoints, withdrawn filter, stamp, audit ---------------


def _api_world() -> tuple[str, str, str]:
    """Schema + instance over HTTP; returns (schema_id, instance_id, node_id)."""

    sid = client.post("/schemas", json={"name": "Claim-API"}).json()["id"]
    client.post(
        "/schemas/" + sid + "/serial-insert",
        json={"label": "Pruefen", "after_node_id": "start"},
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node = next(nid for nid, n in nodes.items() if n.get("label") == "Pruefen")
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "r-sb"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Anna", "role_ids": ["r-sb"], "agent_id": ANNA},
    )
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Bert", "role_ids": ["r-sb"], "agent_id": BERT},
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": node, "rule": {"kind": "ROLE", "ref": "r-sb"}},
    )
    staff_via_api(client, sid)  # no-op safety for any future extra step
    client.post(f"/schemas/{sid}/release")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    return sid, node, iid


def test_claim_endpoint_withdraws_stamps_and_audits() -> None:
    _sid, node, iid = _api_world()

    assert any(t["node_id"] == node for t in client.get(f"/agents/{BERT}/tasks").json())

    resp = client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": ANNA})
    assert resp.status_code == 200
    body = resp.json()
    assert body["claimed_by"][node] == ANNA
    assert node in body["node_claimed_at"]  # boundary stamp

    # Withdrawn view: gone for Bert, kept (and marked) for Anna.
    assert not any(
        t["node_id"] == node for t in client.get(f"/agents/{BERT}/tasks").json()
    )
    mine = [t for t in client.get(f"/agents/{ANNA}/tasks").json() if t["node_id"] == node]
    assert mine and mine[0]["claimed_by"] == ANNA

    # The instance-wide list stays complete and names the owner.
    all_tasks = client.get(f"/instances/{iid}/tasks").json()
    assert [t["claimed_by"] for t in all_tasks if t["node_id"] == node] == [ANNA]

    # A second claim conflicts (W1) -> 409, and the audit trail has the claim.
    resp = client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": BERT})
    assert resp.status_code == 409
    events = client.get(f"/instances/{iid}/audit").json()
    assert any(
        e["event_type"] == "ACTIVITY_CLAIMED" and e["agent_id"] == ANNA for e in events
    )


def test_return_endpoint_reopens_and_audits() -> None:
    _sid, node, iid = _api_world()
    client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": ANNA})

    resp = client.post(f"/instances/{iid}/return", json={"node_id": node, "agent_id": ANNA})
    assert resp.status_code == 200
    assert resp.json()["claimed_by"] == {}
    assert any(
        t["node_id"] == node for t in client.get(f"/agents/{BERT}/tasks").json()
    )
    events = client.get(f"/instances/{iid}/audit").json()
    assert any(e["event_type"] == "ACTIVITY_RETURNED" for e in events)

    # Returning an unclaimed step is a 409 (nothing to return).
    resp = client.post(f"/instances/{iid}/return", json={"node_id": node, "agent_id": ANNA})
    assert resp.status_code == 409


def test_claimed_step_completes_only_for_the_owner_via_api() -> None:
    _sid, node, iid = _api_world()
    client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": ANNA})

    resp = client.post(
        f"/instances/{iid}/complete", json={"node_id": node, "agent_id": BERT}
    )
    assert resp.status_code == 409

    resp = client.post(
        f"/instances/{iid}/complete", json={"node_id": node, "agent_id": ANNA}
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "COMPLETED"


def test_claim_without_agent_identity_is_a_422() -> None:
    _sid, node, iid = _api_world()
    resp = client.post(f"/instances/{iid}/claim", json={"node_id": node})
    assert resp.status_code == 422
