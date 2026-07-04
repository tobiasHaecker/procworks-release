# SPDX-License-Identifier: BUSL-1.1
"""External-task runtime tests — outbound integration boundary (P2, roadmap E11).

Two layers are covered:

* the versioned ``/v1/external-tasks`` API (fetch-and-lock, complete, failure,
  bpmn-error, extend/unlock, incidents) and its integration-scope gate, driven
  through a FastAPI ``TestClient``; and
* the pure :class:`procworks.integration_runtime.ExternalTaskRuntime` with a
  controllable clock, so lock expiry, back-off, retries and priority ordering
  are tested deterministically without sleeping.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from procworks import (
    add_data_element,
    assign_service,
    connect_data,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
    set_automation,
    set_node_priority,
)
from procworks.api import app, get_principal
from procworks.auth import (
    INTEGRATION,
    SCOPE_TASKS_COMPLETE,
    SCOPE_TASKS_FETCH,
    Principal,
)
from procworks.execution import ExecutionContext
from procworks.integration_runtime import ExternalTaskError, ExternalTaskRuntime
from procworks.model import (
    AccessMode,
    AutomationKind,
    DataType,
    ExternalTaskState,
    ImpactUrgency,
    NodeType,
    ProcessInstance,
    ProcessSchema,
    WorkItemPriority,
)
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)

client = TestClient(app)

_topics = (f"p2-topic-{n}" for n in itertools.count(1))


def _unique_topic() -> str:
    return next(_topics)


# --- API layer ------------------------------------------------------------


def _external_task_schema(topic: str) -> tuple[str, str]:
    """Create + release a one-activity EXTERNAL_TASK schema; return (sid, node)."""

    sid = client.post("/schemas", json={"name": f"ext-{topic}"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Run", "after_node_id": "start"},
    )
    schema = client.get(f"/schemas/{sid}").json()
    node_id = next(
        nid for nid, node in schema["nodes"].items() if node["type"] == "ACTIVITY"
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Betrag", "data_type": "INTEGER", "element_id": "betrag"},
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Freigabe", "data_type": "BOOLEAN", "element_id": "approved"},
    )
    client.post(
        f"/schemas/{sid}/service",
        json={"node_id": node_id, "name": "Run", "automatic": True},
    )
    client.post(
        f"/schemas/{sid}/automation",
        json={"node_id": node_id, "automation": "EXTERNAL_TASK", "topic": topic},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": node_id, "element_id": "betrag", "mode": "READ",
              "mandatory": False},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": node_id, "element_id": "approved", "mode": "WRITE",
              "mandatory": False},
    )
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, node_id


def _start_with_betrag(sid: str, betrag: int) -> str:
    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    client.put(f"/v1/instances/{iid}/data", json={"values": {"betrag": betrag}})
    return iid


def test_fetch_lock_complete_round_trip() -> None:
    topic = _unique_topic()
    sid, _ = _external_task_schema(topic)
    iid = _start_with_betrag(sid, 1200)

    resp = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    )
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["state"] == "LOCKED"
    assert task["topic"] == topic
    assert task["input_variables"] == {"betrag": 1200}

    done = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {"approved": True}},
    )
    assert done.status_code == 200
    assert done.json()["state"] == "COMPLETED"

    instance = client.get(f"/v1/instances/{iid}").json()
    assert instance["state"] == "COMPLETED"
    assert instance["data_values"]["approved"] is True


def test_complete_twice_is_conflict() -> None:
    topic = _unique_topic()
    sid, _ = _external_task_schema(topic)
    _start_with_betrag(sid, 10)
    task = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    ).json()[0]

    first = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {}},
    )
    second = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {}},
    )
    assert first.status_code == 200
    assert second.status_code == 409


def test_complete_rejects_non_writable_output() -> None:
    topic = _unique_topic()
    sid, _ = _external_task_schema(topic)
    _start_with_betrag(sid, 10)
    task = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    ).json()[0]

    # 'betrag' is only a READ access, so writing it back is rejected (422).
    resp = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {"betrag": 5}},
    )
    assert resp.status_code == 422


def test_failure_to_incident_then_resolve() -> None:
    topic = _unique_topic()
    sid, _ = _external_task_schema(topic)
    _start_with_betrag(sid, 10)
    task = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    ).json()[0]

    failed = client.post(
        f"/v1/external-tasks/{task['id']}/failure",
        json={"worker_id": "w1", "error_message": "boom", "retries": 0},
    )
    assert failed.status_code == 200
    assert failed.json()["state"] == "INCIDENT"

    incidents = client.get("/v1/incidents", params={"unresolved_only": True}).json()
    incident = next(i for i in incidents if i["external_task_id"] == task["id"])
    assert incident["message"] == "boom"

    resolved = client.post(f"/v1/incidents/{incident['id']}/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True
    # The task is re-queued, so it can be fetched again.
    again = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w2", "topics": [topic], "lock_ms": 300_000},
    ).json()
    assert any(t["id"] == task["id"] for t in again)


def test_bpmn_error_reports_code() -> None:
    topic = _unique_topic()
    sid, _ = _external_task_schema(topic)
    _start_with_betrag(sid, 10)
    task = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    ).json()[0]

    resp = client.post(
        f"/v1/external-tasks/{task['id']}/bpmn-error",
        json={"worker_id": "w1", "error_code": "INSUFFICIENT_FUNDS"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "BPMN_ERROR"
    assert resp.json()["error_code"] == "INSUFFICIENT_FUNDS"


@pytest.fixture
def as_service() -> Iterator[Callable[[set[str]], None]]:
    def _set(scopes: set[str]) -> None:
        app.dependency_overrides[get_principal] = lambda: Principal(
            subject="svc",
            roles=frozenset({INTEGRATION}),
            scopes=frozenset(scopes),
        )

    yield _set
    app.dependency_overrides.pop(get_principal, None)


def test_service_token_scopes_fetch_and_complete(as_service) -> None:
    topic = _unique_topic()
    sid, _ = _external_task_schema(topic)
    _start_with_betrag(sid, 10)

    # fetch requires tasks:fetch
    as_service(set())
    assert (
        client.post(
            "/v1/external-tasks/fetch-and-lock",
            json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
        ).status_code
        == 403
    )
    as_service({SCOPE_TASKS_FETCH})
    locked = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    )
    assert locked.status_code == 200
    task_id = locked.json()[0]["id"]

    # complete requires tasks:complete (a fetch-only token is rejected)
    as_service({SCOPE_TASKS_FETCH})
    assert (
        client.post(
            f"/v1/external-tasks/{task_id}/complete",
            json={"worker_id": "w1", "variables": {}},
        ).status_code
        == 403
    )
    as_service({SCOPE_TASKS_COMPLETE})
    assert (
        client.post(
            f"/v1/external-tasks/{task_id}/complete",
            json={"worker_id": "w1", "variables": {}},
        ).status_code
        == 200
    )


# --- runtime layer (deterministic clock) ----------------------------------


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def _build_runtime(
    *,
    topic: str = "t",
    betrag: int = 10,
    priority: WorkItemPriority | None = None,
) -> tuple[
    ExternalTaskRuntime, str, _Clock, InMemoryExternalTaskStore, InMemoryInstanceStore
]:
    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    schema = create_empty_schema("U", schema_id="s1")
    schema = serial_insert(schema, "Run", after_node_id="start")
    node_id = next(
        nid for nid, node in schema.nodes.items() if node.type is NodeType.ACTIVITY
    )
    schema = add_data_element(schema, "Betrag", DataType.INTEGER, element_id="betrag")
    schema = add_data_element(
        schema, "Freigabe", DataType.BOOLEAN, element_id="approved"
    )
    schema = assign_service(schema, node_id, "Run", automatic=True)
    schema = set_automation(
        schema, node_id, AutomationKind.EXTERNAL_TASK, topic=topic
    )
    schema = connect_data(
        schema, node_id, "betrag", AccessMode.READ, mandatory=False
    )
    schema = connect_data(
        schema, node_id, "approved", AccessMode.WRITE, mandatory=False
    )
    if priority is not None:
        schema = set_node_priority(schema, node_id, priority)
    schema = release(schema)
    schemas.put(schema)

    context = ExecutionContext(make_resolver(schemas), instances)
    instance = instantiate(schema, context=context)
    instance.data_values["betrag"] = betrag
    instances.put(instance)

    tasks = InMemoryExternalTaskStore()
    clock = _Clock()

    def schema_for(inst: ProcessInstance) -> ProcessSchema:
        resolved = schemas.get(inst.schema_id)
        assert resolved is not None
        return resolved

    runtime = ExternalTaskRuntime(tasks, instances, schema_for, context, now=clock)
    return runtime, instance.id, clock, tasks, instances


def test_runtime_materialises_and_resolves_input() -> None:
    runtime, _iid, _clock, _tasks, _instances = _build_runtime(betrag=42)
    locked = runtime.fetch_and_lock("w1", ["t"], lock_ms=10_000)
    assert len(locked) == 1
    assert locked[0].input_variables == {"betrag": 42}
    assert locked[0].state is ExternalTaskState.LOCKED
    # A second fetch finds nothing new (no duplicate materialisation).
    assert runtime.fetch_and_lock("w1", ["t"], lock_ms=10_000) == []


def test_runtime_complete_writes_output_and_advances() -> None:
    runtime, iid, _clock, _tasks, instances = _build_runtime()
    task = runtime.fetch_and_lock("w1", ["t"], lock_ms=10_000)[0]
    completed = runtime.complete(task.id, "w1", {"approved": True})
    assert completed.state is ExternalTaskState.COMPLETED
    instance = instances.get(iid)
    assert instance is not None
    assert instance.data_values["approved"] is True


def test_runtime_lock_expiry_blocks_completion_and_reclaims() -> None:
    runtime, _iid, clock, _tasks, _instances = _build_runtime()
    task = runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000)[0]
    clock.t = 1006.0  # lock (expires at 1005) is now stale
    with pytest.raises(ExternalTaskError) as err:
        runtime.complete(task.id, "w1", {})
    assert err.value.status == 409
    # The expired lock is reclaimable by another worker.
    reclaimed = runtime.fetch_and_lock("w2", ["t"], lock_ms=5_000)
    assert [t.id for t in reclaimed] == [task.id]
    assert reclaimed[0].worker_id == "w2"


def test_runtime_extend_lock_prevents_expiry() -> None:
    runtime, _iid, clock, _tasks, _instances = _build_runtime()
    task = runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000)[0]
    clock.t = 1004.0
    runtime.extend_lock(task.id, "w1", 5_000)  # now expires at 1009
    clock.t = 1006.0
    assert runtime.complete(task.id, "w1", {}).state is ExternalTaskState.COMPLETED


def test_runtime_unlock_requeues_immediately() -> None:
    runtime, _iid, _clock, _tasks, _instances = _build_runtime()
    task = runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000)[0]
    runtime.unlock(task.id, "w1")
    again = runtime.fetch_and_lock("w2", ["t"], lock_ms=5_000)
    assert [t.id for t in again] == [task.id]


def test_runtime_failure_backoff_then_available() -> None:
    runtime, _iid, clock, _tasks, _instances = _build_runtime()
    task = runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000)[0]
    failed = runtime.failure(task.id, "w1", "transient")
    assert failed.state is ExternalTaskState.CREATED
    assert failed.retries_left == 4
    # Still backing off: not fetchable yet.
    assert runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000) == []
    # After the back-off window it is fetchable again.
    clock.t = (failed.available_at or 0.0) + 1.0
    assert len(runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000)) == 1


def test_runtime_failure_exhausted_creates_incident() -> None:
    runtime, _iid, _clock, _tasks, _instances = _build_runtime()
    task = runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000)[0]
    failed = runtime.failure(task.id, "w1", "fatal", retries=0)
    assert failed.state is ExternalTaskState.INCIDENT
    incidents = runtime.list_incidents(unresolved_only=True)
    assert len(incidents) == 1
    assert incidents[0].external_task_id == task.id
    # Resolving re-queues the task.
    runtime.resolve_incident(incidents[0].id)
    requeued = runtime.get(task.id)
    assert requeued is not None
    assert requeued.state is ExternalTaskState.CREATED


def test_runtime_priority_ordering() -> None:
    runtime, _iid, _clock, tasks, _instances = _build_runtime()
    # Materialise the (MEDIUM) task, then add a CRITICAL sibling on the queue.
    medium = runtime.fetch_and_lock("scout", ["t"], lock_ms=1)[0]
    runtime.unlock(medium.id, "scout")
    critical = medium.model_copy(
        update={"id": "et_high", "priority": WorkItemPriority(
            impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH
        ).level}
    )
    tasks.put(critical)

    picked = runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000, max_tasks=1)
    assert [t.id for t in picked] == ["et_high"]


# --- durable store round-trip (SQLite) ------------------------------------


def test_sqlalchemy_external_task_store_round_trip(tmp_path: Path) -> None:
    from procworks.db import SqlAlchemyExternalTaskStore
    from procworks.model import ExternalTask, Incident

    url = f"sqlite:///{tmp_path / 'tasks.db'}"
    store = SqlAlchemyExternalTaskStore(url, create_tables=True)

    task = ExternalTask(
        id="et_1",
        instance_id="i1",
        node_id="act_1",
        topic="invoice-check",
        input_variables={"betrag": 1200},
    )
    store.put(task)
    loaded = store.get("et_1")
    assert loaded is not None
    assert loaded.topic == "invoice-check"
    assert loaded.input_variables == {"betrag": 1200}
    assert [t.id for t in store.list_tasks()] == ["et_1"]

    incident = Incident(
        id="inc_1",
        external_task_id="et_1",
        instance_id="i1",
        node_id="act_1",
        message="boom",
        created_at=1000.0,
    )
    store.put_incident(incident)
    assert store.get_incident("inc_1") is not None
    assert [i.id for i in store.list_incidents()] == ["inc_1"]

