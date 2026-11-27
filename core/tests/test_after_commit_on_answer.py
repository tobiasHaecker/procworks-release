# SPDX-License-Identifier: BUSL-1.1
"""A unit that commits on a deliberate answer still delivers what it committed.

An endpoint may end with an ``HTTPException`` *after* it changed something on
purpose -- the failed attempt of an external task is recorded (the task turns
into an incident), then the request answers 502. The unit of work commits
those writes (``commit_on``). The deliveries registered for them (webhooks,
mail, pushes, :func:`procworks.unit_of_work.after_commit`) ran only when the
block ended normally, so exactly the incident notice stayed in the outbox
until some unrelated later request happened to drive it -- on a quiet system
never.

Pinned here: after a commit the deliveries run, whatever the block raised; the
answer itself reaches the caller unchanged. A unit that rolls back delivers
nothing.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend

import procworks.api as api_module
from procworks import db, unit_of_work
from procworks.api import app
from procworks.integration_runtime import ExternalTaskError, ExternalTaskRuntime
from procworks.model import ExternalTask
from procworks.outbox import OutboxDispatcher
from procworks.store import InMemoryWebhookStore
from procworks.unit_of_work import ConcurrentModification

client = TestClient(app)


# --- the unit itself ------------------------------------------------------------


def test_a_deliberate_answer_commits_and_then_delivers() -> None:
    delivered: list[str] = []
    answer = HTTPException(status_code=502, detail="external system failed")

    with pytest.raises(HTTPException) as raised:
        with unit_of_work.unit_of_work(commit_on=(HTTPException,)):
            unit_of_work.after_commit("webhooks", lambda: delivered.append("webhooks"))
            unit_of_work.after_commit("mail", lambda: delivered.append("mail"))
            unit_of_work.after_commit("push", lambda: delivered.append("push"))
            raise answer

    assert raised.value is answer  # the answer reaches the caller unchanged
    assert delivered == ["webhooks", "mail", "push"]


def test_deliveries_run_after_the_serialising_lock_is_released() -> None:
    """Deliveries talk to the outside; they never hold up the other writers."""

    lock = threading.Lock()
    held_during_delivery: list[bool] = []

    with pytest.raises(HTTPException):
        with unit_of_work.unit_of_work(serialize=lock, commit_on=(HTTPException,)):
            unit_of_work.after_commit(
                "webhooks", lambda: held_during_delivery.append(lock.locked())
            )
            raise HTTPException(status_code=409, detail="no")

    assert held_during_delivery == [False]


def test_a_unit_that_rolls_back_delivers_nothing() -> None:
    delivered: list[str] = []

    with pytest.raises(RuntimeError, match="unexpected"):
        with unit_of_work.unit_of_work(commit_on=(HTTPException,)):
            unit_of_work.after_commit("webhooks", lambda: delivered.append("webhooks"))
            raise RuntimeError("unexpected")

    assert delivered == []


def test_a_noted_conflict_outranks_the_answer_and_delivers_nothing() -> None:
    """The answer was built on a stale state: rolled back, repeated, not announced."""

    delivered: list[str] = []
    conflict = ConcurrentModification("instance", "instance_7")

    with pytest.raises(ConcurrentModification) as raised:
        with unit_of_work.unit_of_work(commit_on=(HTTPException,)) as uow:
            unit_of_work.after_commit("webhooks", lambda: delivered.append("webhooks"))
            uow.note_conflict(conflict)
            raise HTTPException(status_code=502, detail="external system failed")

    assert raised.value is conflict
    assert delivered == []


def test_a_failing_delivery_never_replaces_the_answer() -> None:
    delivered: list[str] = []

    def broken() -> None:
        raise OSError("subscriber unreachable")

    with pytest.raises(HTTPException) as raised:
        with unit_of_work.unit_of_work(commit_on=(HTTPException,)):
            unit_of_work.after_commit("webhooks", broken)
            unit_of_work.after_commit("mail", lambda: delivered.append("mail"))
            raise HTTPException(status_code=502, detail="external system failed")

    assert raised.value.status_code == 502
    assert delivered == ["mail"]  # one broken delivery does not stop the others


# --- through the API: the incident notice of a failed pre-fetch -------------------


class _Recorder:
    """Webhook transport that records instead of sending (no network)."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        del url, headers, timeout
        self.events.append(json.loads(body)["event"])
        return 200


@pytest.fixture(params=["memory", "sqlite"])
def recorder(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> _Recorder:
    """The API (in memory or on SQLite) with a subscriber of the task events."""

    sent = _Recorder()
    if request.param == "sqlite":
        url = use_sql_backend(monkeypatch, tmp_path)
        store: object = db.SqlAlchemyWebhookStore(url, create_tables=True)
    else:
        store = InMemoryWebhookStore()
    outbox = OutboxDispatcher(store, transport=sent)  # type: ignore[arg-type]  # both stores satisfy the protocol
    outbox.subscribe(
        "https://hooks.example.org/procworks", ["task.ready", "task.incident"], ""
    )
    monkeypatch.setattr(api_module, "_outbox", outbox)

    def failing_prefetch(self: ExternalTaskRuntime, task: ExternalTask) -> None:
        raise ExternalTaskError(
            "pre-fetch of 'kunde' failed: lookup: external system failed", 502
        )

    monkeypatch.setattr(ExternalTaskRuntime, "_prefetch_external", failing_prefetch)
    return sent


def _external_task_case(topic: str) -> str:
    sid = client.post("/schemas", json={"name": f"Abholen {topic}"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Export", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Export")
    for path, body in (
        ("service", {"node_id": act, "name": "Export", "automatic": True}),
        (
            "automation",
            {"node_id": act, "automation": "EXTERNAL_TASK", "topic": topic, "retry_max": 0},
        ),
    ):
        resp = client.post(f"/schemas/{sid}/{path}", json=body)
        assert resp.status_code == 200, resp.text
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    resp = client.post(f"/schemas/{sid}/instances")
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def test_the_incident_of_a_failed_fetch_is_delivered_without_a_further_request(
    recorder: _Recorder,
) -> None:
    iid = _external_task_case("ausgabe-incident")

    resp = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w-1", "topics": ["ausgabe-incident"], "max_tasks": 1},
    )

    # The deliberate answer is unchanged ...
    assert resp.status_code == 502, resp.text
    assert "pre-fetch" in resp.json()["detail"]["message"]
    # ... the failed attempt was committed (the task is an incident) ...
    incidents = client.get("/v1/incidents").json()
    assert [i["instance_id"] for i in incidents] == [iid]
    # ... and its notice went out with this very request (the task is
    # created when the worker asks for it, so its "ready" goes out as well).
    assert recorder.events == ["task.ready", "task.incident"]
    assert all(e.state.value == "DELIVERED" for e in api_module._outbox._store.list_entries())
