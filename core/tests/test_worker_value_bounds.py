# SPDX-License-Identifier: BUSL-1.1
"""The numbers a worker sends are bounded like the modelled ones.

The modelled back-off, request timeout and retry budget of an automatic step
have upper bounds; the values a *worker* names in its requests did not:

* ``lock_ms`` (fetch-and-lock, extend-lock) and ``retry_timeout_ms`` (failure)
  of ``10**400`` could not be turned into a point in time -- the request
  ended in a 500;
* ``10**13`` was accepted and parked a task for centuries, without an
  incident;
* ``retries`` (failure) took any budget, unlike ``retry_max`` in the model.

Now the request models bound them (``lock_ms`` and ``retry_timeout_ms`` at
most one day, ``retries`` at most :data:`procworks.model.MAX_RETRY_MAX`).
A larger or non-finite value is a 422 that names the field and the bound
(``type`` ``less_than_equal``), never a 500; the bound itself is accepted.
"""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from test_external_tasks import _external_task_schema, _start_with_betrag

from procworks.api import app
from procworks.model import MAX_LOCK_MS, MAX_RETRY_BACKOFF_MS, MAX_RETRY_MAX

client = TestClient(app)
_topics = (f"bound-topic-{n}" for n in itertools.count(1))

#: Values that are too large: one above the bound, a huge integer, and the
#: raw JSON literal ``1e400`` (a non-finite float once parsed).
_TOO_LARGE = ["above", "huge", "1e400"]


def _raw(value: str, bound: int) -> str:
    """The JSON spelling of a test value relative to ``bound``."""

    return {"above": str(bound + 1), "huge": str(10**400), "1e400": "1e400"}[value]


def _post_raw(path: str, body: str) -> Response:
    return client.post(path, content=body, headers={"content-type": "application/json"})


def _locked_task() -> tuple[str, str]:
    """A locked external task of a fresh topic: ``(task_id, topic)``."""

    topic = next(_topics)
    sid, _ = _external_task_schema(topic)
    _start_with_betrag(sid, 5)
    [task] = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 60_000},
    ).json()
    return str(task["id"]), topic


def _refusal_names(resp: Response, field: str, bound: int) -> None:
    """The 422 names ``field``; for a finite too-large number also the bound."""

    assert resp.status_code == 422, resp.text
    [error] = [e for e in resp.json()["detail"] if e["loc"][-1] == field]
    assert error["type"] in ("less_than_equal", "finite_number")
    if error["type"] == "less_than_equal":
        assert error["ctx"]["le"] == bound


def test_the_bounds() -> None:
    assert MAX_LOCK_MS == 24 * 3600 * 1000
    assert MAX_RETRY_BACKOFF_MS == 24 * 3600 * 1000
    assert MAX_RETRY_MAX == 100


@pytest.mark.parametrize("value", _TOO_LARGE)
def test_fetch_and_lock_refuses_a_lock_above_a_day(value: str) -> None:
    topic = next(_topics)
    sid, _ = _external_task_schema(topic)
    _start_with_betrag(sid, 5)
    resp = _post_raw(
        "/v1/external-tasks/fetch-and-lock",
        f'{{"worker_id": "w1", "topics": ["{topic}"], "lock_ms": {_raw(value, MAX_LOCK_MS)}}}',
    )
    _refusal_names(resp, "lock_ms", MAX_LOCK_MS)
    # Nothing was locked: the task is still there for a well-behaved worker.
    [task] = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w2", "topics": [topic], "lock_ms": 60_000},
    ).json()
    assert task["worker_id"] == "w2"


def test_fetch_and_lock_accepts_exactly_a_day() -> None:
    topic = next(_topics)
    sid, _ = _external_task_schema(topic)
    _start_with_betrag(sid, 5)
    resp = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": MAX_LOCK_MS},
    )
    assert resp.status_code == 200
    [task] = resp.json()
    assert task["state"] == "LOCKED"


@pytest.mark.parametrize("value", _TOO_LARGE)
def test_extend_lock_refuses_a_lock_above_a_day(value: str) -> None:
    task_id, _ = _locked_task()
    resp = _post_raw(
        f"/v1/external-tasks/{task_id}/extend-lock",
        f'{{"worker_id": "w1", "lock_ms": {_raw(value, MAX_LOCK_MS)}}}',
    )
    _refusal_names(resp, "lock_ms", MAX_LOCK_MS)


def test_extend_lock_accepts_exactly_a_day() -> None:
    task_id, _ = _locked_task()
    resp = client.post(
        f"/v1/external-tasks/{task_id}/extend-lock",
        json={"worker_id": "w1", "lock_ms": MAX_LOCK_MS},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "LOCKED"


@pytest.mark.parametrize("value", _TOO_LARGE)
def test_failure_refuses_a_retry_timeout_above_a_day(value: str) -> None:
    task_id, _ = _locked_task()
    resp = _post_raw(
        f"/v1/external-tasks/{task_id}/failure",
        '{"worker_id": "w1", "error_message": "down", "retries": 3, '
        f'"retry_timeout_ms": {_raw(value, MAX_RETRY_BACKOFF_MS)}}}',
    )
    _refusal_names(resp, "retry_timeout_ms", MAX_RETRY_BACKOFF_MS)
    # The report was refused as a whole: the worker still holds the task.
    task = client.get(f"/v1/external-tasks/{task_id}").json()
    assert (task["state"], task["worker_id"]) == ("LOCKED", "w1")


def test_failure_accepts_a_retry_timeout_of_exactly_a_day() -> None:
    task_id, _ = _locked_task()
    resp = client.post(
        f"/v1/external-tasks/{task_id}/failure",
        json={"worker_id": "w1", "error_message": "down", "retries": 3,
              "retry_timeout_ms": MAX_RETRY_BACKOFF_MS},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "CREATED"


@pytest.mark.parametrize("value", _TOO_LARGE)
def test_failure_refuses_a_retry_budget_above_the_model_bound(value: str) -> None:
    task_id, _ = _locked_task()
    resp = _post_raw(
        f"/v1/external-tasks/{task_id}/failure",
        f'{{"worker_id": "w1", "error_message": "down", "retries": {_raw(value, MAX_RETRY_MAX)}}}',
    )
    _refusal_names(resp, "retries", MAX_RETRY_MAX)


def test_failure_accepts_the_model_bound_as_budget() -> None:
    task_id, _ = _locked_task()
    resp = client.post(
        f"/v1/external-tasks/{task_id}/failure",
        json={"worker_id": "w1", "error_message": "down", "retries": MAX_RETRY_MAX},
    )
    assert resp.status_code == 200
    assert resp.json()["retries_left"] == MAX_RETRY_MAX


def test_negative_and_zero_keep_their_old_refusal() -> None:
    """Negativ: the lower bounds are unchanged (``lock_ms`` >= 1, ``retry_timeout_ms`` >= 0)."""

    task_id, _ = _locked_task()
    resp = client.post(
        f"/v1/external-tasks/{task_id}/extend-lock", json={"worker_id": "w1", "lock_ms": 0}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "greater_than_equal"
