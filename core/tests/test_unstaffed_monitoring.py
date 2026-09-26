# SPDX-License-Identifier: BUSL-1.1
"""Stalled instances show up in the monitoring (Validierung 2026-09-25, VAL-09).

Instances whose open step nobody may work -- an empty rule, an ad-hoc step
without rule, a four-eyes step after a supervision act -- appeared with
"overdue 0, escalated 0"; only the detail view said "niemand zuständig".
``assignment.unstaffed_steps`` and ``GET /monitoring/unstaffed`` are the last
line of defence, independent of the modelling-time guards (Z2, ad-hoc B2).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from staffing import staff_via_api, staffed

from procworks import (
    ExecutionContext,
    complete_activity,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
)
from procworks.api import app
from procworks.assignment import unstaffed_steps
from procworks.model import NodeState
from procworks.store import InMemoryInstanceStore

client = TestClient(app)


def _ids(schema, *labels):  # type: ignore[no-untyped-def]
    return [next(n.id for n in schema.nodes.values() if n.label == lab) for lab in labels]


def test_step_without_rule_is_reported_as_no_rule() -> None:
    schema = serial_insert(create_empty_schema("U", schema_id="unst1"), "A", after_node_id="start")
    schema = release(staffed(schema))
    [act] = _ids(schema, "A")
    context = ExecutionContext(lambda *_: None, InMemoryInstanceStore())
    instance = instantiate(schema, context=context)
    assert instance.node_states[act] is NodeState.ACTIVATED
    legacy = schema.model_copy(deep=True)
    del legacy.staff_rules[act]  # e.g. an ad-hoc step from before the B2 gate

    [step] = unstaffed_steps(legacy, instance)

    assert (step.node_id, step.reason) == (act, "no_rule")
    assert unstaffed_steps(schema, instance) == []  # counter-example: staffed


def _four_eyes_after_supervision() -> tuple[str, str]:
    """API instance: "Erfassen" done without a performer, "Freigeben" = its performer."""

    sid = client.post("/schemas", json={"name": "Niemand zuständig"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    erfassen = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Freigeben", "after_node_id": erfassen}
    ).json()
    freigeben = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Freigeben")
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(
        f"/schemas/{sid}/agents", json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"}
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": erfassen, "rule": {"kind": "ROLE", "ref": "sb"}},
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": freigeben, "rule": {"kind": "NODE_PERFORMING_AGENT", "ref": erfassen}},
    )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    # Open mode: completed without a person -> no performer is recorded.
    done = client.post(f"/instances/{iid}/complete", json={"node_id": erfassen})
    assert done.status_code == 200, done.text
    return iid, freigeben


def test_monitoring_lists_a_step_whose_rule_finds_nobody() -> None:
    iid, freigeben = _four_eyes_after_supervision()

    rows = [r for r in client.get("/monitoring/unstaffed").json() if r["instance_id"] == iid]

    assert [(r["node_id"], r["label"], r["reason"]) for r in rows] == [
        (freigeben, "Freigeben", "nobody")
    ]


def test_monitoring_leaves_staffed_steps_and_test_instances_out() -> None:
    sid = client.post("/schemas", json={"name": "Besetzt"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    staff_via_api(client, sid)
    test_iid = client.post(f"/schemas/{sid}/instances").json()["id"]  # draft -> test instance
    client.post(f"/schemas/{sid}/release")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    reported = {r["instance_id"] for r in client.get("/monitoring/unstaffed").json()}

    assert iid not in reported and test_iid not in reported


def test_completing_the_step_away_clears_the_report() -> None:
    schema = serial_insert(create_empty_schema("U2", schema_id="unst2"), "A", after_node_id="start")
    schema = release(staffed(schema))
    [act] = _ids(schema, "A")
    context = ExecutionContext(lambda *_: None, InMemoryInstanceStore())
    instance = instantiate(schema, context=context)
    legacy = schema.model_copy(deep=True)
    del legacy.staff_rules[act]
    assert unstaffed_steps(legacy, instance)

    instance = complete_activity(instance, legacy, act, context=context)

    assert unstaffed_steps(legacy, instance) == []
