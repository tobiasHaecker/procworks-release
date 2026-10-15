# SPDX-License-Identifier: BUSL-1.1
"""Completing an automatic step by hand is a supervision act -- and ends its task.

A step driven by an outside system (external task or push) used to be
completable through the ordinary completion like any other step: a modeller
finished it without a reason, the history showed a plain completion, and the
external task stayed locked -- after its lock ran out a second worker received
it, and its report then failed for good with 409, while the real system might
already have done the work twice. Now:

* only a modeller or administrator may complete such a step by hand, and only
  with a reason; it is recorded as ``ACTIVITY_SUPERVISED``;
* any open task of the step is closed (``COMPLETED``), its open incidents are
  resolved, and no worker can fetch or report it any more;
* an operator gets 409 ``EX.automatic``, exactly like for a claim;
* an automatic step **without** an external task (a plain service the
  integration completes through the regular API) behaves as before.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from invisible_text import INVISIBLE_REASONS, assert_reason_required

import procworks.api as api_module
from procworks.api import app
from procworks.audit import EventType
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)
from procworks.model import ExternalTaskState

client = TestClient(app)
_topics = (f"supervised-{n}" for n in itertools.count(1))


def _schema(topic: str | None) -> tuple[str, str]:
    """Released one-step schema; automatic with an external task on ``topic``.

    ``topic=None`` keeps the step automatic without any external task.
    Built in open mode. Returns ``(schema_id, node_id)``.
    """

    sid = client.post("/schemas", json={"name": f"Automatik {topic}"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Pruefen", "after_node_id": "start"}
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node_id = next(nid for nid, n in nodes.items() if n["type"] == "ACTIVITY")
    client.post(f"/schemas/{sid}/data-elements",
                json={"name": "Ok", "data_type": "BOOLEAN", "element_id": "ok"})
    client.post(f"/schemas/{sid}/data-access", json={
        "node_id": node_id, "element_id": "ok", "mode": "WRITE", "mandatory": False})
    client.post(f"/schemas/{sid}/service",
                json={"node_id": node_id, "name": "Pruefen", "automatic": True})
    if topic is not None:
        client.post(f"/schemas/{sid}/automation", json={
            "node_id": node_id, "automation": "EXTERNAL_TASK", "topic": topic})
    assert client.post(f"/schemas/{sid}/release").status_code == 200, sid
    return sid, node_id


@pytest.fixture
def login() -> Iterator[Any]:
    """``login(name, roles)`` -> headers; password mode from the first call."""

    backend = PasswordAuthBackend(InMemoryCredentialStore())
    original = api_module._auth_backend

    def _login(name: str, roles: list[str]) -> dict[str, str]:
        api_module._auth_backend = backend
        backend.create_user(subject=name, login=name, roles=roles, agent_id=None)
        user = backend.store.get_user(name)
        assert user is not None
        backend.store.put_user(
            user.model_copy(
                update={"password_hash": hash_password("secret-pw1"), "must_change": False}
            )
        )
        return {"Authorization": f"Bearer {backend.login(name, 'secret-pw1').token}"}

    try:
        yield _login
    finally:
        api_module._auth_backend = original


def _case(topic: str | None) -> tuple[str, str]:
    sid, node_id = _schema(topic)
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    return iid, node_id


def _fetch(topic: str, headers: dict[str, str], worker: str = "w1") -> list[dict[str, Any]]:
    resp = client.post("/v1/external-tasks/fetch-and-lock", headers=headers,
                       json={"worker_id": worker, "topics": [topic], "lock_ms": 300_000})
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def _complete(iid: str, node_id: str, headers: dict[str, str], reason: str | None = None) -> Any:
    body: dict[str, object] = {"node_id": node_id, "data": {"ok": True}}
    if reason is not None:
        body["supervision_reason"] = reason
    return client.post(f"/instances/{iid}/complete", json=body, headers=headers)


def _events(iid: str) -> list[EventType]:
    return [e.event_type for e in api_module._audit.for_instance(iid)]


def test_a_modeller_needs_a_reason(login: Any) -> None:
    topic = next(_topics)
    iid, node_id = _case(topic)
    mara = login("mara", ["modeler"])

    refused = _complete(iid, node_id, mara)

    assert refused.status_code == 422
    assert_reason_required(refused, "automatic", api_module.AUTOMATIC_SUPERVISION_REQUIRED)
    states = client.get(f"/instances/{iid}", headers=mara).json()["node_states"]
    assert states[node_id] == "ACTIVATED"
    assert [t["instance_id"] for t in _fetch(topic, mara)] == [iid]  # task untouched


def test_with_a_reason_it_is_recorded_as_supervision_and_closes_the_task(login: Any) -> None:
    topic = next(_topics)
    iid, node_id = _case(topic)
    mara = login("mara", ["modeler"])
    [task] = _fetch(topic, mara)  # a worker holds the lock

    done = _complete(iid, node_id, mara, reason="Dienst ausgefallen, Prüfung telefonisch erledigt")

    assert done.status_code == 200, done.text
    assert done.json()["state"] == "COMPLETED"
    supervised = [
        e for e in api_module._audit.for_instance(iid)
        if e.event_type is EventType.ACTIVITY_SUPERVISED
    ]
    assert [e.detail for e in supervised] == [
        {"actor": "mara", "reason": "Dienst ausgefallen, Prüfung telefonisch erledigt"}
    ]
    stored = api_module._external_tasks.get(task["id"])
    assert stored is not None and stored.state is ExternalTaskState.COMPLETED
    # The worker that still believed to hold it cannot report it any more ...
    late = client.post(f"/v1/external-tasks/{task['id']}/complete", headers=mara,
                       json={"worker_id": "w1", "variables": {"ok": True}})
    assert late.status_code == 409
    assert late.json()["detail"]["code"] == "ET.not-locked"
    assert late.json()["detail"]["params"] == {"state": "COMPLETED"}
    # ... and nobody can fetch it again.
    assert _fetch(topic, mara, worker="w2") == []


def test_the_incident_of_a_closed_task_is_resolved(login: Any) -> None:
    topic = next(_topics)
    iid, node_id = _case(topic)
    admin = login("ada", ["admin"])
    [task] = _fetch(topic, admin)
    client.post(f"/v1/external-tasks/{task['id']}/bpmn-error", headers=admin,
                json={"worker_id": "w1", "error_code": "ABGELEHNT"})
    assert [i for i in client.get("/v1/incidents?unresolved_only=true", headers=admin).json()
            if i["external_task_id"] == task["id"]]

    assert _complete(iid, node_id, admin, reason="fachlich geklärt").status_code == 200

    assert not [i for i in client.get("/v1/incidents?unresolved_only=true", headers=admin).json()
                if i["external_task_id"] == task["id"]]


def test_an_operator_cannot_complete_an_automatic_step(login: Any) -> None:
    topic = next(_topics)
    iid, node_id = _case(topic)
    otto = login("otto", ["operator", "viewer"])  # viewer: may read every case
    before = _events(iid)

    refused = _complete(iid, node_id, otto, reason="bitte")

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "EX.automatic"
    assert refused.json()["detail"]["params"] == {"step": "Pruefen"}
    assert _events(iid) == before
    mara = login("mara", ["modeler"])
    assert [t["instance_id"] for t in _fetch(topic, mara)] == [iid]


def test_an_automatic_step_without_external_task_completes_as_before(login: Any) -> None:
    iid, node_id = _case(None)
    otto = login("otto", ["operator", "viewer"])

    done = _complete(iid, node_id, otto)

    assert done.status_code == 200, done.text
    assert EventType.ACTIVITY_SUPERVISED not in _events(iid)


@pytest.mark.parametrize(
    "invisible", [text for _, text in INVISIBLE_REASONS], ids=[i for i, _ in INVISIBLE_REASONS]
)
def test_an_invisible_reason_does_not_count(login: Any, invisible: str) -> None:
    topic = next(_topics)
    iid, node_id = _case(topic)
    mara = login("mara", ["modeler"])

    refused = _complete(iid, node_id, mara, reason=invisible)

    assert refused.status_code == 422
    assert_reason_required(refused, "automatic", api_module.AUTOMATIC_SUPERVISION_REQUIRED)
    states = client.get(f"/instances/{iid}", headers=mara).json()["node_states"]
    assert states[node_id] == "ACTIVATED"
