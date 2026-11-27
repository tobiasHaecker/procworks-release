# SPDX-License-Identifier: BUSL-1.1
"""A test instance of a draft never reaches the outside -- and only modellers see it.

A modeller who plays through a revision of a productive process as a test
instance uses the same topics and push targets as the real process. Its
automatic steps used to become real external tasks: real workers fetched them
(and booked, wrote, sent ...), and subscribers received ``task.ready`` and
``task.completed``. Now:

* a test instance materialises no external task, pushes nothing and announces
  no ``task.*`` event; a task stored for one earlier is never handed out;
* its automatic steps are completed by hand in the test view, without a
  reason (it is a simulation);
* only modellers and administrators read or act on a test instance -- the
  people who may start one. An operator who would be responsible for one of
  its steps, and a viewer, get 404 like for a missing instance, and do not see
  it in their instance list.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)
from procworks.model import ExternalTask

client = TestClient(app)
_names = (f"isolation-{n}" for n in itertools.count(1))


class _Transport:
    """Records every outbound POST (no real network)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        self.calls.append(url)
        return 200


@pytest.fixture
def outside(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Transport, list[str]]]:
    """Capture pushes (transport) and emitted domain events (their types)."""

    transport = _Transport()
    events: list[str] = []
    monkeypatch.setattr(api_module._outbox, "_transport", transport)
    monkeypatch.setattr(api_module, "_emit_event", lambda kind, data: events.append(kind))
    api_module._outbox._circuit.clear()
    yield transport, events
    api_module._outbox._circuit.clear()


def _automatic_draft(kind: str, ref: str) -> tuple[str, str]:
    """Draft with one automatic step (``EXTERNAL_TASK`` on topic ``ref`` or
    ``HTTP_PUSH`` to endpoint ``ref``). Returns ``(schema_id, node_id)``."""

    sid = client.post("/schemas", json={"name": f"Entwurf {ref}"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Buchen", "after_node_id": "start"}
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node_id = next(nid for nid, n in nodes.items() if n["type"] == "ACTIVITY")
    client.post(f"/schemas/{sid}/service",
                json={"node_id": node_id, "name": "Buchen", "automatic": True})
    key = "topic" if kind == "EXTERNAL_TASK" else "endpoint_ref"
    set_ok = client.post(f"/schemas/{sid}/automation",
                         json={"node_id": node_id, "automation": kind, key: ref})
    assert set_ok.status_code == 200, set_ok.text
    return sid, node_id


def _tasks_of(instance_id: str) -> list[ExternalTask]:
    return [t for t in api_module._external_tasks.list_tasks() if t.instance_id == instance_id]


def test_a_test_instance_creates_no_external_task(outside: tuple[_Transport, list[str]]) -> None:
    _transport, events = outside
    topic = next(_names)
    sid, _ = _automatic_draft("EXTERNAL_TASK", topic)

    test = client.post(f"/schemas/{sid}/instances").json()
    fetched = client.post("/v1/external-tasks/fetch-and-lock",
                          json={"worker_id": "w1", "topics": [topic], "lock_ms": 60_000})

    assert test["is_test"] is True
    assert fetched.status_code == 200 and fetched.json() == []
    assert _tasks_of(test["id"]) == []
    assert not [e for e in events if e.startswith("task.")]


def test_a_test_instance_pushes_nothing(outside: tuple[_Transport, list[str]]) -> None:
    transport, events = outside
    ref = next(_names)
    api_module._push_endpoints.register(ref, "https://tool.example.com/push")
    sid, _ = _automatic_draft("HTTP_PUSH", ref)

    test = client.post(f"/schemas/{sid}/instances").json()
    client.post("/v1/external-tasks/drive-push")

    assert transport.calls == []
    assert _tasks_of(test["id"]) == []
    assert not [e for e in events if e.startswith("task.")]


def test_an_old_task_of_a_test_instance_is_never_handed_out() -> None:
    topic = next(_names)
    sid, node_id = _automatic_draft("EXTERNAL_TASK", topic)
    test = client.post(f"/schemas/{sid}/instances").json()
    # As stored by an earlier version, which still materialised it.
    api_module._external_tasks.put(
        ExternalTask(id=f"et_{topic}", instance_id=test["id"], node_id=node_id, topic=topic)
    )

    fetched = client.post("/v1/external-tasks/fetch-and-lock",
                          json={"worker_id": "w1", "topics": [topic], "lock_ms": 60_000})

    assert fetched.json() == []


def test_its_automatic_step_is_simulated_by_hand_without_a_reason() -> None:
    topic = next(_names)
    sid, node_id = _automatic_draft("EXTERNAL_TASK", topic)
    test = client.post(f"/schemas/{sid}/instances").json()

    done = client.post(f"/instances/{test['id']}/complete", json={"node_id": node_id})

    assert done.status_code == 200, done.text
    assert done.json()["state"] == "COMPLETED"
    assert api_module._audit.for_instance(test["id"]) == []


def test_the_released_process_still_creates_its_task(
    outside: tuple[_Transport, list[str]],
) -> None:
    _transport, events = outside
    topic = next(_names)
    sid, _ = _automatic_draft("EXTERNAL_TASK", topic)
    assert client.post(f"/schemas/{sid}/release").status_code == 200

    real = client.post(f"/schemas/{sid}/instances").json()
    fetched = client.post("/v1/external-tasks/fetch-and-lock",
                          json={"worker_id": "w1", "topics": [topic], "lock_ms": 60_000})

    assert real["is_test"] is False
    assert [t["instance_id"] for t in fetched.json()] == [real["id"]]
    assert "task.ready" in events


# --- who reads a test instance -------------------------------------------


@pytest.fixture
def login() -> Iterator[Any]:
    """``login(name, roles, agent_id)`` -> headers; password mode from the first call."""

    backend = PasswordAuthBackend(InMemoryCredentialStore())
    original = api_module._auth_backend

    def _login(name: str, roles: list[str], agent_id: str | None = None) -> dict[str, str]:
        api_module._auth_backend = backend
        backend.create_user(subject=name, login=name, roles=roles, agent_id=agent_id)
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


def _staffed_test_instance() -> tuple[str, str]:
    """Test instance of a draft whose step "Erfassen" agent ``a1`` may work.

    Built in open mode. Returns ``(instance_id, node_id)``.
    """

    sid = client.post("/schemas", json={"name": next(_names)}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    node_id = next(nid for nid, n in schema["nodes"].items() if n["label"] == "Erfassen")
    client.post(f"/schemas/{sid}/roles", json={"name": "sb", "role_id": "sb"})
    client.post(f"/schemas/{sid}/agents",
                json={"name": "a1", "role_ids": ["sb"], "agent_id": "a1"})
    client.post(f"/schemas/{sid}/staff-rule",
                json={"node_id": node_id, "rule": {"kind": "ROLE", "ref": "sb"}})
    staff_via_api(client, sid)
    test = client.post(f"/schemas/{sid}/instances").json()
    assert test["is_test"] is True
    return test["id"], node_id


def test_a_responsible_operator_does_not_see_a_test_instance(login: Any) -> None:
    iid, node_id = _staffed_test_instance()
    missing = "instance_never_there"
    erika = login("erika", ["operator"], "a1")

    read = client.get(f"/instances/{iid}", headers=erika)
    done = client.post(f"/instances/{iid}/complete", json={"node_id": node_id}, headers=erika)

    absent = client.get(f"/instances/{missing}", headers=erika)
    assert (read.status_code, done.status_code) == (404, 404)
    assert read.text.replace(iid, "<id>") == absent.text.replace(missing, "<id>")
    assert iid not in client.get("/instances", headers=erika).json()
    stored = api_module._instances.get(iid)
    assert stored is not None and stored.node_states[node_id] == "ACTIVATED"


def test_a_viewer_does_not_see_a_test_instance(login: Any) -> None:
    iid, _ = _staffed_test_instance()
    vera = login("vera", ["viewer"])

    assert client.get(f"/instances/{iid}", headers=vera).status_code == 404
    assert client.get(f"/v1/instances/{iid}", headers=vera).status_code == 404
    assert iid not in client.get("/instances", headers=vera).json()


def test_modellers_and_admins_work_with_test_instances(login: Any) -> None:
    iid, node_id = _staffed_test_instance()
    mara = login("mara", ["modeler"])
    ada = login("ada", ["admin"])

    assert client.get(f"/instances/{iid}", headers=ada).status_code == 200
    assert iid in client.get("/instances", headers=mara).json()
    done = client.post(f"/instances/{iid}/complete",
                       json={"node_id": node_id, "agent_id": "a1"}, headers=mara)
    assert done.status_code == 200, done.text


def test_a_modeller_simulates_an_automatic_step_without_a_reason(login: Any) -> None:
    """Outside a test instance this would be a supervision act with a mandatory
    reason; in a test instance it is the way to play it through."""

    topic = next(_names)
    sid, node_id = _automatic_draft("EXTERNAL_TASK", topic)
    test = client.post(f"/schemas/{sid}/instances").json()
    mara = login("mara", ["modeler"])

    done = client.post(f"/instances/{test['id']}/complete", json={"node_id": node_id},
                       headers=mara)

    assert done.status_code == 200, done.text
    assert done.json()["state"] == "COMPLETED"
