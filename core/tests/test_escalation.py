# SPDX-License-Identifier: BUSL-1.1
"""T3/E9: modelled overdue reactions (Eskalations-Konzept).

Validator side: a policy is well-formed and decidable (T3a-T3c) or cannot be
stored at all. Runtime side: the lazy boundary sweep fires due stages at most
once per activation -- FUNCTIONAL broadens the eligible set (and informs the
added performers), HIERARCHICAL informs a leadership set without making it
eligible; every fired stage leaves a ``TASK_ESCALATED`` audit event and the
mail outbox dedups per ``esc|instance|node|stage|activation``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from procworks import (
    DataType,
    EscalationKind,
    EscalationPolicy,
    EscalationStage,
    NodeState,
    StaffRule,
    StaffRuleKind,
    add_agent,
    add_data_element,
    add_role,
    assign_staff_rule,
    complete_activity,
    create_empty_schema,
    delete_node,
    insert_loop,
    instantiate,
    release,
    serial_insert,
    set_escalation_policy,
    set_mail_binding,
    set_time_constraint,
    update_agent,
    validate,
)
from procworks import api as api_module
from procworks.model import MailBinding, TimeConstraint
from procworks.operations import CorrectnessError

client = TestClient(api_module.app)


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _staffed_timed_schema():
    """start -> Pruefen (ROLE r-sb: Anna; Soll-Zeit 60s) -> end, draft."""

    schema = create_empty_schema("Frist")
    schema = serial_insert(schema, "Pruefen", after_node_id="start")
    schema = add_role(schema, "Sachbearbeitung", role_id="r-sb")
    schema = add_role(schema, "Senior", role_id="r-senior")
    schema = add_role(schema, "Leitung", role_id="r-leitung")
    schema = add_agent(schema, "Anna", role_ids=["r-sb"], agent_id="a-anna")
    schema = add_agent(schema, "Bert", role_ids=["r-senior"], agent_id="a-bert")
    schema = add_agent(schema, "Lena", role_ids=["r-leitung"], agent_id="a-lena")
    node = _nid(schema, "Pruefen")
    schema = assign_staff_rule(
        schema, node, StaffRule(kind=StaffRuleKind.ROLE, ref="r-sb")
    )
    return set_time_constraint(
        schema, node, TimeConstraint(target_lead_seconds=60)
    )


def _stage(after: float, kind: EscalationKind, role: str) -> EscalationStage:
    return EscalationStage(
        after_seconds=after,
        kind=kind,
        rule=StaffRule(kind=StaffRuleKind.ROLE, ref=role),
    )


# --- validator: T3a-T3c ----------------------------------------------------


def test_t3_wellformed_policy_is_accepted() -> None:
    schema = _staffed_timed_schema()
    schema = set_escalation_policy(
        schema,
        _nid(schema, "Pruefen"),
        EscalationPolicy(
            stages=[
                _stage(0, EscalationKind.FUNCTIONAL, "r-senior"),
                _stage(3600, EscalationKind.HIERARCHICAL, "r-leitung"),
            ]
        ),
    )
    assert validate(schema) == []
    # Clearing works and leaves no trace.
    cleared = set_escalation_policy(schema, _nid(schema, "Pruefen"), None)
    assert cleared.escalation_policies == {}


def test_t3a_requires_a_resolvable_target_time() -> None:
    schema = _staffed_timed_schema()
    node = _nid(schema, "Pruefen")
    schema = set_time_constraint(schema, node, None)  # drop the target time

    with pytest.raises(CorrectnessError) as err:
        set_escalation_policy(
            schema,
            node,
            EscalationPolicy(stages=[_stage(0, EscalationKind.FUNCTIONAL, "r-senior")]),
        )
    assert any(f.rule == "T3" and "target time" in f.message for f in err.value.findings)


def test_t3b_stages_must_ascend_strictly() -> None:
    schema = _staffed_timed_schema()
    node = _nid(schema, "Pruefen")

    with pytest.raises(CorrectnessError) as err:
        set_escalation_policy(
            schema,
            node,
            EscalationPolicy(
                stages=[
                    _stage(600, EscalationKind.FUNCTIONAL, "r-senior"),
                    _stage(600, EscalationKind.HIERARCHICAL, "r-leitung"),
                ]
            ),
        )
    assert any("ascending" in f.message for f in err.value.findings)

    with pytest.raises(CorrectnessError):
        set_escalation_policy(schema, node, EscalationPolicy(stages=[]))


def test_t3c_targets_must_resolve_and_not_reference_nodes() -> None:
    schema = _staffed_timed_schema()
    node = _nid(schema, "Pruefen")

    with pytest.raises(CorrectnessError) as err:  # empty possible set
        set_escalation_policy(
            schema,
            node,
            EscalationPolicy(stages=[_stage(0, EscalationKind.FUNCTIONAL, "r-gibtsnicht")]),
        )
    assert any(f.rule == "T3" or f.rule == "Z1" for f in err.value.findings)

    with pytest.raises(CorrectnessError) as err:  # node-referencing kind
        set_escalation_policy(
            schema,
            node,
            EscalationPolicy(
                stages=[
                    EscalationStage(
                        after_seconds=0,
                        kind=EscalationKind.HIERARCHICAL,
                        rule=StaffRule(
                            kind=StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR,
                            ref=node,
                        ),
                    )
                ]
            ),
        )
    assert any("node-referencing" in f.message for f in err.value.findings)


def test_delete_node_cleans_the_policy() -> None:
    schema = _staffed_timed_schema()
    node = _nid(schema, "Pruefen")
    schema = set_escalation_policy(
        schema,
        node,
        EscalationPolicy(stages=[_stage(0, EscalationKind.FUNCTIONAL, "r-senior")]),
    )
    gone = delete_node(schema, node)
    assert gone.escalation_policies == {}
    assert validate(gone) == []


def test_n3_couples_functional_targets_into_mail_addressability() -> None:
    """A mail-notified step must be addressable for escalation performers too."""

    schema = _staffed_timed_schema()
    node = _nid(schema, "Pruefen")
    schema = update_agent(schema, "a-anna", email="anna@example.org")
    schema = set_escalation_policy(
        schema,
        node,
        EscalationPolicy(stages=[_stage(0, EscalationKind.FUNCTIONAL, "r-senior")]),
    )
    # Bert (the escalation performer) has no address -> N3 rejects the binding.
    with pytest.raises(CorrectnessError) as err:
        set_mail_binding(schema, node, MailBinding(subject="Hi", body="T"))
    assert any(f.rule == "N3" for f in err.value.findings)

    schema = update_agent(schema, "a-bert", email="bert@example.org")
    schema = set_mail_binding(schema, node, MailBinding(subject="Hi", body="T"))
    assert validate(schema) == []


# --- runtime: loop reset ---------------------------------------------------


def test_loop_iteration_reset_clears_fired_stages() -> None:
    schema = create_empty_schema("Schleife")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "na", DataType.BOOLEAN, element_id="na")
    schema = insert_loop(schema, _nid(schema, "Erfassen"), "Nacharbeit", discriminator="na")
    schema = add_role(schema, "SB", role_id="r-sb")
    schema = add_agent(schema, "Anna", role_ids=["r-sb"], agent_id="a-anna")
    for label in ("Erfassen", "Nacharbeit"):
        schema = assign_staff_rule(
            schema, _nid(schema, label), StaffRule(kind=StaffRuleKind.ROLE, ref="r-sb")
        )
    schema = release(schema)
    body = _nid(schema, "Nacharbeit")

    inst = instantiate(schema)
    inst = complete_activity(inst, schema, _nid(schema, "Erfassen"))
    inst.escalated_stages[body] = 1  # as if a stage had fired this round
    inst = complete_activity(inst, schema, body, {"na": True})  # repeat
    assert inst.node_states[body] is NodeState.ACTIVATED
    assert body not in inst.escalated_stages  # fresh round starts unescalated


# --- boundary: sweep, broadening, mail, audit, idempotency -----------------


def _api_world() -> tuple[str, str, str]:
    """Released schema + instance over HTTP; returns (sid, node_id, iid)."""

    sid = client.post("/schemas", json={"name": "Eskalation-API"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Pruefen", "after_node_id": "start"},
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node = next(nid for nid, n in nodes.items() if n.get("label") == "Pruefen")
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "r-sb"})
    client.post(f"/schemas/{sid}/roles", json={"name": "Senior", "role_id": "r-senior"})
    client.post(f"/schemas/{sid}/roles", json={"name": "Leitung", "role_id": "r-lead"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Anna", "role_ids": ["r-sb"], "agent_id": "a-anna"},
    )
    client.post(
        f"/schemas/{sid}/agents",
        json={
            "name": "Bert",
            "role_ids": ["r-senior"],
            "agent_id": "a-bert",
            "email": "bert@example.org",
        },
    )
    client.post(
        f"/schemas/{sid}/agents",
        json={
            "name": "Lena",
            "role_ids": ["r-lead"],
            "agent_id": "a-lena",
            "email": "lena@example.org",
        },
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": node, "rule": {"kind": "ROLE", "ref": "r-sb"}},
    )
    client.post(
        f"/schemas/{sid}/time-constraint",
        json={"node_id": node, "constraint": {"target_lead_seconds": 60}},
    )
    resp = client.post(
        f"/schemas/{sid}/escalation-policy",
        json={
            "node_id": node,
            "policy": {
                "stages": [
                    {
                        "after_seconds": 0,
                        "kind": "FUNCTIONAL",
                        "rule": {"kind": "ROLE", "ref": "r-senior"},
                    },
                    {
                        "after_seconds": 3600,
                        "kind": "HIERARCHICAL",
                        "rule": {"kind": "ROLE", "ref": "r-lead"},
                    },
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    client.post(f"/schemas/{sid}/release")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    return sid, node, iid


def test_sweep_fires_stages_broadens_notifies_and_stays_idempotent() -> None:
    _sid, node, iid = _api_world()
    inst = client.get(f"/instances/{iid}").json()
    activated = datetime.fromisoformat(inst["node_activated_at"][node])

    # Before the due instant nothing fires; Bert (senior) sees nothing.
    assert api_module._escalation_sweep(now=activated + timedelta(seconds=30)) == 0
    assert not any(
        t["node_id"] == node for t in client.get("/agents/a-bert/tasks").json()
    )

    # Past the due instant stage 1 (FUNCTIONAL) fires exactly once.
    over = activated + timedelta(seconds=61)
    assert api_module._escalation_sweep(now=over) == 1
    assert api_module._escalation_sweep(now=over) == 0  # idempotent
    mine = [t for t in client.get("/agents/a-bert/tasks").json() if t["node_id"] == node]
    assert mine and mine[0]["escalated_stage"] == 1  # offer broadened to Bert
    # Bert may now even claim the escalated task (full eligibility).
    resp = client.post(
        f"/instances/{iid}/claim", json={"node_id": node, "agent_id": "a-bert"}
    )
    assert resp.status_code == 200
    client.post(f"/instances/{iid}/return", json={"node_id": node, "agent_id": "a-bert"})

    # The added performer was notified durably (dedup per stage+activation).
    dedup1 = f"esc|{iid}|{node}|0|{activated.isoformat()}"
    entry = api_module._mail_outbox_store.find_by_dedup_key(dedup1)
    assert entry is not None and entry.recipients == ["bert@example.org"]

    # Stage 2 (HIERARCHICAL) fires later: Lena is informed but never eligible.
    assert api_module._escalation_sweep(now=over + timedelta(seconds=3600)) == 1
    assert not any(
        t["node_id"] == node for t in client.get("/agents/a-lena/tasks").json()
    )
    dedup2 = f"esc|{iid}|{node}|1|{activated.isoformat()}"
    entry2 = api_module._mail_outbox_store.find_by_dedup_key(dedup2)
    assert entry2 is not None and entry2.recipients == ["lena@example.org"]

    # Both stages are on the audit trail with their kind.
    events = client.get(f"/instances/{iid}/audit").json()
    kinds = [
        e["detail"]["kind"] for e in events if e["event_type"] == "TASK_ESCALATED"
    ]
    assert kinds == ["FUNCTIONAL", "HIERARCHICAL"]


def test_admin_sweep_endpoint_reports_fired_stages() -> None:
    _sid, node, iid = _api_world()
    resp = client.post("/admin/escalations/sweep")
    assert resp.status_code == 200
    assert "fired" in resp.json()
