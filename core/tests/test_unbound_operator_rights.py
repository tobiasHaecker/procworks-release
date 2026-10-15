# SPDX-License-Identifier: BUSL-1.1
"""Worklists and absences of a person belong to that person (and supervision).

``/agents/{id}/tasks`` and ``/agents/{id}/absences`` are self-service for a
bound operator and supervision for modelers and administrators. A personal
operator login *without* an agent binding is neither the person nor a
supervisor: it must not read anybody's worklist (tasks with their context) nor
record or remove anybody's absence (which steers who receives tasks). The
open dev mode and machine identities (static tokens, role ``integration``)
keep their unrestricted access -- they are the documented integration path.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web_vm import needs_node, run_app_js

import procworks.api as api_module
from procworks import (
    add_agent,
    add_role,
    assign_staff_rule,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
)
from procworks.api import app
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)
from procworks.auth_token import TokenAuthBackend
from procworks.model import AbsenceEntry, StaffRule, StaffRuleKind

client = TestClient(app)

#: login -> (roles, agent binding); every password is ``<login>-pw-1``.
LOGINS: dict[str, tuple[list[str], str | None]] = {
    "admin": (["admin"], None),
    "mara": (["modeler"], None),
    "frei": (["operator"], None),
    "erika": (["operator"], "a1"),
    "revision": (["operator", "viewer"], None),
}


def _window() -> dict[str, str]:
    now = datetime.now(UTC)
    return {
        "start_at": (now - timedelta(hours=1)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
    }


@pytest.fixture
def world() -> Iterator[str]:
    """One running step offered to Erika (``a1``) plus one absence of ``a2``.

    :returns: (via ``yield``) the id of the stored absence of ``a2``.
    """

    for store in (api_module._store, api_module._instances, api_module._absence_store,
                  api_module._audit):
        store.clear()
    schema = create_empty_schema("Rechte", schema_id="rechte")
    schema = serial_insert(schema, "Bearbeiten", after_node_id="start")
    act = next(n.id for n in schema.nodes.values() if n.label == "Bearbeiten")
    schema = add_role(schema, "Sachbearbeitung", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1")
    schema = add_agent(schema, "Nina", role_ids=["sb"], agent_id="a2")
    schema = assign_staff_rule(schema, act, StaffRule(kind=StaffRuleKind.ROLE, ref="sb"))
    rel = release(schema)
    api_module._store.put(rel)
    api_module._instances.put(instantiate(rel))
    now = datetime.now(UTC)
    entry = api_module._absence_store.put_entry(AbsenceEntry(
        id="abs_nina", agent_id="a2", start_at=now, end_at=now + timedelta(days=1)))

    backend = PasswordAuthBackend(InMemoryCredentialStore())
    for login, (roles, agent) in LOGINS.items():
        user, _ = backend.create_user(subject=login, login=login, roles=roles, agent_id=agent)
        backend.store.put_user(user.model_copy(
            update={"password_hash": hash_password(f"{login}-pw-1"), "must_change": False}))
    original = api_module._auth_backend
    api_module._auth_backend = backend
    try:
        yield entry.id
    finally:
        api_module._auth_backend = original
        for store in (api_module._store, api_module._instances,
                      api_module._absence_store, api_module._audit):
            store.clear()


def _as(login: str) -> dict[str, str]:
    backend = api_module._auth_backend
    assert isinstance(backend, PasswordAuthBackend)
    return {"Authorization": f"Bearer {backend.login(login, f'{login}-pw-1').token}"}


def _absence_ids(agent_id: str) -> list[str]:
    return [e.id for e in api_module._list_absences(agent_id)]


def _assert_forbidden_by_agent_guard(resp_status: int, body: object) -> None:
    """403 from the person guard: plain ``forbidden`` (the role gate let it pass)."""

    assert resp_status == 403, body
    assert body == {"detail": "forbidden"}


def test_unbound_operator_cannot_read_foreign_worklists_or_absences(world: str) -> None:
    headers = _as("frei")
    # The role gate admits the login (own, empty views work) ...
    assert client.get("/me/tasks", headers=headers).json() == []
    assert client.get("/me/absences", headers=headers).json() == []
    # ... the person guard refuses everybody else's.
    for path in ("/agents/a1/tasks", "/agents/a2/absences"):
        resp = client.get(path, headers=headers)
        _assert_forbidden_by_agent_guard(resp.status_code, resp.json())


def test_unbound_operator_cannot_record_or_remove_absences(world: str) -> None:
    headers = _as("frei")
    created = client.post("/agents/a1/absences", json=_window(), headers=headers)
    _assert_forbidden_by_agent_guard(created.status_code, created.json())
    assert _absence_ids("a1") == []
    removed = client.delete(f"/agents/a2/absences/{world}", headers=headers)
    _assert_forbidden_by_agent_guard(removed.status_code, removed.json())
    assert _absence_ids("a2") == [world]


@pytest.mark.parametrize("login", ["admin", "mara"])
def test_supervisors_keep_access(world: str, login: str) -> None:
    headers = _as(login)
    tasks = client.get("/agents/a1/tasks", headers=headers)
    assert tasks.status_code == 200 and len(tasks.json()) == 1
    assert client.get("/agents/a2/absences", headers=headers).status_code == 200
    created = client.post("/agents/a1/absences", json=_window(), headers=headers)
    assert created.status_code == 201
    assert client.delete(
        f"/agents/a1/absences/{created.json()['id']}", headers=headers
    ).status_code == 204


def test_bound_operator_sees_own_but_not_foreign(world: str) -> None:
    headers = _as("erika")
    own = client.get("/agents/a1/tasks", headers=headers)
    assert own.status_code == 200 and len(own.json()) == 1
    assert client.get("/agents/a1/absences", headers=headers).status_code == 200
    foreign = client.get("/agents/a2/absences", headers=headers)
    _assert_forbidden_by_agent_guard(foreign.status_code, foreign.json())


def test_open_mode_stays_unrestricted(world: str) -> None:
    api_module._auth_backend = api_module.OpenAuthBackend()
    assert client.get("/agents/a1/tasks").status_code == 200
    assert client.get("/agents/a2/absences").status_code == 200


def test_unbound_machine_token_keeps_access(world: str) -> None:
    """Token mode is the integration path: an unbound token principal is unchanged."""

    api_module._auth_backend = TokenAuthBackend(
        {"bridge": {"subject": "hr-bridge", "roles": ["operator"]}}
    )
    headers = {"Authorization": "Bearer bridge"}
    assert client.get("/agents/a1/tasks", headers=headers).status_code == 200
    created = client.post("/agents/a1/absences", json=_window(), headers=headers)
    assert created.status_code == 201


def test_unknown_agent_gets_no_existence_hint(world: str) -> None:
    """Edge case: for the unbound operator an unknown id is just another foreign one."""

    resp = client.post("/agents/ghost/absences", json=_window(), headers=_as("frei"))
    _assert_forbidden_by_agent_guard(resp.status_code, resp.json())
    admin = client.post("/agents/ghost/absences", json=_window(), headers=_as("admin"))
    assert admin.status_code == 404


def test_unbound_login_cannot_return_a_colleagues_claim(world: str) -> None:
    """The same guard decides the override of a foreign claim.

    ``revision`` reads every instance (role ``viewer``), so the read gate lets
    it reach the claim; it is still neither the claimant nor a supervisor.
    """

    instance_id = api_module._instances.list_ids()[0]
    node_id = client.get("/agents/a1/tasks", headers=_as("erika")).json()[0]["node_id"]
    claimed = client.post(f"/instances/{instance_id}/claim", json={"node_id": node_id},
                          headers=_as("erika"))
    assert claimed.status_code == 200, claimed.json()
    resp = client.post(f"/instances/{instance_id}/return", json={"node_id": node_id},
                       headers=_as("revision"))
    _assert_forbidden_by_agent_guard(resp.status_code, resp.json())
    assert api_module._instances.get(instance_id).claimed_by.get(node_id) == "a1"


# --- web client -------------------------------------------------------------

_TASKS_VIEW = r"""
state.authMode = "password";
state.principal = { subject: "__LOGIN__", roles: __ROLES__ };
respond((p) => {
  if (p === "/directory/agents") return { body: [{ agent_id: "a1", name: "Erika" }] };
  if (p.endsWith("/tasks") || p.endsWith("/absences")) return { body: [] };
  return { status: 404 };
});
await viewTasks();
return {
  calls: calls.slice(), content: textOf(byId("content")), toasts: textOf(byId("toast-root")),
};
"""


@needs_node
def test_tasks_view_of_an_unbound_operator_asks_nobody_elses_list(tmp_path: Path) -> None:
    """The client does not offer a person picker the core would refuse."""

    snap = run_app_js(
        _TASKS_VIEW.replace("__LOGIN__", "frei").replace("__ROLES__", '["operator"]'), tmp_path
    )
    assert snap["calls"] == ["GET /directory/agents"], snap["calls"]
    assert "keinem Bearbeiter zugeordnet" in snap["content"]
    assert snap["toasts"] == ""


@needs_node
def test_tasks_view_of_an_unbound_supervisor_keeps_the_picker(tmp_path: Path) -> None:
    snap = run_app_js(
        _TASKS_VIEW.replace("__LOGIN__", "mara").replace("__ROLES__", '["modeler"]'), tmp_path
    )
    assert "GET /agents/a1/tasks" in snap["calls"], snap["calls"]
    assert "Aufgaben ansehen von" in snap["content"]
