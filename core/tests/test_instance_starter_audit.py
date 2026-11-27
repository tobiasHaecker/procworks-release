# SPDX-License-Identifier: BUSL-1.1
"""Every start of an instance names its starter in the audit.

``INSTANCE_CREATED`` is the event that says who began a case. It carries the
starting agent (when the login is bound to one) and the login or token subject
as ``detail.actor`` -- on the legacy path and on ``/v1`` alike, for personal
logins, unbound logins and machine tokens. The bound agent also makes the
starter *involved*, so an operator who starts a case through ``/v1`` can read
it afterwards, exactly as on the legacy path; an uninvolved colleague still
gets 404.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app
from procworks.audit import AuditEvent, EventType
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)
from procworks.auth_token import TokenAuthBackend

client = TestClient(app)

_STARTER_TOKEN = "starter-token-geheim"
_TOKENS = {
    _STARTER_TOKEN: {
        "subject": "erp-starter",
        "roles": ["integration"],
        "scopes": ["instances:start", "data:read"],
    }
}


def _released_schema(name: str) -> str:
    """Released schema "Erfassen" (role sb: only a1 may work it); built in open mode.

    ``a2`` exists in the organisation but is responsible for nothing, so the
    only way for it to become involved in a case is to start it.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    erfassen = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    for role in ("sb", "other"):
        client.post(f"/schemas/{sid}/roles", json={"name": role, "role_id": role})
    for agent_id, role in (("a1", "sb"), ("a2", "other"), ("a3", "other")):
        client.post(
            f"/schemas/{sid}/agents",
            json={"name": agent_id, "role_ids": [role], "agent_id": agent_id},
        )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": erfassen, "rule": {"kind": "ROLE", "ref": "sb"}},
    )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid


@pytest.fixture
def login() -> Iterator[Any]:
    """``login(name, roles, agent_id)`` -> headers; password mode plus machine tokens."""

    backend = PasswordAuthBackend(
        InMemoryCredentialStore(), machine_tokens=TokenAuthBackend(_TOKENS)
    )
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


def _created(instance_id: str) -> AuditEvent:
    """The one ``INSTANCE_CREATED`` event of ``instance_id``."""

    events = [
        e
        for e in api_module._audit.for_instance(instance_id)
        if e.event_type is EventType.INSTANCE_CREATED
    ]
    assert len(events) == 1, events
    return events[0]


@pytest.mark.parametrize("path", ["/schemas/{sid}/instances", "/v1/schemas/{sid}/instances"])
def test_a_bound_operator_is_recorded_as_starter_and_reads_its_case(
    login: Any, path: str
) -> None:
    sid = _released_schema(f"Starter gebunden {path}")
    paul = login("paul", ["operator"], "a2")

    started = client.post(path.format(sid=sid), headers=paul)

    assert started.status_code == 201, started.text
    iid = started.json()["id"]
    event = _created(iid)
    assert event.agent_id == "a2"
    assert (event.detail or {}).get("actor") == "paul"
    assert client.get(f"/v1/instances/{iid}", headers=paul).status_code == 200
    assert client.get(f"/instances/{iid}", headers=paul).status_code == 200


def test_an_uninvolved_operator_still_gets_404_on_a_v1_started_case(login: Any) -> None:
    sid = _released_schema("Starter fremd")
    paul = login("paul", ["operator"], "a2")
    otto = login("otto", ["operator"], "a3")

    iid = client.post(f"/v1/schemas/{sid}/instances", headers=paul).json()["id"]

    assert client.get(f"/v1/instances/{iid}", headers=otto).status_code == 404
    assert client.get(f"/instances/{iid}", headers=otto).status_code == 404


def test_a_machine_token_is_recorded_as_starter(login: Any) -> None:
    sid = _released_schema("Starter Maschine")
    login("ada", ["admin"])  # switches to password mode with machine tokens

    started = client.post(
        f"/v1/schemas/{sid}/instances",
        headers={"Authorization": f"Bearer {_STARTER_TOKEN}"},
    )

    assert started.status_code == 201, started.text
    event = _created(started.json()["id"])
    assert event.agent_id is None
    assert (event.detail or {}).get("actor") == "erp-starter"


@pytest.mark.parametrize("path", ["/schemas/{sid}/instances", "/v1/schemas/{sid}/instances"])
def test_an_unbound_login_is_recorded_by_name_not_as_system(login: Any, path: str) -> None:
    sid = _released_schema(f"Starter ungebunden {path}")
    mara = login("mara", ["modeler"])

    started = client.post(path.format(sid=sid), headers=mara)

    assert started.status_code == 201, started.text
    event = _created(started.json()["id"])
    assert event.agent_id is None
    assert (event.detail or {}).get("actor") == "mara"


def test_the_open_mode_records_no_pseudo_actor() -> None:
    """Without any login there is no identity: no ``actor`` like ``anonymous``."""

    sid = _released_schema("Starter offen")

    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]

    event = _created(iid)
    assert event.agent_id is None
    assert "actor" not in (event.detail or {})
