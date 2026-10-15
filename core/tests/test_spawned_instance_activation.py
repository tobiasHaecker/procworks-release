# SPDX-License-Identifier: BUSL-1.1
"""A sub-process child or follow-up starts like any other case.

Instances the engine starts on its own never pass through a start request, so
the boundary side effects of a start never ran for them: no activation time
on their steps (no due date, no escalation, no waiting-time figure), no
modelled mail, and no ``instance.started`` for subscribers -- although they
did send ``instance.completed`` at the end. Now each spawned instance gets
the same start as a directly started one: start and activation times, the
mail of every step that is ready at once, and ``instance.started`` (plus
``instance.completed`` when it already finished while it was spawned).
Throw-away test instances and what they spawn still do none of this.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app

client = TestClient(app)
_names = (f"Aktivierung {n}" for n in itertools.count(1))


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, object]]]:
    """Record every emitted domain event as ``(type, instance_id)``."""

    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(
        api_module, "_emit_event", lambda kind, data: seen.append((kind, data.get("instance_id")))
    )
    yield seen


def _node_id(schema_id: str, label: str) -> str:
    nodes = client.get(f"/schemas/{schema_id}").json()["nodes"]
    return next(n["id"] for n in nodes.values() if n["label"] == label)


def _target(label: str, *, mail: bool = False) -> str:
    """Released one-step schema; with ``mail`` its step sends a notification."""

    sid = client.post("/schemas", json={"name": next(_names)}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": label, "after_node_id": "start"})
    node = _node_id(sid, label)
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(f"/schemas/{sid}/agents", json={
        "name": "Erika", "role_ids": ["sb"], "agent_id": "a-erika", "email": "erika@firma.de"})
    client.post(f"/schemas/{sid}/staff-rule",
                json={"node_id": node, "rule": {"kind": "ROLE", "ref": "sb"}})
    if mail:
        bound = client.post(f"/schemas/{sid}/mail-binding",
                            json={"node_id": node, "binding": {"subject": "Neue Aufgabe"}})
        assert bound.status_code == 200, bound.text
    assert client.post(f"/schemas/{sid}/release").status_code == 200, sid
    return sid


def _parent(child: str, follow: str | None, *, release: bool = True) -> tuple[str, str]:
    """Parent "Vorher" -> SUBPROCESS(child), optionally an ASYNC follow-up."""

    pid = client.post("/schemas", json={"name": next(_names)}).json()["id"]
    client.post(f"/schemas/{pid}/serial-insert", json={"label": "Vorher", "after_node_id": "start"})
    vorher = _node_id(pid, "Vorher")
    assert client.post(f"/schemas/{pid}/subprocess", json={
        "label": "Teilprozess", "after_node_id": vorher,
        "target_schema_id": child, "target_version": 1}).status_code == 200
    if follow is not None:
        linked = client.post(f"/schemas/{pid}/follow-up",
                             json={"target_schema_id": follow, "target_version": 1})
        assert linked.status_code == 200, linked.text
    staff_via_api(client, pid)
    if release:
        assert client.post(f"/schemas/{pid}/release").status_code == 200
    return pid, vorher


def _spawn_child(pid: str, vorher: str) -> tuple[str, str]:
    """Start the parent and finish "Vorher"; return (parent id, child id)."""

    iid = client.post(f"/schemas/{pid}/instances").json()["id"]
    assert client.post(f"/instances/{iid}/complete", json={"node_id": vorher}).status_code == 200
    child = next(iter(client.get(f"/instances/{iid}").json()["child_instances"].values()))
    return iid, child


def _mails_of(instance_id: str) -> list[object]:
    return [e for e in api_module._mail_outbox_store.list_entries()
            if e.instance_id == instance_id]


def test_a_child_starts_its_clock_like_a_direct_start() -> None:
    child_schema = _target("Kindschritt")
    pid, vorher = _parent(child_schema, None)

    _iid, child = _spawn_child(pid, vorher)

    stored = client.get(f"/instances/{child}").json()
    node = _node_id(child_schema, "Kindschritt")
    assert stored["started_at"] is not None
    assert node in stored["node_activated_at"]
    [task] = client.get(f"/instances/{child}/tasks").json()
    assert task["node_id"] == node


def test_a_follow_up_starts_its_clock_too() -> None:
    child_schema = _target("Kindschritt")
    follow_schema = _target("Folgeschritt")
    pid, vorher = _parent(child_schema, follow_schema)
    iid, child = _spawn_child(pid, vorher)

    client.post(f"/instances/{child}/complete",
                json={"node_id": _node_id(child_schema, "Kindschritt")})

    [follow_up] = client.get(f"/instances/{iid}").json()["follow_up_instances"]
    stored = client.get(f"/instances/{follow_up}").json()
    assert stored["started_at"] is not None
    assert _node_id(follow_schema, "Folgeschritt") in stored["node_activated_at"]


def test_a_child_queues_the_mail_of_its_first_step() -> None:
    child_schema = _target("Kindschritt", mail=True)
    pid, vorher = _parent(child_schema, None)

    _iid, child = _spawn_child(pid, vorher)

    assert len(_mails_of(child)) == 1


def test_spawned_instances_announce_their_start(events: list[tuple[str, object]]) -> None:
    child_schema = _target("Kindschritt")
    follow_schema = _target("Folgeschritt")
    pid, vorher = _parent(child_schema, follow_schema)
    iid, child = _spawn_child(pid, vorher)
    client.post(f"/instances/{child}/complete",
                json={"node_id": _node_id(child_schema, "Kindschritt")})
    [follow_up] = client.get(f"/instances/{iid}").json()["follow_up_instances"]

    started = [i for kind, i in events if kind == "instance.started"]
    assert started == [iid, child, follow_up]


def test_a_child_that_finishes_at_once_announces_start_and_end(
    events: list[tuple[str, object]],
) -> None:
    empty = client.post("/schemas", json={"name": next(_names)}).json()["id"]
    assert client.post(f"/schemas/{empty}/release").status_code == 200
    pid, vorher = _parent(empty, None)

    _iid, child = _spawn_child(pid, vorher)

    assert [kind for kind, i in events if i == child] == [
        "instance.started", "instance.completed"
    ]


def test_what_a_test_instance_spawns_stays_silent(events: list[tuple[str, object]]) -> None:
    child_schema = _target("Kindschritt", mail=True)
    pid, vorher = _parent(child_schema, None, release=False)

    iid, child = _spawn_child(pid, vorher)

    assert client.get(f"/instances/{child}").json()["is_test"] is True
    assert [kind for kind, i in events if i in (iid, child)] == []
    assert _mails_of(child) == []


def test_a_child_step_escalates_once_it_is_overdue() -> None:
    # Target with a lead time of 60 s and one escalation stage.
    sid = client.post("/schemas", json={"name": next(_names)}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Kindschritt",
                                                       "after_node_id": "start"})
    node = _node_id(sid, "Kindschritt")
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(f"/schemas/{sid}/roles", json={"name": "Senior", "role_id": "senior"})
    client.post(f"/schemas/{sid}/agents",
                json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a-erika"})
    client.post(f"/schemas/{sid}/agents",
                json={"name": "Bert", "role_ids": ["senior"], "agent_id": "a-bert"})
    client.post(f"/schemas/{sid}/staff-rule",
                json={"node_id": node, "rule": {"kind": "ROLE", "ref": "sb"}})
    client.post(f"/schemas/{sid}/time-constraint",
                json={"node_id": node, "constraint": {"target_lead_seconds": 60}})
    policy = client.post(f"/schemas/{sid}/escalation-policy", json={
        "node_id": node, "policy": {"stages": [
            {"after_seconds": 0, "kind": "FUNCTIONAL", "rule": {"kind": "ROLE", "ref": "senior"}}
        ]}})
    assert policy.status_code == 200, policy.text
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    pid, vorher = _parent(sid, None)
    _iid, child = _spawn_child(pid, vorher)
    activated = datetime.fromisoformat(
        client.get(f"/instances/{child}").json()["node_activated_at"][node]
    )

    api_module._escalation_sweep(now=activated + timedelta(seconds=61))

    assert client.get(f"/instances/{child}").json()["escalated_stages"] == {node: 1}
