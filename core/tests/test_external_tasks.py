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


def _bpmn_error(topic: str, code: str = "BONITAET_ABGELEHNT") -> tuple[str, dict[str, object]]:
    """Start a case, fetch its task as ``w1`` and report a BPMN error.

    :returns: ``(instance_id, task)`` with the task as the report answered.
    """

    sid, _ = _external_task_schema(topic)
    iid = _start_with_betrag(sid, 10)
    task = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    ).json()[0]
    resp = client.post(
        f"/v1/external-tasks/{task['id']}/bpmn-error",
        json={"worker_id": "w1", "error_code": code},
    )
    assert resp.status_code == 200, resp.text
    return iid, resp.json()


def _incidents_of(task_id: object) -> list[dict[str, object]]:
    return [
        i for i in client.get("/v1/incidents?unresolved_only=true").json()
        if i["external_task_id"] == task_id
    ]


def test_a_bpmn_error_becomes_a_visible_incident() -> None:
    """A reported business error used to leave the case running with an
    active step and no signal at all. It is now an incident naming the
    reported code, shown wherever incidents are shown."""

    iid, task = _bpmn_error(_unique_topic())

    [incident] = _incidents_of(task["id"])
    assert incident["instance_id"] == iid
    assert "BPMN error 'BONITAET_ABGELEHNT'" in str(incident["message"])
    assert task["state"] == "BPMN_ERROR"


def test_resolving_the_bpmn_incident_re_queues_the_task() -> None:
    topic = _unique_topic()
    _iid, task = _bpmn_error(topic)
    [incident] = _incidents_of(task["id"])

    resolved = client.post(f"/v1/incidents/{incident['id']}/resolve")

    assert resolved.status_code == 200
    again = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w2", "topics": [topic], "lock_ms": 300_000},
    ).json()
    [refetched] = [t for t in again if t["id"] == task["id"]]
    assert refetched["error_code"] is None
    done = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w2", "variables": {"approved": True}},
    )
    assert done.status_code == 200, done.text
    assert _incidents_of(task["id"]) == []


def test_a_bpmn_error_task_is_not_handed_out_until_resolved() -> None:
    topic = _unique_topic()
    _iid, task = _bpmn_error(topic)

    again = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w2", "topics": [topic], "lock_ms": 300_000},
    ).json()
    assert all(t["id"] != task["id"] for t in again)
    refused = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {}},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "ET.not-locked"


def test_a_bpmn_error_announces_the_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import procworks.api as api_module

    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(api_module, "_emit_event", lambda kind, data: events.append((kind, data)))

    _iid, task = _bpmn_error(_unique_topic(), "ABGELEHNT")

    [incident_event] = [data for kind, data in events if kind == "task.incident"]
    assert incident_event["task_id"] == task["id"]
    assert incident_event["error_code"] == "ABGELEHNT"


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


def _correct_betrag(instances: InMemoryInstanceStore, iid: str, betrag: int) -> None:
    """A data correction on the case (as ``PUT …/data`` stores it)."""

    instance = instances.get(iid)
    assert instance is not None
    instance.data_values["betrag"] = betrag
    instances.put(instance)


def test_fetch_hands_out_the_inputs_as_the_case_holds_them_now() -> None:
    """The package is resolved when the task is handed out, like a push: a
    value corrected after the task was created reaches the worker. Before,
    the task kept the value of its creation -- next to external data that
    the pre-fetch read for the *corrected* key."""

    runtime, iid, _clock, tasks, instances = _build_runtime(betrag=3)
    [created] = runtime.sync()
    assert created.input_variables == {"betrag": 3}
    _correct_betrag(instances, iid, 1)

    [locked] = runtime.fetch_and_lock("w1", ["t"], lock_ms=10_000)

    assert locked.input_variables == {"betrag": 1}
    stored = tasks.get(locked.id)
    assert stored is not None and stored.input_variables == {"betrag": 1}


def test_a_retry_after_a_failure_carries_the_corrected_value() -> None:
    """The usual path: an attempt fails, the case data is corrected, the
    next fetch delivers the corrected value."""

    runtime, iid, clock, _tasks, instances = _build_runtime(betrag=3)
    [task] = runtime.fetch_and_lock("w1", ["t"], lock_ms=10_000)
    failed = runtime.failure(task.id, "w1", "Kunde nicht eindeutig", retry_timeout_ms=0)
    assert failed.state is ExternalTaskState.CREATED
    _correct_betrag(instances, iid, 1)

    [again] = runtime.fetch_and_lock("w2", ["t"], lock_ms=10_000)

    assert again.id == task.id
    assert again.input_variables == {"betrag": 1}


def test_a_locked_task_keeps_the_package_it_was_handed_out_with() -> None:
    """Only handing out resolves: what a worker holds does not change under it."""

    runtime, iid, _clock, tasks, instances = _build_runtime(betrag=3)
    [locked] = runtime.fetch_and_lock("w1", ["t"], lock_ms=10_000)
    _correct_betrag(instances, iid, 7)

    assert runtime.fetch_and_lock("w2", ["t"], lock_ms=10_000) == []
    stored = tasks.get(locked.id)
    assert stored is not None and stored.input_variables == {"betrag": 3}


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
    runtime, iid, _clock, tasks, instances = _build_runtime()
    # Materialise the (MEDIUM) task, then queue a CRITICAL task of a second
    # case at the same step (a step holds only one open task, so the sibling
    # needs a case of its own).
    medium = runtime.fetch_and_lock("scout", ["t"], lock_ms=1)[0]
    runtime.unlock(medium.id, "scout")
    case = instances.get(iid)
    assert case is not None
    instances.put(case.model_copy(update={"id": "instance_critical"}))
    critical = medium.model_copy(
        update={
            "id": "et_high",
            "instance_id": "instance_critical",
            "state": ExternalTaskState.CREATED,
            "worker_id": None,
            "lock_expires_at": None,
            "priority": WorkItemPriority(
                impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH
            ).level,
        }
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



def _incident_started_by(sid: str, topic: str, starter: dict[str, str],
                         worker: dict[str, str]) -> str:
    """Start ``sid`` as ``starter``; the worker fails its automatic step for good.

    Returns the id of the resulting incident.
    """

    client.post(f"/schemas/{sid}/instances", headers=starter)
    task = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
        headers=worker,
    ).json()[0]
    client.post(
        f"/v1/external-tasks/{task['id']}/failure",
        json={"worker_id": "w1", "error_message": "boom", "retries": 0},
        headers=worker,
    )
    return next(
        i["id"] for i in client.get("/v1/incidents", headers=worker).json()
        if i["external_task_id"] == task["id"]
    )


def test_uninvolved_operator_neither_sees_nor_resolves_foreign_incidents() -> None:
    """An operator sees and resolves only incidents of cases it is involved in."""

    from procworks.auth_password import (
        InMemoryCredentialStore,
        PasswordAuthBackend,
        hash_password,
    )

    backend = PasswordAuthBackend(InMemoryCredentialStore())
    import procworks.api as api_module

    original = api_module._auth_backend

    def login(name: str, roles: list[str], agent_id: str | None) -> dict[str, str]:
        backend.create_user(subject=name, login=name, roles=roles, agent_id=agent_id)
        user = backend.store.get_user(name)
        assert user is not None
        backend.store.put_user(
            user.model_copy(update={"password_hash": hash_password("secret-pw1"),
                                    "must_change": False})
        )
        return {"Authorization": f"Bearer {backend.login(name, 'secret-pw1').token}"}

    topic = _unique_topic()
    sid, _ = _external_task_schema(topic)  # built in open mode, before the switch
    try:
        api_module._auth_backend = backend
        admin = login("root", ["admin"], None)
        erika = login("erika", ["operator"], "a1")
        paul = login("paul", ["operator"], "a2")
        incident = _incident_started_by(sid, topic, erika, admin)  # erika started it

        assert all(i["id"] != incident for i in client.get("/v1/incidents", headers=paul).json())
        assert client.post(f"/v1/incidents/{incident}/resolve", headers=paul).status_code == 404
        assert any(i["id"] == incident for i in client.get("/v1/incidents", headers=erika).json())
        assert any(i["id"] == incident for i in client.get("/v1/incidents", headers=admin).json())
        assert client.post(f"/v1/incidents/{incident}/resolve", headers=erika).status_code == 200
    finally:
        api_module._auth_backend = original


# --- engine refusals reach the worker with their reason code ---------------


def test_runtime_engine_refusal_carries_the_engine_code_and_params() -> None:
    """Lehnt die Engine den Abschluss ab, kam beim Worker nur die englische
    Meldung an. Jetzt reisen ``code`` und ``params`` der Engine mit -- wie bei
    jeder anderen Engine-Ablehnung."""

    from procworks.model import NodeState

    runtime, iid, _clock, _tasks, instances = _build_runtime()
    task = runtime.fetch_and_lock("w1", ["t"], lock_ms=10_000)[0]
    instance = instances.get(iid)
    assert instance is not None
    instance.node_states[task.node_id] = NodeState.COMPLETED  # Schritt schon erledigt
    instances.put(instance)

    with pytest.raises(ExternalTaskError) as err:
        runtime.complete(task.id, "w1", {})

    assert err.value.status == 409
    assert err.value.code == "EX.not-ready"
    assert err.value.params == {"step": "Run"}
    assert "cannot be completed" in err.value.message


def test_the_api_passes_code_and_params_of_an_external_refusal() -> None:
    from fastapi import HTTPException

    import procworks.api as api_module

    def refuse() -> object:
        raise ExternalTaskError(
            "activity 'x' cannot be completed", 409, code="EX.not-ready",
            params={"step": "Run"},
        )

    with pytest.raises(HTTPException) as exc:
        api_module._run_external(refuse)

    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "message": "activity 'x' cannot be completed",
        "code": "EX.not-ready",
        "params": {"step": "Run"},
    }


def test_a_refusal_without_code_keeps_its_plain_answer() -> None:
    """Gegenprobe: ohne Code bleibt die Antwort unverändert (nur ``message``)."""

    from fastapi import HTTPException

    import procworks.api as api_module

    def refuse() -> object:
        raise ExternalTaskError("task 'x' is locked by another worker", 409)

    with pytest.raises(HTTPException) as exc:
        api_module._run_external(refuse)

    assert exc.value.detail == {"message": "task 'x' is locked by another worker"}


# --- every refusal of the external-task runtime names its reason ------------
#
# Lock and state conflicts, unknown tasks/incidents/instances and invalid
# outputs came back with an English ``message`` only. Each now carries a
# ``code`` the web client words from its catalogue; ``message`` stays as
# integrations know it.


def _locked_task(topic: str) -> dict[str, object]:
    sid, _ = _external_task_schema(topic)
    _start_with_betrag(sid, 5)
    tasks = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    ).json()
    assert len(tasks) == 1
    task: dict[str, object] = tasks[0]
    return task


def test_a_report_of_a_foreign_worker_names_the_lock_holder_conflict() -> None:
    task = _locked_task(_unique_topic())
    res = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w2", "variables": {}},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "ET.locked-by-other"
    assert res.json()["detail"]["message"] == f"task '{task['id']}' is locked by another worker"


def test_a_report_on_an_unlocked_task_names_its_state() -> None:
    task = _locked_task(_unique_topic())
    client.post(f"/v1/external-tasks/{task['id']}/unlock", json={"worker_id": "w1"})
    res = client.post(
        f"/v1/external-tasks/{task['id']}/extend-lock",
        json={"worker_id": "w1", "lock_ms": 1000},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "ET.not-locked"
    assert res.json()["detail"]["params"] == {"state": "CREATED"}


def test_an_expired_lock_is_refused_with_its_own_code() -> None:
    runtime, _iid, clock, _tasks, _instances = _build_runtime()
    task = runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000)[0]
    clock.t = 1006.0
    with pytest.raises(ExternalTaskError) as err:
        runtime.complete(task.id, "w1", {})
    assert (err.value.status, err.value.code) == (409, "ET.lock-expired")


def test_a_report_for_a_vanished_instance_says_so() -> None:
    runtime, iid, _clock, _tasks, instances = _build_runtime()
    task = runtime.fetch_and_lock("w1", ["t"], lock_ms=5_000)[0]
    instances._rows.pop(iid)  # gone behind the runtime's back
    with pytest.raises(ExternalTaskError) as err:
        runtime.complete(task.id, "w1", {})
    assert (err.value.status, err.value.code) == (409, "ET.instance-gone")


@pytest.mark.parametrize(
    ("variables", "code", "params"),
    [
        ({"betrag": 3}, "ET.output-not-writable", {"name": "betrag", "step": "Run"}),
        ({"approved": "ja"}, "ET.output-type", {"name": "approved", "type": "BOOLEAN"}),
    ],
)
def test_an_invalid_output_is_refused_with_the_offending_variable(
    variables: dict[str, object], code: str, params: dict[str, str]
) -> None:
    task = _locked_task(_unique_topic())
    res = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": variables},
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == code
    assert res.json()["detail"]["params"] == params
    assert client.get(f"/v1/external-tasks/{task['id']}").json()["state"] == "LOCKED"


@pytest.mark.parametrize(
    ("method", "path", "kind"),
    [
        ("get", "/v1/external-tasks/et_missing", "external_task"),
        ("post", "/v1/external-tasks/et_missing/unlock", "external_task"),
        ("post", "/v1/incidents/inc_missing/resolve", "incident"),
        ("get", "/v1/instances/inst_missing", "instance"),
        ("get", "/v1/instances/inst_missing/data", "instance"),
    ],
)
def test_unknown_tasks_incidents_and_instances_answer_404_with_a_code(
    method: str, path: str, kind: str
) -> None:
    body = {"json": {"worker_id": "w1"}} if method == "post" else {}
    res = getattr(client, method)(path, **body)
    assert res.status_code == 404
    detail = res.json()["detail"]
    assert detail["code"] == "OP.not-found"
    assert detail["params"]["kind"] == kind
    assert detail["message"]  # the technical text stays for integrations


def test_the_internal_instance_404_keeps_its_plain_answer() -> None:
    """Gegenprobe: ausserhalb von ``/v1`` bleibt die 404 unveraendert."""

    res = client.get("/instances/inst_missing")
    assert res.status_code == 404
    assert res.json()["detail"] == "instance 'inst_missing' not found"


def test_a_foreign_incident_looks_exactly_like_a_missing_one() -> None:
    import procworks.api as api_module
    from procworks.auth_password import (
        InMemoryCredentialStore,
        PasswordAuthBackend,
        hash_password,
    )

    backend = PasswordAuthBackend(InMemoryCredentialStore())
    original = api_module._auth_backend

    def login(name: str, roles: list[str], agent_id: str | None) -> dict[str, str]:
        backend.create_user(subject=name, login=name, roles=roles, agent_id=agent_id)
        user = backend.store.get_user(name)
        assert user is not None
        backend.store.put_user(
            user.model_copy(update={"password_hash": hash_password("secret-pw1"),
                                    "must_change": False})
        )
        return {"Authorization": f"Bearer {backend.login(name, 'secret-pw1').token}"}

    topic = _unique_topic()
    sid, _ = _external_task_schema(topic)
    try:
        api_module._auth_backend = backend
        admin = login("root2", ["admin"], None)
        erika = login("erika2", ["operator"], "a1")
        paul = login("paul2", ["operator"], "a2")
        incident = _incident_started_by(sid, topic, erika, admin)
        foreign = client.post(f"/v1/incidents/{incident}/resolve", headers=paul)
        missing = client.post("/v1/incidents/inc_nope/resolve", headers=paul)
    finally:
        api_module._auth_backend = original
    assert foreign.status_code == missing.status_code == 404
    # Same code, kind and wording; only the echoed id differs.
    def shape(detail: dict[str, object]) -> tuple[object, ...]:
        params = detail["params"]
        assert isinstance(params, dict)
        message = str(detail["message"]).replace(str(params["name"]), "<id>")
        return detail["code"], params["kind"], message

    assert shape(foreign.json()["detail"]) == shape(missing.json()["detail"])
    assert foreign.json()["detail"]["code"] == "OP.not-found"
