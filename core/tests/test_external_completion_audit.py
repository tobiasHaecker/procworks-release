# SPDX-License-Identifier: BUSL-1.1
"""An automatic step completed from outside is recorded like a human completion.

A worker reporting an external task (pull) and a tool calling back after a
push both end in the same engine completion as a person finishing a step. The
record of it must be the same, too: ``ACTIVITY_COMPLETED`` for the step (with
the sending identity and the worker as sender), ``INSTANCE_COMPLETED`` plus the
``instance.completed`` webhook when the case ends. Without it the KPIs, the
process map and the history did not know the step had run, and subscribers
never heard that the case was done. A refused report records nothing, and a
throw-away test instance stays out of the audit as on every other path.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks.api import app, get_principal
from procworks.audit import AuditEvent, EventType
from procworks.auth import INTEGRATION, SCOPE_TASKS_COMPLETE, SCOPE_TASKS_FETCH, Principal

client = TestClient(app)

_names = (f"ext-audit-{n}" for n in itertools.count(1))


def _automatic_schema(automation: dict[str, str], *, release: bool = True) -> tuple[str, str]:
    """One automatic step that writes ``approved``; returns (schema id, node id)."""

    sid = client.post("/schemas", json={"name": next(_names)}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Automatik", "after_node_id": "start"}
    ).json()
    node_id = next(n["id"] for n in schema["nodes"].values() if n["type"] == "ACTIVITY")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Freigabe", "data_type": "BOOLEAN", "element_id": "approved"},
    )
    client.post(f"/schemas/{sid}/service", json={"node_id": node_id, "name": "Automatik",
                                                  "automatic": True})
    assert client.post(
        f"/schemas/{sid}/automation", json={"node_id": node_id, **automation}
    ).status_code == 200
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": node_id, "element_id": "approved", "mode": "WRITE",
              "mandatory": False},
    )
    if release:
        assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, node_id


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    """Capture every emitted domain event (no outbox, no delivery)."""

    seen: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        api_module, "_emit_event", lambda kind, payload: seen.append((kind, dict(payload)))
    )
    return seen


@pytest.fixture
def as_worker() -> Iterator[Callable[[], None]]:
    """Switch the caller to the machine identity ``erp-worker``."""

    def _set() -> None:
        app.dependency_overrides[get_principal] = lambda: Principal(
            subject="erp-worker",
            roles=frozenset({INTEGRATION}),
            scopes=frozenset({SCOPE_TASKS_FETCH, SCOPE_TASKS_COMPLETE}),
        )

    yield _set
    app.dependency_overrides.pop(get_principal, None)


def _audit(instance_id: str, kind: EventType) -> list[AuditEvent]:
    return [e for e in api_module._audit.for_instance(instance_id) if e.event_type is kind]


def _fetch(topic: str, worker: str = "w1") -> dict[str, object]:
    tasks = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": worker, "topics": [topic], "lock_ms": 300_000},
    ).json()
    assert len(tasks) == 1, tasks
    task: dict[str, object] = tasks[0]
    return task


def _completed_instances(events: list[tuple[str, dict[str, object]]]) -> list[object]:
    return [p["instance_id"] for kind, p in events if kind == "instance.completed"]


def test_a_pulled_task_completion_is_audited_and_announced(
    events: list[tuple[str, dict[str, object]]], as_worker: Callable[[], None]
) -> None:
    topic = next(_names)
    sid, node_id = _automatic_schema({"automation": "EXTERNAL_TASK", "topic": topic})
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    as_worker()
    task = _fetch(topic)

    done = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {"approved": True}},
    )

    assert done.status_code == 200, done.text
    [step] = _audit(iid, EventType.ACTIVITY_COMPLETED)
    assert step.node_id == node_id
    assert step.label == "Automatik"
    assert step.agent_id is None
    assert step.detail is not None
    assert step.detail["actor"] == "erp-worker"
    assert step.detail["worker_id"] == "w1"
    assert step.detail["external_task"] == task["id"]
    assert len(_audit(iid, EventType.INSTANCE_COMPLETED)) == 1
    assert _completed_instances(events) == [iid]
    app.dependency_overrides.pop(get_principal, None)  # KPIs are not a worker's read
    kpis = client.get("/monitoring/kpis", params={"schema_id": sid}).json()
    assert (kpis["total_instances"], kpis["completed"]) == (1, 1)
    assert [a["node_id"] for a in kpis["activity_stats"]] == [node_id]


def test_a_push_callback_completion_is_audited_and_announced(
    events: list[tuple[str, dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pushed: list[dict[str, object]] = []
    monkeypatch.setattr(
        api_module, "_push_external", lambda binding, payload: pushed.append(payload)
    )
    ref = next(_names)
    api_module._push_endpoints.register(ref, "https://tool.example.com/push", "")
    sid, node_id = _automatic_schema({"automation": "HTTP_PUSH", "endpoint_ref": ref})
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    [payload] = [p for p in pushed if p["instance_id"] == iid]

    done = client.post(
        f"/v1/external-tasks/{payload['task_id']}/complete",
        json={"worker_id": payload["callback_token"], "variables": {"approved": True}},
    )

    assert done.status_code == 200, done.text
    [step] = _audit(iid, EventType.ACTIVITY_COMPLETED)
    assert step.node_id == node_id
    assert step.detail is not None
    assert step.detail["external_task"] == payload["task_id"]
    # The callback token is a credential for this one task: never in the audit.
    assert payload["callback_token"] not in step.detail.values()
    assert step.detail["channel"] == "push"
    assert len(_audit(iid, EventType.INSTANCE_COMPLETED)) == 1
    assert _completed_instances(events) == [iid]


def test_a_refused_report_records_nothing(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    topic = next(_names)
    sid, _node = _automatic_schema({"automation": "EXTERNAL_TASK", "topic": topic})
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    task = _fetch(topic)

    foreign = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w2", "variables": {"approved": True}},
    )
    wrong_type = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {"approved": "vielleicht"}},
    )

    assert foreign.status_code == 409
    assert "locked by another worker" in foreign.json()["detail"]["message"]
    assert wrong_type.status_code == 422
    assert "is not a BOOLEAN" in wrong_type.json()["detail"]["message"]
    assert _audit(iid, EventType.ACTIVITY_COMPLETED) == []
    assert _completed_instances(events) == []

    first = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {"approved": True}},
    )
    second = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {"approved": True}},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert "is not locked" in second.json()["detail"]["message"]
    assert len(_audit(iid, EventType.ACTIVITY_COMPLETED)) == 1
    assert len(_audit(iid, EventType.INSTANCE_COMPLETED)) == 1
    assert _completed_instances(events) == [iid]


def test_a_test_instance_completed_by_hand_stays_out_of_the_audit(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    """A test instance's automatic step is never handed to a worker; it is
    completed by hand (simulated) -- and that, too, records nothing."""

    topic = next(_names)
    sid, node = _automatic_schema(
        {"automation": "EXTERNAL_TASK", "topic": topic}, release=False
    )
    started = client.post(f"/schemas/{sid}/instances").json()
    assert started["is_test"] is True
    assert client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    ).json() == []

    done = client.post(
        f"/instances/{started['id']}/complete",
        json={"node_id": node, "data": {"approved": True}},
    )

    assert done.status_code == 200, done.text
    assert client.get(f"/instances/{started['id']}").json()["state"] == "COMPLETED"
    assert api_module._audit.for_instance(started["id"]) == []
    assert _completed_instances(events) == []
