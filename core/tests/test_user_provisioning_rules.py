# SPDX-License-Identifier: BUSL-1.1
"""A login is created only with a role, for an existing person, once per person.

Accounts that could never work were created silently: a login without any
role (403 everywhere), a login bound to a person that exists in no
organisation (an empty worklist without a hint), and a second login for a
person who already had one. The decision is: exactly one login per person,
a role is mandatory, the person must exist. A login without any person behind
it (an administrator, a modeller) stays possible.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from test_auth_password import _admin_login, password_mode  # noqa: F401 - fixture

import procworks.api as api_module
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    PasswordPolicyError,
)
from procworks.model import Agent, OrgModel

client = TestClient(api_module.app)


@pytest.fixture
def org() -> Iterator[str]:
    """A shared organisation with the persons ``p-erika`` and ``p-paul``."""

    model = OrgModel(
        id="org-provisioning",
        name="Provisioning",
        agents={
            "p-erika": Agent(id="p-erika", name="Erika Sander"),
            "p-paul": Agent(id="p-paul", name="Paul Klein"),
        },
    )
    api_module._org_store.put(model)
    try:
        yield "org-provisioning"
    finally:  # the in-memory registry has no delete; drop the test's entry
        # (its versioned rows -- a new org must never replace a stored one)
        rows = getattr(getattr(api_module._org_store, "_orgs", None), "_rows", {})
        rows.pop("org-provisioning", None)


def _create(backend: PasswordAuthBackend, body: dict[str, object]):  # type: ignore[no-untyped-def]
    headers = {"Authorization": f"Bearer {_admin_login(backend)}"}
    return client.post("/users", json=body, headers=headers)


def _logins(backend: PasswordAuthBackend) -> set[str]:
    return {u.login for u in backend.store.list_users()}


# --- refused, with the reason ----------------------------------------------------


def test_a_login_without_a_role_is_refused(password_mode: PasswordAuthBackend) -> None:  # noqa: F811
    before = _logins(password_mode)

    resp = _create(password_mode, {"login": "ohne.rolle", "roles": []})

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "USERS.no-role"
    assert _logins(password_mode) == before


def test_a_login_for_an_unknown_person_is_refused(
    password_mode: PasswordAuthBackend,  # noqa: F811
    org: str,
) -> None:
    before = _logins(password_mode)

    resp = _create(
        password_mode, {"login": "geist", "roles": ["operator"], "agent_id": "p-gibtsnicht"}
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "USERS.unknown-agent"
    assert resp.json()["detail"]["params"] == {"agent": "p-gibtsnicht"}
    assert _logins(password_mode) == before


def test_a_second_login_for_the_same_person_is_refused(
    password_mode: PasswordAuthBackend,  # noqa: F811
    org: str,
) -> None:
    first = _create(password_mode, {"login": "paul.klein", "roles": ["operator"],
                                     "agent_id": "p-paul"})
    assert first.status_code == 201, first.text
    before = _logins(password_mode)

    resp = _create(password_mode, {"login": "paul.zwei", "roles": ["viewer"],
                                    "agent_id": "p-paul"})

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "USERS.agent-taken"
    assert resp.json()["detail"]["params"] == {"agent": "Paul Klein", "login": "paul.klein"}
    assert _logins(password_mode) == before


# --- still possible ------------------------------------------------------------


def test_a_login_for_an_existing_person_is_created(
    password_mode: PasswordAuthBackend,  # noqa: F811
    org: str,
) -> None:
    resp = _create(password_mode, {"roles": ["operator"], "agent_id": "p-erika"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["agent_id"] == "p-erika"
    assert resp.json()["login"] == "erika.sander"


def test_a_login_without_a_person_stays_possible(password_mode: PasswordAuthBackend) -> None:  # noqa: F811
    resp = _create(password_mode, {"login": "mara.modell", "roles": ["modeler"]})

    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["agent_id"] is None


def test_several_logins_without_a_person_are_fine(password_mode: PasswordAuthBackend) -> None:  # noqa: F811
    assert _create(password_mode, {"login": "a.eins", "roles": ["viewer"]}).status_code == 201
    assert _create(password_mode, {"login": "a.zwei", "roles": ["viewer"]}).status_code == 201


# --- the backend enforces role and single binding on its own ---------------------


def test_the_backend_refuses_an_empty_role_set_and_a_double_binding() -> None:
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    with pytest.raises(PasswordPolicyError) as no_role:
        backend.create_user(subject="x", roles=[], login="x")
    assert no_role.value.code == "USERS.no-role"

    backend.create_user(subject="erika", roles=["operator"], login="erika", agent_id="a1")
    with pytest.raises(PasswordPolicyError) as taken:
        backend.create_user(subject="erika2", roles=["operator"], login="erika2", agent_id="a1")
    assert taken.value.code == "USERS.agent-taken"
    assert taken.value.params["login"] == "erika"
    assert {u.login for u in backend.store.list_users()} == {"erika"}
