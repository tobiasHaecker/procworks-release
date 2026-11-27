# SPDX-License-Identifier: BUSL-1.1
"""A failed push leaves the task open, retries it, and ends in an incident.

The push side of an automatic step delivers the input package to a tool. When
that delivery fails (the tool answers 500, or cannot be reached), the task must
not be locked to a callback that will never come: it stays ``CREATED``, is
pushed again on a later drive once its back-off is over, and after its retry
budget is used up it becomes a visible incident -- the same rules as a worker's
reported failure. Resolving the incident gives it a new budget. A successful
push behaves as before, and a push that could not even be handed over (no
endpoint configured) is retried on the next drive without using the budget.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks import (
    add_data_element,
    assign_service,
    connect_data,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
    set_automation,
)
from procworks.api import app
from procworks.execution import ExecutionContext
from procworks.integration_runtime import ExternalTaskRuntime
from procworks.model import (
    AccessMode,
    AutomationKind,
    DataType,
    ExternalTaskState,
    NodeType,
    OutboxState,
    ProcessInstance,
    ProcessSchema,
    ServiceBinding,
)
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)

_URL = "https://tool.example.com/push"
_refs = (f"fail-ep-{n}" for n in itertools.count(1))


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Sink:
    """Push sink reporting delivery: ``ok`` decides, every call is recorded."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls: list[dict[str, object]] = []

    def __call__(self, binding: ServiceBinding, payload: dict[str, object]) -> bool:
        self.calls.append(payload)
        return self.ok


def _runtime(
    sink: _Sink, *, retry_max: int = 3
) -> tuple[ExternalTaskRuntime, _Clock, list[tuple[str, dict[str, object]]]]:
    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    schema = create_empty_schema("Push", schema_id="pushfail")
    schema = serial_insert(schema, "Melden", after_node_id="start")
    node_id = next(n.id for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    schema = add_data_element(schema, "Freigabe", DataType.BOOLEAN, element_id="approved")
    schema = assign_service(schema, node_id, "Melden", automatic=True)
    schema = set_automation(
        schema, node_id, AutomationKind.HTTP_PUSH, endpoint_ref="ep", retry_max=retry_max
    )
    schema = connect_data(schema, node_id, "approved", AccessMode.WRITE, mandatory=False)
    schema = release(schema)
    schemas.put(schema)
    context = ExecutionContext(make_resolver(schemas), instances)
    instances.put(instantiate(schema, context=context))
    clock = _Clock()
    events: list[tuple[str, dict[str, object]]] = []

    def schema_for(inst: ProcessInstance) -> ProcessSchema:
        resolved = schemas.get(inst.schema_id)
        assert resolved is not None
        return resolved

    runtime = ExternalTaskRuntime(
        InMemoryExternalTaskStore(),
        instances,
        schema_for,
        context,
        now=clock,
        on_push=sink,
        on_event=lambda kind, data: events.append((kind, data)),
    )
    return runtime, clock, events


def _after_backoff(runtime: ExternalTaskRuntime, clock: _Clock) -> None:
    [task] = runtime.list_tasks()
    assert task.available_at is not None
    clock.t = task.available_at + 0.001


def test_a_failed_delivery_leaves_the_task_open_with_a_back_off() -> None:
    sink = _Sink(ok=False)
    runtime, clock, _events = _runtime(sink)

    assert runtime.drive_push() == []

    [task] = runtime.list_tasks()
    assert task.state is ExternalTaskState.CREATED
    assert task.worker_id is None
    assert task.retries_left == 2
    assert task.available_at is not None and task.available_at > clock.t
    assert runtime.list_incidents() == []
    # Still in back-off: no second attempt yet.
    assert runtime.drive_push() == []
    assert len(sink.calls) == 1


def test_the_next_drive_after_the_back_off_pushes_again() -> None:
    sink = _Sink(ok=False)
    runtime, clock, _events = _runtime(sink)
    runtime.drive_push()

    sink.ok = True
    _after_backoff(runtime, clock)
    pushed = runtime.drive_push()

    assert len(pushed) == 1
    assert pushed[0].state is ExternalTaskState.LOCKED
    assert pushed[0].worker_id == sink.calls[-1]["callback_token"]
    assert len(sink.calls) == 2


def test_an_exhausted_budget_becomes_an_incident_and_resolving_resets_it() -> None:
    sink = _Sink(ok=False)
    runtime, clock, events = _runtime(sink, retry_max=2)

    runtime.drive_push()
    _after_backoff(runtime, clock)
    runtime.drive_push()

    [task] = runtime.list_tasks()
    assert task.state is ExternalTaskState.INCIDENT
    [incident] = runtime.list_incidents(unresolved_only=True)
    assert incident.external_task_id == task.id
    assert "push" in incident.message
    assert [k for k, _ in events].count("task.incident") == 1
    assert runtime.drive_push() == []  # an incident is not pushed
    assert len(sink.calls) == 2

    runtime.resolve_incident(incident.id)
    [task] = runtime.list_tasks()
    assert task.state is ExternalTaskState.CREATED
    assert task.retries_left == 2
    sink.ok = True
    assert [t.id for t in runtime.drive_push()] == [task.id]


def test_a_push_that_cannot_be_handed_over_keeps_its_budget() -> None:
    """No endpoint configured (the sink raises): retried next drive, no budget used."""

    class _Raising:
        def __call__(self, binding: ServiceBinding, payload: dict[str, object]) -> bool:
            raise RuntimeError("endpoint not configured")

    runtime, _clock, _events = _runtime(_Sink(ok=True))
    runtime._on_push = _Raising()

    assert runtime.drive_push() == []

    [task] = runtime.list_tasks()
    assert task.state is ExternalTaskState.CREATED
    assert task.retries_left == 3
    assert task.available_at is None


# --- through the API: the outbox does not retry behind the task's back -----

client = TestClient(app)


class _Transport:
    """Fake transport: fixed status, or a connection error (no network)."""

    def __init__(self, status: int | None) -> None:
        self.status = status
        self.calls = 0

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        self.calls += 1
        if self.status is None:
            raise OSError("connection refused")
        return self.status


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Transport]:
    fake = _Transport(500)
    monkeypatch.setattr(api_module._outbox, "_transport", fake)
    api_module._outbox._circuit.clear()
    try:
        yield fake
    finally:
        api_module._outbox._store.clear()
        api_module._outbox._circuit.clear()


def _push_instance(ref: str) -> str:
    api_module._push_endpoints.register(ref, _URL, "")
    sid = client.post("/schemas", json={"name": f"push-{ref}"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Melden", "after_node_id": "start"}
    ).json()
    node_id = next(n["id"] for n in schema["nodes"].values() if n["type"] == "ACTIVITY")
    client.post(f"/schemas/{sid}/service", json={"node_id": node_id, "name": "Melden",
                                                  "automatic": True})
    client.post(
        f"/schemas/{sid}/automation",
        json={"node_id": node_id, "automation": "HTTP_PUSH", "endpoint_ref": ref,
              "retry_max": 2},
    )
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid: str = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    return iid


def _task_of(iid: str) -> dict[str, object]:
    [task] = [t for t in api_module._external_tasks.list_tasks() if t.instance_id == iid]
    return task.model_dump(mode="json")


def _end_back_off(task_id: str) -> None:
    task = api_module._external_tasks.get(task_id)
    assert task is not None
    task.available_at = None
    api_module._external_tasks.put(task)


@pytest.mark.parametrize("status", [500, None], ids=["http-500", "unreachable"])
def test_a_failing_endpoint_ends_in_an_incident_not_in_a_locked_task(
    transport: _Transport, status: int | None
) -> None:
    transport.status = status
    iid = _push_instance(next(_refs))

    task = _task_of(iid)
    assert task["state"] == "CREATED"
    assert client.get(f"/v1/external-tasks/{task['id']}").json()["state"] == "CREATED"
    # One attempt per push; the outbox entry is finished, not retried later.
    entries = [e for e in api_module._outbox._store.list_entries()
               if e.payload.get("task_id") == task["id"]]
    assert [e.state for e in entries] == [OutboxState.DEAD]

    _end_back_off(str(task["id"]))
    assert client.post("/v1/external-tasks/drive-push").json() == []

    assert _task_of(iid)["state"] == "INCIDENT"
    incidents = [i for i in client.get("/v1/incidents").json()
                 if i["external_task_id"] == task["id"]]
    assert len(incidents) == 1 and not incidents[0]["resolved"]
    assert transport.calls == 2


def test_a_healthy_endpoint_still_locks_on_the_callback_token(transport: _Transport) -> None:
    transport.status = 200
    iid = _push_instance(next(_refs))

    task = _task_of(iid)
    assert task["state"] == "LOCKED"
    entries = [e for e in api_module._outbox._store.list_entries()
               if e.payload.get("task_id") == task["id"]]
    assert [e.state for e in entries] == [OutboxState.DELIVERED]
    assert json.loads(json.dumps(entries[0].payload))["callback_token"] == task["worker_id"]
