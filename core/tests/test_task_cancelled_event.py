# SPDX-License-Identifier: BUSL-1.1
"""A discarded external task announces itself: ``task.cancelled``.

A task whose case or step ended without it (completed another way, case
finished, case gone) is closed as ``CANCELLED`` (``cancel_reason``
``"step-ended"``) instead of being handed out again. Integrations did not
learn of it: a worker that had listed the task waited for it in vain. Now
the closing emits ``task.cancelled`` (task, case, step, topic, ``reason``),
which webhooks can subscribe to like the other ``task.*`` events -- exactly
once per task (a parallel call that already changed the task wins, and then
nothing is announced). Tasks that the database migration of an older
version closed are not announced afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_external_task_exactly_once import _World

from procworks import complete_activity
from procworks.model import ExternalTask, ExternalTaskState
from procworks.outbox import WEBHOOK_EVENTS


class _Events:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, event_type: str, payload: dict[str, object]) -> None:
        self.calls.append((event_type, payload))

    def of(self, event_type: str) -> list[dict[str, object]]:
        return [p for t, p in self.calls if t == event_type]


def _runtime(world: _World, events: _Events) -> Any:
    runtime = world.runtime()
    runtime._emit = lambda event_type, **payload: events(event_type, payload)
    return runtime


def test_the_event_can_be_subscribed_to() -> None:
    assert "task.cancelled" in WEBHOOK_EVENTS


def test_a_task_whose_step_ended_another_way_announces_its_cancellation() -> None:
    world = _World("memory")
    events = _Events()
    world.runtime().sync()
    case = world.instances.get(world.case_ids[0])
    schema = world.schemas.get("s1")
    world.instances.put(complete_activity(case, schema, world.node_id, context=world.context))
    [task] = world.tasks.list_tasks()

    assert _runtime(world, events).fetch_and_lock("w1", ["buchen"], lock_ms=60_000) == []

    assert events.of("task.cancelled") == [{
        "task_id": task.id, "instance_id": case.id, "node_id": world.node_id,
        "topic": "buchen", "reason": "step-ended",
    }]


def test_a_cancelled_task_is_announced_once() -> None:
    world = _World("memory", cases=0)
    events = _Events()
    world.tasks.add(ExternalTask(id="et_orphan", instance_id="gone", node_id="n1", topic="buchen"))

    _runtime(world, events).fetch_and_lock("w1", ["buchen"], lock_ms=60_000)
    _runtime(world, events).fetch_and_lock("w2", ["buchen"], lock_ms=60_000)

    assert [p["task_id"] for p in events.of("task.cancelled")] == ["et_orphan"]


def test_nothing_is_announced_when_a_parallel_call_changed_the_task_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World("memory", cases=0)
    events = _Events()
    world.tasks.add(ExternalTask(id="et_orphan", instance_id="gone", node_id="n1", topic="buchen"))
    monkeypatch.setattr(world.tasks, "put_if_unchanged", lambda task: False)

    _runtime(world, events).fetch_and_lock("w1", ["buchen"], lock_ms=60_000)

    assert events.of("task.cancelled") == []


def test_a_push_task_of_a_finished_case_announces_its_cancellation() -> None:
    from procworks.model import AutomationKind

    world = _World("memory", kind=AutomationKind.HTTP_PUSH, on_push=lambda *_: True)
    events = _Events()
    world.tasks.add(
        ExternalTask(id="et_push_left", instance_id="gone", node_id=world.node_id, topic="")
    )

    _runtime(world, events).drive_push()

    assert [(p["task_id"], p["reason"]) for p in events.of("task.cancelled")] == [
        ("et_push_left", "step-ended")
    ]


def test_tasks_closed_by_the_migration_are_not_announced_afterwards(tmp_path: Path) -> None:
    """A task stored as CANCELLED already (e.g. by the database migration of an
    older version) is never picked up again -- no late event."""

    world = _World("memory", cases=0)
    events = _Events()
    world.tasks.add(ExternalTask(id="et_alt", instance_id="gone", node_id="n1", topic="buchen",
                                 state=ExternalTaskState.CANCELLED, cancel_reason="duplicate"))

    runtime = _runtime(world, events)
    runtime.sync()
    runtime.fetch_and_lock("w1", ["buchen"], lock_ms=60_000)
    runtime.drive_push()

    assert events.calls == []
