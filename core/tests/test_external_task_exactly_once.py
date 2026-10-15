# SPDX-License-Identifier: BUSL-1.1
"""Every external task is handed out exactly once, and every step has one task.

Workers poll ``fetch-and-lock`` in parallel -- several threads of one worker,
several worker instances, several API processes. Each call used to read the
queue, pick the first claimable task and store it as ``LOCKED`` without any
condition, and to create the task of a newly activated step after checking
"no open task yet" on what it had just read. Two calls that read the same
state therefore both locked the same task (two workers ran the same payment
or booking), and both created a task for the same step (each further
parallel call added one more, and those copies stayed fetchable after the
case had ended).

Pinned here, against the in-memory stores and against the SQLAlchemy stores
on an SQLite file:

* the store contract -- a step holds at most one open task (the database
  refuses a second one, it is not left to the caller's check), a save from a
  stale read is refused, and a conditional save reports a lost race instead
  of overwriting the winner;
* the races, forced with a barrier after the read: two fetches of one task,
  two first fetches that both create the step's task, many fetches over many
  cases, two push drives;
* a task whose case has ended (or whose step is no longer active) is never
  handed out or pushed again, it is closed as ``CANCELLED``;
* a second worker gets a locked task only after the lock has expired;
* a push whose tool reports back while the push is still running keeps the
  completion.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend

import procworks.api as api_module
from procworks import (
    add_data_element,
    assign_service,
    connect_data,
    create_empty_schema,
    db,
    instantiate,
    release,
    serial_insert,
    set_automation,
)
from procworks.api import app
from procworks.execution import ExecutionContext, complete_activity
from procworks.integration_runtime import ExternalTaskRuntime
from procworks.model import (
    AccessMode,
    AutomationKind,
    DataType,
    ExternalTask,
    ExternalTaskState,
    InstanceState,
    NodeType,
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
from procworks.unit_of_work import ConcurrentModification

client = TestClient(app)

_names = (f"genau-einmal-{n}" for n in itertools.count(1))

#: Seconds a barrier waits for the other threads. Where the code under test
#: serialises the callers (in-memory API units), the wait simply ends.
_BARRIER_TIMEOUT = 1.0


class _Clock:
    """A controllable clock (epoch seconds) for lock expiry and back-off."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _cancelled() -> ExternalTaskState:
    """The end state of a task that is no longer needed (looked up by value)."""

    return ExternalTaskState("CANCELLED")


# --- building blocks ----------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """``"memory"`` or the URL of a fresh SQLite file."""

    if request.param == "memory":
        return "memory"
    return f"sqlite:///{tmp_path / 'tasks.db'}"


def _stores(backend: str) -> tuple[Any, Any, Any]:
    """Schema, instance and task store on ``backend`` (see :func:`backend`)."""

    if backend == "memory":
        return InMemorySchemaStore(), InMemoryInstanceStore(), InMemoryExternalTaskStore()
    return (
        db.SqlAlchemySchemaStore(backend, create_tables=True),
        db.SqlAlchemyInstanceStore(backend, create_tables=True),
        db.SqlAlchemyExternalTaskStore(backend, create_tables=True),
    )


class _World:
    """A released one-step automatic process with ``cases`` running cases."""

    def __init__(
        self,
        backend: str,
        *,
        cases: int = 1,
        kind: AutomationKind = AutomationKind.EXTERNAL_TASK,
        on_push: Callable[[ServiceBinding, dict[str, object]], bool | None] | None = None,
    ) -> None:
        self.schemas, self.instances, self.tasks = _stores(backend)
        schema = create_empty_schema("Genau einmal", schema_id="s1")
        schema = serial_insert(schema, "Buchen", after_node_id="start")
        self.node_id = next(
            nid for nid, node in schema.nodes.items() if node.type is NodeType.ACTIVITY
        )
        schema = add_data_element(schema, "Betrag", DataType.INTEGER, element_id="betrag")
        schema = assign_service(schema, self.node_id, "Buchen", automatic=True)
        if kind is AutomationKind.EXTERNAL_TASK:
            schema = set_automation(schema, self.node_id, kind, topic="buchen")
        else:
            schema = set_automation(schema, self.node_id, kind, endpoint_ref="erp")
        schema = connect_data(schema, self.node_id, "betrag", AccessMode.READ, mandatory=False)
        schema = release(schema)
        self.schemas.put(schema)
        self.context = ExecutionContext(make_resolver(self.schemas), self.instances)
        self.case_ids: list[str] = []
        for n in range(cases):
            instance = instantiate(schema, context=self.context)
            instance.data_values["betrag"] = n
            self.instances.put(instance)
            self.case_ids.append(instance.id)
        self.clock = _Clock()
        self.on_push = on_push

    def schema_for(self, instance: ProcessInstance) -> ProcessSchema:
        resolved = self.schemas.get(instance.schema_id)
        assert resolved is not None
        return resolved

    def runtime(self) -> ExternalTaskRuntime:
        """A fresh runtime over the shared stores (the API builds one per request)."""

        return ExternalTaskRuntime(
            self.tasks,
            self.instances,
            self.schema_for,
            self.context,
            now=self.clock,
            on_push=self.on_push,
        )

    def open_tasks_per_step(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for task in self.tasks.list_tasks():
            if task.state in (
                ExternalTaskState.CREATED,
                ExternalTaskState.LOCKED,
                ExternalTaskState.INCIDENT,
                ExternalTaskState.BPMN_ERROR,
            ):
                key = (task.instance_id, task.node_id)
                counts[key] = counts.get(key, 0) + 1
        return counts


def _hold_first_reads(
    monkeypatch: pytest.MonkeyPatch, target: object, name: str, parties: int
) -> None:
    """Hold the first ``parties`` calls of ``target.name`` until all have arrived.

    Placed on the read of the queue: every caller then works on the state it
    read before any of them writes -- the interleaving that handed one task to
    several workers. Later calls pass straight through.
    """

    original = getattr(target, name)
    barrier = threading.Barrier(parties, timeout=_BARRIER_TIMEOUT)
    counter = itertools.count()
    lock = threading.Lock()

    def held(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with lock:
            call = next(counter)
        if call < parties:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return result

    monkeypatch.setattr(target, name, held)


def _parallel[T](calls: list[Callable[[], T]]) -> list[T]:
    """Run ``calls`` at the same time and return their results in order."""

    with ThreadPoolExecutor(len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result() for future in futures]


# --- the store contract -------------------------------------------------------


def _task(task_id: str, *, instance_id: str = "i1", node_id: str = "n1") -> ExternalTask:
    return ExternalTask(id=task_id, instance_id=instance_id, node_id=node_id, topic="t")


def test_a_step_holds_at_most_one_open_task(backend: str) -> None:
    _, _, tasks = _stores(backend)

    assert tasks.add(_task("et_1")) is True
    assert tasks.add(_task("et_2")) is False  # same step, the first is still open
    assert [t.id for t in tasks.list_tasks()] == ["et_1"]
    assert tasks.add(_task("et_3", node_id="n2")) is True  # another step
    assert tasks.add(_task("et_4", instance_id="i2")) is True  # another case

    # Once the first task has ended, the step may get its next one (a loop round).
    first = tasks.get("et_1")
    first.state = ExternalTaskState.COMPLETED
    tasks.put(first)
    assert tasks.add(_task("et_5")) is True


def test_a_stale_task_save_is_refused_and_the_first_save_survives(backend: str) -> None:
    _, _, tasks = _stores(backend)
    tasks.add(_task("et_1"))
    first = tasks.get("et_1")
    second = tasks.get("et_1")

    first.state, first.worker_id = ExternalTaskState.LOCKED, "w1"
    tasks.put(first)
    second.state, second.worker_id = ExternalTaskState.LOCKED, "w2"
    with pytest.raises(ConcurrentModification) as refused:
        tasks.put(second)

    assert (refused.value.kind, refused.value.key) == ("external_task", "et_1")
    assert tasks.get("et_1").worker_id == "w1"


def test_a_lost_conditional_save_says_so_and_keeps_the_winner(backend: str) -> None:
    _, _, tasks = _stores(backend)
    tasks.add(_task("et_1"))
    first = tasks.get("et_1")
    second = tasks.get("et_1")

    first.worker_id = "w1"
    assert tasks.put_if_unchanged(first) is True
    second.worker_id = "w2"
    assert tasks.put_if_unchanged(second) is False
    assert tasks.get("et_1").worker_id == "w1"
    # The winner may save again on top of its own save.
    first.worker_id = "w1-again"
    assert tasks.put_if_unchanged(first) is True


def test_a_new_task_never_replaces_a_stored_one(backend: str) -> None:
    _, _, tasks = _stores(backend)
    tasks.add(_task("et_1"))

    assert tasks.add(_task("et_1", node_id="n9")) is False
    with pytest.raises(ConcurrentModification):
        tasks.put(_task("et_1", node_id="n9"))
    assert tasks.get("et_1").node_id == "n1"


def test_a_loaded_task_is_detached_from_the_store(backend: str) -> None:
    _, _, tasks = _stores(backend)
    tasks.add(_task("et_1"))

    loaded = tasks.get("et_1")
    loaded.worker_id = "changed but not saved"

    assert tasks.get("et_1").worker_id is None
    assert [t.worker_id for t in tasks.list_tasks()] == [None]


# --- races of the runtime -----------------------------------------------------


def test_two_concurrent_fetches_hand_one_task_to_one_worker(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _World(backend)
    world.runtime().sync()  # the task exists, both fetches find it CREATED
    _hold_first_reads(monkeypatch, world.tasks, "list_tasks", 2)

    handed = _parallel(
        [
            lambda: world.runtime().fetch_and_lock("w1", ["buchen"], lock_ms=60_000),
            lambda: world.runtime().fetch_and_lock("w2", ["buchen"], lock_ms=60_000),
        ]
    )

    ids = [task.id for batch in handed for task in batch]
    assert len(ids) == 1, handed
    [stored] = world.tasks.list_tasks()
    assert stored.state is ExternalTaskState.LOCKED
    winner = next(w for w, batch in zip(("w1", "w2"), handed, strict=True) if batch)
    assert stored.worker_id == winner


def test_two_concurrent_first_fetches_create_one_task_for_the_step(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _World(backend)
    # Both read "no open task" before either creates one.
    _hold_first_reads(monkeypatch, world.tasks, "list_tasks", 2)

    handed = _parallel(
        [
            lambda: world.runtime().fetch_and_lock("w1", ["buchen"], lock_ms=60_000),
            lambda: world.runtime().fetch_and_lock("w2", ["buchen"], lock_ms=60_000),
        ]
    )

    assert len(world.tasks.list_tasks()) == 1
    assert sum(len(batch) for batch in handed) == 1


def test_many_concurrent_fetches_hand_out_every_task_once(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = 8
    world = _World(backend, cases=workers)
    _hold_first_reads(monkeypatch, world.tasks, "list_tasks", workers)

    handed = _parallel(
        [
            (lambda w=w: world.runtime().fetch_and_lock(f"w{w}", ["buchen"], lock_ms=60_000))
            for w in range(workers)
        ]
    )

    ids = [task.id for batch in handed for task in batch]
    assert len(ids) == len(set(ids)), "a task went to more than one worker"
    assert all(count == 1 for count in world.open_tasks_per_step().values())
    assert len(world.tasks.list_tasks()) == workers
    # Whatever was not handed out now is still there for the next call, once.
    rest = world.runtime().fetch_and_lock("late", ["buchen"], lock_ms=60_000, max_tasks=50)
    assert sorted(ids + [t.id for t in rest]) == sorted(t.id for t in world.tasks.list_tasks())


# --- the API path (one unit of work per request) ------------------------------


def _external_task_schema() -> tuple[str, str]:
    topic = next(_names)
    sid = client.post("/schemas", json={"name": f"Einmal {topic}"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Run", "after_node_id": "start"})
    schema = client.get(f"/schemas/{sid}").json()
    node_id = next(nid for nid, n in schema["nodes"].items() if n["type"] == "ACTIVITY")
    client.post(f"/schemas/{sid}/service",
                json={"node_id": node_id, "name": "Run", "automatic": True})
    client.post(f"/schemas/{sid}/automation",
                json={"node_id": node_id, "automation": "EXTERNAL_TASK", "topic": topic})
    released = client.post(f"/schemas/{sid}/release")
    assert released.status_code == 200, released.text
    return sid, topic


@pytest.fixture(params=["memory", "sqlite"])
def api_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> str:
    """Run the API test once on the in-memory stores and once on SQLite."""

    if request.param == "sqlite":
        use_sql_backend(monkeypatch, tmp_path)
    return str(request.param)


def test_two_concurrent_fetch_requests_hand_the_task_to_one_worker(
    api_backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, topic = _external_task_schema()
    started = client.post(f"/v1/schemas/{sid}/instances", json={})
    assert started.status_code == 201, started.text
    _hold_first_reads(monkeypatch, api_module._external_tasks, "list_tasks", 2)

    def fetch(worker: str) -> httpx.Response:
        return client.post(
            "/v1/external-tasks/fetch-and-lock",
            json={"worker_id": worker, "topics": [topic], "lock_ms": 60_000},
        )

    answers = _parallel([lambda: fetch("w1"), lambda: fetch("w2")])

    assert [a.status_code for a in answers] == [200, 200], [a.text for a in answers]
    ids = [task["id"] for a in answers for task in a.json()]
    assert len(ids) == 1, [a.json() for a in answers]
    mine = [t for t in api_module._external_tasks.list_tasks() if t.topic == topic]
    assert len(mine) == 1
    assert mine[0].id == ids[0] and mine[0].state is ExternalTaskState.LOCKED


# --- tasks that are no longer needed ------------------------------------------


def test_a_task_of_a_finished_case_is_never_handed_out_and_is_cancelled() -> None:
    world = _World("memory")
    runtime = world.runtime()
    [task] = runtime.fetch_and_lock("w1", ["buchen"], lock_ms=60_000)
    runtime.complete(task.id, "w1", {})
    case = world.instances.get(world.case_ids[0])
    assert case is not None and case.state is InstanceState.COMPLETED
    # A copy left behind by an earlier version (the step's own task is done).
    assert world.tasks.add(
        ExternalTask(id="et_left", instance_id=case.id, node_id=world.node_id, topic="buchen")
    )

    assert world.runtime().fetch_and_lock("w2", ["buchen"], lock_ms=60_000) == []

    left = world.tasks.get("et_left")
    assert left.state is _cancelled()
    assert left.cancel_reason == "step-ended"


def test_a_task_whose_step_was_finished_another_way_is_cancelled() -> None:
    world = _World("memory")
    world.runtime().sync()
    # The step is completed without closing its task (as the open mode does).
    case = world.instances.get(world.case_ids[0])
    schema = world.schemas.get("s1")
    world.instances.put(complete_activity(case, schema, world.node_id, context=world.context))

    assert world.runtime().fetch_and_lock("w1", ["buchen"], lock_ms=60_000) == []
    [task] = world.tasks.list_tasks()
    assert (task.state, task.cancel_reason) == (_cancelled(), "step-ended")


def test_a_task_of_a_vanished_case_is_cancelled() -> None:
    world = _World("memory", cases=0)
    world.tasks.add(ExternalTask(id="et_orphan", instance_id="gone", node_id="n1", topic="buchen"))

    assert world.runtime().fetch_and_lock("w1", ["buchen"], lock_ms=60_000) == []
    assert world.tasks.get("et_orphan").state is _cancelled()


def test_a_cancelled_task_cannot_be_reported() -> None:
    world = _World("memory", cases=0)
    world.tasks.add(ExternalTask(id="et_orphan", instance_id="gone", node_id="n1", topic="buchen"))
    world.runtime().fetch_and_lock("w1", ["buchen"], lock_ms=60_000)

    from procworks.integration_runtime import ExternalTaskError

    with pytest.raises(ExternalTaskError) as refused:
        world.runtime().complete("et_orphan", "w1", {})
    assert refused.value.code == "ET.not-locked"
    assert refused.value.params == {"state": "CANCELLED"}


# --- locks ----------------------------------------------------------------------


def test_a_second_worker_gets_a_locked_task_only_after_the_lock_expired() -> None:
    world = _World("memory")
    [task] = world.runtime().fetch_and_lock("w1", ["buchen"], lock_ms=10_000)

    world.clock.t += 9.999
    assert world.runtime().fetch_and_lock("w2", ["buchen"], lock_ms=10_000) == []
    world.clock.t += 0.001
    [again] = world.runtime().fetch_and_lock("w2", ["buchen"], lock_ms=10_000)

    assert again.id == task.id
    assert world.tasks.get(task.id).worker_id == "w2"


# --- resuming after an incident ------------------------------------------------


def test_an_incident_keeps_the_step_s_one_task_and_resolving_requeues_it(
    backend: str,
) -> None:
    world = _World(backend)
    runtime = world.runtime()
    [task] = runtime.fetch_and_lock("w1", ["buchen"], lock_ms=60_000)
    runtime.failure(task.id, "w1", "down", retries=0)
    # The incident still occupies the step: no second task is created for it.
    assert world.runtime().fetch_and_lock("w2", ["buchen"], lock_ms=60_000) == []
    assert len(world.tasks.list_tasks()) == 1
    [incident] = runtime.list_incidents(unresolved_only=True)

    world.runtime().resolve_incident(incident.id)
    [again] = world.runtime().fetch_and_lock("w2", ["buchen"], lock_ms=60_000)

    assert again.id == task.id
    assert len(world.tasks.list_tasks()) == 1


def test_of_two_concurrent_resolutions_one_requeues_the_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = _World(f"sqlite:///{tmp_path / 'resolve.db'}")
    runtime = world.runtime()
    [task] = runtime.fetch_and_lock("w1", ["buchen"], lock_ms=60_000)
    runtime.failure(task.id, "w1", "down", retries=0)
    [incident] = runtime.list_incidents(unresolved_only=True)
    _hold_first_reads(monkeypatch, world.tasks, "get", 2)

    def resolve() -> str:
        try:
            world.runtime().resolve_incident(incident.id)
        except ConcurrentModification:
            return "conflict"
        return "resolved"

    outcomes = _parallel([resolve, resolve])

    # The later save is refused (an API request would be repeated and then
    # find the task already queued) -- never two budgets on one task.
    assert sorted(outcomes) == ["conflict", "resolved"]
    stored = world.tasks.get(task.id)
    assert stored.state is ExternalTaskState.CREATED


# --- push -----------------------------------------------------------------------


class _Sink:
    """Push sink: records every delivery, optionally waits for a partner."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.lock = threading.Lock()
        self.during: Callable[[dict[str, object]], None] | None = None

    def __call__(self, binding: ServiceBinding, payload: dict[str, object]) -> bool:
        with self.lock:
            self.calls.append(payload)
        if self.during is not None:
            self.during(payload)
        return True


def test_two_concurrent_push_drives_push_a_step_once(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _Sink()
    world = _World(backend, kind=AutomationKind.HTTP_PUSH, on_push=sink)
    world.runtime().sync()  # nothing for push; materialise through a first read
    # Both drives read the queue (after materialising) before either pushes.
    _hold_first_reads(monkeypatch, world.tasks, "list_tasks", 4)

    _parallel([lambda: world.runtime().drive_push(), lambda: world.runtime().drive_push()])

    assert len(sink.calls) == 1, sink.calls
    [task] = world.tasks.list_tasks()
    assert task.state is ExternalTaskState.LOCKED
    assert task.worker_id == sink.calls[0]["callback_token"]


def test_a_report_arriving_while_the_push_is_running_keeps_the_completion(
    backend: str,
) -> None:
    sink = _Sink()
    world = _World(backend, kind=AutomationKind.HTTP_PUSH, on_push=sink)
    # The tool answers at once, before the push call has returned.
    sink.during = lambda payload: world.runtime().complete(
        str(payload["task_id"]), str(payload["callback_token"]), {}
    )

    world.runtime().drive_push()

    [task] = world.tasks.list_tasks()
    assert task.state is ExternalTaskState.COMPLETED
    case = world.instances.get(world.case_ids[0])
    assert case is not None and case.state is InstanceState.COMPLETED


def test_an_abandoned_push_is_pushed_again_once_its_claim_expired() -> None:
    sink = _Sink()
    world = _World("memory", kind=AutomationKind.HTTP_PUSH, on_push=sink)
    world.runtime().drive_push()
    [task] = world.tasks.list_tasks()
    # A process that claimed the push and died before delivering it.
    task.state, task.worker_id = ExternalTaskState.LOCKED, "push_dead"
    task.lock_expires_at = world.clock.t + 1
    world.tasks.put(task)

    world.runtime().drive_push()
    assert len(sink.calls) == 1  # the claim still holds
    world.clock.t += 2
    world.runtime().drive_push()

    assert len(sink.calls) == 2
    stored = world.tasks.get(task.id)
    assert stored.worker_id == sink.calls[1]["callback_token"]
    assert stored.lock_expires_at is None  # delivered: waits for the tool, no expiry


def test_a_delivered_push_is_not_pushed_again() -> None:
    sink = _Sink()
    world = _World("memory", kind=AutomationKind.HTTP_PUSH, on_push=sink)
    world.runtime().drive_push()
    world.clock.t += 10_000

    world.runtime().drive_push()

    assert len(sink.calls) == 1


def test_a_push_task_of_a_finished_case_is_cancelled_not_pushed() -> None:
    sink = _Sink()
    world = _World("memory", kind=AutomationKind.HTTP_PUSH, on_push=sink)
    world.runtime().sync()
    world.tasks.add(
        ExternalTask(id="et_push_left", instance_id="gone", node_id=world.node_id, topic="")
    )

    world.runtime().drive_push()

    assert [c["task_id"] for c in sink.calls] != ["et_push_left"]
    assert all(c["task_id"] != "et_push_left" for c in sink.calls)
    assert world.tasks.get("et_push_left").state is _cancelled()


# --- web client -------------------------------------------------------------------


def test_the_client_names_every_task_state() -> None:
    """A refused report names the task's state (``ET.not-locked``) in German."""

    from web_source import app_js_source

    source = app_js_source()
    names = source[source.index("const EXTERNAL_TASK_STATE_NAMES = {"):]
    names = names[: names.index("};")]
    missing = [state.value for state in ExternalTaskState if f"{state.value}:" not in names]
    assert not missing, missing
