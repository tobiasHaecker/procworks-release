# SPDX-License-Identifier: BUSL-1.1
"""A task whose pre-fetch fails does not block the other tasks of the queue.

``fetch-and-lock`` reads the external data of each task it hands out. When that
read failed for one task, the whole call answered 502: every other task of the
topic (and of every other topic in the same call) stayed out of reach, and the
tasks already locked in that call were stored as LOCKED but never returned --
they hung until their lock ran out. Now the failing task counts as one failed
attempt (back-off, then an incident once its budget is used up) and is
skipped; the call hands out the healthy tasks up to ``max_tasks``.

A push follows the same rule: a failing pre-fetch before the delivery counts
as a failed attempt. Before, the push task stayed ``CREATED`` without using
its budget and was retried on every drive -- forever, without an incident.
"""

from __future__ import annotations

import pytest

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
from procworks.execution import ExecutionContext
from procworks.integration_runtime import ExternalTaskError, ExternalTaskRuntime
from procworks.model import (
    AccessMode,
    AutomationKind,
    DataType,
    ExternalTask,
    ExternalTaskState,
    NodeType,
    ProcessInstance,
    ProcessSchema,
)
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Runtime(ExternalTaskRuntime):
    """Runtime whose pre-fetch fails for the instances named in ``broken``."""

    broken: set[str]

    def _prefetch_external(self, task: ExternalTask) -> None:
        if task.instance_id in self.broken:
            raise ExternalTaskError(
                f"pre-fetch of 'kunde' failed: lookup: external system failed "
                f"(OperationalError) [{task.instance_id}]",
                502,
            )


def _schema(
    schemas: InMemorySchemaStore,
    topic: str,
    retry_max: int,
    kind: AutomationKind = AutomationKind.EXTERNAL_TASK,
) -> ProcessSchema:
    schema = create_empty_schema(f"Abholen {topic}", schema_id=f"fetch-{topic}")
    schema = serial_insert(schema, "Pruefen", after_node_id="start")
    node_id = next(n.id for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    schema = add_data_element(schema, "Ok", DataType.BOOLEAN, element_id="ok")
    schema = assign_service(schema, node_id, "Pruefen", automatic=True)
    if kind is AutomationKind.EXTERNAL_TASK:
        schema = set_automation(schema, node_id, kind, topic=topic, retry_max=retry_max)
    else:
        schema = set_automation(
            schema, node_id, kind, endpoint_ref=topic, retry_max=retry_max,
            retry_backoff_ms=10_000,
        )
    schema = connect_data(schema, node_id, "ok", AccessMode.WRITE, mandatory=False)
    schema = release(schema)
    schemas.put(schema)
    return schema


def _setup(
    topics: list[str],
    *,
    retry_max: int = 3,
    kind: AutomationKind = AutomationKind.EXTERNAL_TASK,
    on_push: object | None = None,
) -> tuple[_Runtime, _Clock, list[str]]:
    """One running instance per entry of ``topics`` (in that order); with
    ``kind`` ``HTTP_PUSH`` the topic is the endpoint reference."""

    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    context = ExecutionContext(make_resolver(schemas), instances)
    built: dict[str, ProcessSchema] = {}
    ids: list[str] = []
    for topic in topics:
        if topic not in built:
            built[topic] = _schema(schemas, topic, retry_max, kind)
        instance = instantiate(built[topic], context=context)
        instances.put(instance)
        ids.append(instance.id)

    def schema_for(inst: ProcessInstance) -> ProcessSchema:
        resolved = schemas.get(inst.schema_id)
        assert resolved is not None
        return resolved

    clock = _Clock()
    runtime = _Runtime(
        InMemoryExternalTaskStore(), instances, schema_for, context, now=clock,
        on_push=on_push,  # type: ignore[arg-type]  # test sink, duck-typed
    )
    runtime.broken = set()
    return runtime, clock, ids


def _task_of(runtime: ExternalTaskRuntime, instance_id: str) -> ExternalTask:
    [task] = [t for t in runtime.list_tasks() if t.instance_id == instance_id]
    return task


def test_a_failing_pre_fetch_is_skipped_and_the_healthy_task_is_handed_out() -> None:
    runtime, clock, (bad, good) = _setup(["kunden", "kunden"])
    runtime.broken = {bad}

    locked = runtime.fetch_and_lock("w1", ["kunden"], lock_ms=60_000, max_tasks=1)

    assert [t.instance_id for t in locked] == [good]
    failed = _task_of(runtime, bad)
    assert failed.state is ExternalTaskState.CREATED
    assert failed.worker_id is None
    assert failed.retries_left == 2
    assert failed.available_at is not None and failed.available_at > clock.t


def test_tasks_locked_before_the_failure_are_returned_not_left_hanging() -> None:
    runtime, _clock, (good, bad, other) = _setup(["kunden", "kunden", "kunden"])
    runtime.broken = {bad}

    locked = runtime.fetch_and_lock("w1", ["kunden"], lock_ms=60_000, max_tasks=3)

    assert sorted(t.instance_id for t in locked) == sorted([good, other])
    for instance_id in (good, other):
        task = _task_of(runtime, instance_id)
        assert (task.state, task.worker_id) == (ExternalTaskState.LOCKED, "w1")
    assert _task_of(runtime, bad).state is ExternalTaskState.CREATED


def test_a_broken_topic_does_not_block_another_topic_in_the_same_call() -> None:
    runtime, _clock, (bad, good) = _setup(["kaputt", "gesund"])
    runtime.broken = {bad}

    locked = runtime.fetch_and_lock("w1", ["kaputt", "gesund"], lock_ms=60_000, max_tasks=2)

    assert [t.instance_id for t in locked] == [good]


def test_with_nothing_to_hand_out_the_worker_still_learns_why() -> None:
    runtime, clock, (bad,) = _setup(["kunden"])
    runtime.broken = {bad}

    with pytest.raises(ExternalTaskError) as exc:
        runtime.fetch_and_lock("w1", ["kunden"], lock_ms=60_000)

    assert exc.value.status == 502
    assert "pre-fetch of 'kunde' failed" in exc.value.message
    # ... and the attempt is counted: the task waits out its back-off.
    task = _task_of(runtime, bad)
    assert (task.state, task.retries_left) == (ExternalTaskState.CREATED, 2)
    assert task.available_at is not None and task.available_at > clock.t
    assert runtime.fetch_and_lock("w1", ["kunden"], lock_ms=60_000) == []


def test_a_pre_fetch_that_keeps_failing_ends_in_an_incident() -> None:
    runtime, clock, (bad,) = _setup(["kunden"], retry_max=2)
    runtime.broken = {bad}

    for _ in range(2):
        with pytest.raises(ExternalTaskError):
            runtime.fetch_and_lock("w1", ["kunden"], lock_ms=60_000)
        task = _task_of(runtime, bad)
        if task.available_at is not None:
            clock.t = task.available_at

    task = _task_of(runtime, bad)
    assert task.state is ExternalTaskState.INCIDENT
    [incident] = runtime.list_incidents(unresolved_only=True)
    assert incident.external_task_id == task.id
    assert "pre-fetch of 'kunde' failed" in incident.message


def test_without_a_failure_every_task_is_handed_out_as_before() -> None:
    runtime, _clock, ids = _setup(["kunden", "kunden"])

    locked = runtime.fetch_and_lock("w1", ["kunden"], lock_ms=60_000, max_tasks=5)

    assert sorted(t.instance_id for t in locked) == sorted(ids)
    assert runtime.list_incidents() == []


# --- push -----------------------------------------------------------------------


def _push_setup(retry_max: int = 3) -> tuple[_Runtime, _Clock, str, list[object]]:
    delivered: list[object] = []

    def sink(binding: object, payload: dict[str, object]) -> bool:
        delivered.append(payload)
        return True

    runtime, clock, (bad,) = _setup(
        ["erp-push"], retry_max=retry_max, kind=AutomationKind.HTTP_PUSH, on_push=sink
    )
    runtime.broken = {bad}
    return runtime, clock, bad, delivered


def test_a_failing_pre_fetch_before_a_push_counts_as_a_failed_attempt() -> None:
    runtime, clock, bad, delivered = _push_setup()

    assert runtime.drive_push() == []

    task = _task_of(runtime, bad)
    assert (task.state, task.retries_left, task.worker_id) == (
        ExternalTaskState.CREATED, 2, None
    )
    assert task.available_at == pytest.approx(clock.t + 10.0)
    assert delivered == []
    # Not due again before its back-off: a second drive leaves it alone.
    runtime.drive_push()
    assert _task_of(runtime, bad).retries_left == 2


def test_a_push_whose_pre_fetch_keeps_failing_ends_in_an_incident() -> None:
    runtime, clock, bad, delivered = _push_setup(retry_max=2)

    for _ in range(2):
        runtime.drive_push()
        task = _task_of(runtime, bad)
        if task.available_at is not None:
            clock.t = task.available_at

    task = _task_of(runtime, bad)
    assert task.state is ExternalTaskState.INCIDENT
    [incident] = runtime.list_incidents(unresolved_only=True)
    assert "pre-fetch of 'kunde' failed" in incident.message
    assert delivered == []
