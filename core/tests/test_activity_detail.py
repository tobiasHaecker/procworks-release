# SPDX-License-Identifier: BUSL-1.1
"""E2: activity detail states (Aktivitaets-Detailzustaende-Konzept).

SUSPENDED/FAILED live as an overlay over the untouched base marking (V1-V4):
only the owner pauses/continues/fails, a suspended step must resume before
completion, a failed step freezes until its recovery reset -- which clears
overlay + claim + escalation ladder and re-offers the step with a freshly
stamped clock. Rule B refines the time view: once claimed, a modelled
processing duration is measured from the claim instant -- shared by the
worklist bands and the escalation sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from procworks import (
    NodeDetailState,
    NodeState,
    StaffRule,
    StaffRuleKind,
    add_agent,
    add_role,
    assign_staff_rule,
    complete_activity,
    create_empty_schema,
    fail_activity,
    instantiate,
    release,
    reset_activity,
    resume_activity,
    return_activity,
    serial_insert,
    suspend_activity,
)
from procworks import api as api_module
from procworks.execution import ExecutionError
from procworks.model import TimeConstraint
from procworks.worklist_priority import TimeContext, assess

client = TestClient(api_module.app)

ANNA = "a-anna"
BERT = "a-bert"


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _two_agent_schema():
    schema = create_empty_schema("Detail")
    schema = serial_insert(schema, "Pruefen", after_node_id="start")
    schema = add_role(schema, "SB", role_id="r-sb")
    schema = add_agent(schema, "Anna", role_ids=["r-sb"], agent_id=ANNA)
    schema = add_agent(schema, "Bert", role_ids=["r-sb"], agent_id=BERT)
    schema = assign_staff_rule(
        schema, _nid(schema, "Pruefen"), StaffRule(kind=StaffRuleKind.ROLE, ref="r-sb")
    )
    return release(schema)


# --- engine: V1-V4 ---------------------------------------------------------


def test_suspend_blocks_completion_until_resume_v1_v2() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)

    inst = suspend_activity(inst, schema, node, ANNA)  # implicit claim + start
    assert inst.node_states[node] is NodeState.RUNNING  # base marking untouched
    assert inst.node_details[node] is NodeDetailState.SUSPENDED
    assert inst.claimed_by[node] == ANNA

    with pytest.raises(ExecutionError):  # §4: finish only from RUNNING
        complete_activity(inst, schema, node, agent_id=ANNA)
    with pytest.raises(ExecutionError):  # only the owner resumes (V2)
        resume_activity(inst, schema, node, BERT)

    inst = resume_activity(inst, schema, node, ANNA)
    assert node not in inst.node_details
    done = complete_activity(inst, schema, node, agent_id=ANNA)
    assert done.node_states[node] is NodeState.COMPLETED


def test_fail_freezes_the_step_until_reset_v3_v4() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)

    inst = fail_activity(inst, schema, node, ANNA, "Unterlagen fehlen")
    assert inst.node_details[node] is NodeDetailState.FAILED
    assert inst.node_detail_reason[node] == "Unterlagen fehlen"

    with pytest.raises(ExecutionError):  # frozen: no completion
        complete_activity(inst, schema, node, agent_id=ANNA)
    with pytest.raises(ExecutionError):  # frozen: no pause either
        suspend_activity(inst, schema, node, ANNA)
    with pytest.raises(ExecutionError):  # no silent return -- reset instead
        return_activity(inst, schema, node, ANNA)
    with pytest.raises(ExecutionError) as err:  # a stranger cannot reset
        reset_activity(inst, schema, node, BERT)
    assert "V4" in str(err.value)

    back = reset_activity(inst, schema, node, ANNA)
    assert back.node_states[node] is NodeState.ACTIVATED  # fresh offer
    assert node not in back.node_details
    assert node not in back.node_detail_reason
    assert node not in back.claimed_by
    # The supervisory override works regardless of caller identity.
    failed_again = fail_activity(back, schema, node, BERT, "zweiter Versuch")
    forced = reset_activity(failed_again, schema, node, ANNA, force=True)
    assert node not in forced.node_details


def test_suspended_step_must_resume_before_failing_v3() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)
    inst = suspend_activity(inst, schema, node, ANNA)

    with pytest.raises(ExecutionError):
        fail_activity(inst, schema, node, ANNA, "geht nicht")


def test_return_of_a_suspended_step_clears_the_pause() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)
    inst = suspend_activity(inst, schema, node, ANNA)

    back = return_activity(inst, schema, node, ANNA)
    assert node not in back.node_details
    assert back.node_states[node] is NodeState.ACTIVATED


# --- rule B: processing clock from the claim -------------------------------


def test_rule_b_measures_the_duration_from_the_claim() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    schema = schema.model_copy(deep=True)
    schema.time_constraints[node] = TimeConstraint(
        max_duration_seconds=100, target_lead_seconds=60
    )
    t0 = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)

    # Unclaimed: reaction clock from activation -> overdue after 60s.
    unclaimed = TimeContext(now=t0 + timedelta(seconds=100), activated_at={node: t0})
    assert assess(schema, node, unclaimed).criticality.value == "OVERDUE"

    # Claimed at t0+50: processing clock (100s) from the claim -> 50% -> WARNING.
    claimed = TimeContext(
        now=t0 + timedelta(seconds=100),
        activated_at={node: t0},
        claimed_at={node: t0 + timedelta(seconds=50)},
    )
    view = assess(schema, node, claimed)
    assert view.criticality.value == "WARNING"
    assert view.due_at == t0 + timedelta(seconds=150)


# --- boundary: endpoints, sweep coupling -----------------------------------


def _api_world(*, pause_stops_clock: bool = False) -> tuple[str, str]:
    sid = client.post("/schemas", json={"name": "Detail-API"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Pruefen", "after_node_id": "start"},
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node = next(nid for nid, n in nodes.items() if n.get("label") == "Pruefen")
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "r-sb"})
    client.post(f"/schemas/{sid}/roles", json={"name": "Sr", "role_id": "r-senior"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Anna", "role_ids": ["r-sb"], "agent_id": ANNA},
    )
    client.post(
        f"/schemas/{sid}/agents",
        json={
            "name": "Bert",
            "role_ids": ["r-senior"],
            "agent_id": BERT,
            "email": "bert@example.org",
        },
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": node, "rule": {"kind": "ROLE", "ref": "r-sb"}},
    )
    client.post(
        f"/schemas/{sid}/time-constraint",
        json={
            "node_id": node,
            "constraint": {
                "target_lead_seconds": 60,
                "max_duration_seconds": 3600,
                "pause_stops_clock": pause_stops_clock,
            },
        },
    )
    client.post(
        f"/schemas/{sid}/escalation-policy",
        json={
            "node_id": node,
            "policy": {
                "stages": [
                    {
                        "after_seconds": 0,
                        "kind": "FUNCTIONAL",
                        "rule": {"kind": "ROLE", "ref": "r-senior"},
                    }
                ]
            },
        },
    )
    client.post(f"/schemas/{sid}/release")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    return node, iid


def test_detail_endpoints_and_audit_via_api() -> None:
    node, iid = _api_world()

    resp = client.post(f"/instances/{iid}/suspend", json={"node_id": node, "agent_id": ANNA})
    assert resp.status_code == 200
    assert resp.json()["node_details"][node] == "SUSPENDED"
    # The owner's list shows the pause.
    mine = [t for t in client.get(f"/agents/{ANNA}/tasks").json() if t["node_id"] == node]
    assert mine and mine[0]["detail"] == "SUSPENDED"

    resp = client.post(f"/instances/{iid}/complete", json={"node_id": node, "agent_id": ANNA})
    assert resp.status_code == 409  # resume first

    client.post(f"/instances/{iid}/resume", json={"node_id": node, "agent_id": ANNA})
    resp = client.post(f"/instances/{iid}/fail",
        json={"node_id": node, "agent_id": ANNA, "reason": "Unterlagen fehlen"})
    assert resp.status_code == 200
    assert resp.json()["node_detail_reason"][node] == "Unterlagen fehlen"

    before = client.get(f"/instances/{iid}").json()
    resp = client.post(f"/instances/{iid}/reset", json={"node_id": node, "agent_id": ANNA})
    assert resp.status_code == 200
    after = resp.json()
    assert after["node_states"][node] == "ACTIVATED"
    assert after["claimed_by"] == {}
    assert after["node_activated_at"][node] != before["node_activated_at"][node]

    events = [e["event_type"] for e in client.get(f"/instances/{iid}/audit").json()]
    for expected in (
        "ACTIVITY_SUSPENDED",
        "ACTIVITY_RESUMED",
        "ACTIVITY_FAILED",
        "ACTIVITY_RESET",
    ):
        assert expected in events
    failed = next(
        e for e in client.get(f"/instances/{iid}/audit").json()
        if e["event_type"] == "ACTIVITY_FAILED"
    )
    assert failed["detail"]["reason"] == "Unterlagen fehlen"


def test_sweep_respects_rule_b_and_skips_failed_steps() -> None:
    node, iid = _api_world()
    inst = client.get(f"/instances/{iid}").json()
    activated = datetime.fromisoformat(inst["node_activated_at"][node])

    # Claim: the processing clock (3600s from the claim) replaces the 60s
    # reaction clock -- the stage no longer fires at activation+61. Assertions
    # are per-instance: the sweep spans the whole (shared) store.
    client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": ANNA})
    claimed = datetime.fromisoformat(
        client.get(f"/instances/{iid}").json()["node_claimed_at"][node]
    )
    api_module._escalation_sweep(now=activated + timedelta(seconds=61))
    assert client.get(f"/instances/{iid}").json()["escalated_stages"].get(node, 0) == 0
    # Past the processing due instant it fires.
    api_module._escalation_sweep(now=claimed + timedelta(seconds=3601))
    assert client.get(f"/instances/{iid}").json()["escalated_stages"].get(node) == 1

    # A failed step never escalates further.
    node2, iid2 = _api_world()
    client.post(
        f"/instances/{iid2}/fail",
        json={"node_id": node2, "agent_id": ANNA, "reason": "kaputt"},
    )
    api_module._escalation_sweep(now=datetime.now(UTC) + timedelta(days=30))
    tasks2 = client.get(f"/instances/{iid2}/tasks").json()
    assert [t["escalated_stage"] for t in tasks2 if t["node_id"] == node2] == [0]


# --- net time (E2 Stufe C): boundary bookkeeping + sweep coupling ----------


def test_suspend_resume_books_the_pause_at_the_boundary() -> None:
    node, iid = _api_world(pause_stops_clock=True)
    client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": ANNA})

    paused = client.post(
        f"/instances/{iid}/suspend", json={"node_id": node, "agent_id": ANNA}
    ).json()
    assert node in paused["node_suspended_at"]  # pause start stamped
    opened = paused["node_suspended_at"][node]

    # An idempotent re-suspend must not restart the ongoing pause.
    again = client.post(
        f"/instances/{iid}/suspend", json={"node_id": node, "agent_id": ANNA}
    ).json()
    assert again["node_suspended_at"][node] == opened

    resumed = client.post(
        f"/instances/{iid}/resume", json={"node_id": node, "agent_id": ANNA}
    ).json()
    assert node not in resumed["node_suspended_at"]  # interval closed...
    assert resumed["node_paused_seconds"][node] >= 0.0  # ...and credited

    # Completion clears the bookkeeping -- credit belongs to one activation.
    done = client.post(
        f"/instances/{iid}/complete", json={"node_id": node, "agent_id": ANNA}
    ).json()
    assert node not in done["node_paused_seconds"]
    assert node not in done["node_suspended_at"]


def test_sweep_defers_by_pause_credit_only_with_the_opt_in() -> None:
    node, iid = _api_world(pause_stops_clock=True)
    client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": ANNA})
    claimed = datetime.fromisoformat(
        client.get(f"/instances/{iid}").json()["node_claimed_at"][node]
    )
    # Deterministic pause credit: book 7200s directly into the store (the
    # HTTP round-trip would only accumulate milliseconds).
    inst = api_module._instances.get(iid)
    assert inst is not None
    inst.node_paused_seconds[node] = 7200.0
    api_module._instances.put(inst)

    # Past the gross due instant (claim + 3600s) nothing fires -- the pause
    # credit defers the due instant for bands AND sweep alike.
    api_module._escalation_sweep(now=claimed + timedelta(seconds=3601))
    assert client.get(f"/instances/{iid}").json()["escalated_stages"].get(node, 0) == 0
    # Past claim + 3600s + credit it fires.
    api_module._escalation_sweep(now=claimed + timedelta(seconds=3600 + 7200 + 1))
    assert client.get(f"/instances/{iid}").json()["escalated_stages"].get(node) == 1

    # Without the opt-in the same booked credit changes nothing (default).
    node2, iid2 = _api_world()
    client.post(f"/instances/{iid2}/claim", json={"node_id": node2, "agent_id": ANNA})
    claimed2 = datetime.fromisoformat(
        client.get(f"/instances/{iid2}").json()["node_claimed_at"][node2]
    )
    inst2 = api_module._instances.get(iid2)
    assert inst2 is not None
    inst2.node_paused_seconds[node2] = 7200.0
    api_module._instances.put(inst2)
    api_module._escalation_sweep(now=claimed2 + timedelta(seconds=3601))
    assert client.get(f"/instances/{iid2}").json()["escalated_stages"].get(node2) == 1
