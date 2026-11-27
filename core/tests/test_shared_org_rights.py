# SPDX-License-Identifier: BUSL-1.1
"""Modellers maintain a shared organisation; creating one stays with admins.

The modeller guide and the agreed rights say a modeller maintains the
organisation a process uses -- also a shared one. The API refused every
change of a shared organisation to anybody but an administrator, while the
interface showed the buttons. Now a modeller may change the *content* of a
shared organisation (roles, departments, managers, hierarchy, persons,
deputies, mailboxes); creating a whole shared organisation stays an
administrator's matter (there is no endpoint to delete one). The checks
against every linked process (Z1/Z2) apply to a modeller as to anyone.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from web_vm import needs_node, run_app_js

import procworks.api as api_module
from procworks.auth_password import InMemoryCredentialStore, PasswordAuthBackend, hash_password
from procworks.auth_token import TokenAuthBackend

client = TestClient(api_module.app)
_ids = (f"org-rights-{n}" for n in itertools.count(1))
_WORKER = {"worker-token": {"subject": "worker", "roles": ["integration"], "scopes": ["*"]}}


@pytest.fixture
def login() -> Iterator[Any]:
    """``login(name, roles)`` -> headers; password mode from the first call.

    ``login("worker", None)`` returns the headers of a machine token.
    """

    backend = PasswordAuthBackend(
        InMemoryCredentialStore(), machine_tokens=TokenAuthBackend(_WORKER)
    )
    original = api_module._auth_backend

    def _login(name: str, roles: list[str] | None) -> dict[str, str]:
        api_module._auth_backend = backend
        if roles is None:
            return {"Authorization": "Bearer worker-token"}
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


def _shared_org() -> str:
    """A shared organisation with role ``sb``, unit ``u1`` and person ``a1``
    (built in open mode, before the test switches to logins)."""

    org_id = next(_ids)
    created = client.post("/org-models", json={"name": "Amt", "org_model_id": org_id})
    assert created.status_code == 201
    client.post(f"/org-models/{org_id}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(f"/org-models/{org_id}/org-units", json={"name": "Amt", "org_unit_id": "u1"})
    client.post(f"/org-models/{org_id}/agents",
                json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1", "org_unit_id": "u1"})
    return org_id


#: Every content change of a shared organisation: (method, path, body).
def _content_changes(org_id: str) -> list[tuple[str, str, dict[str, object]]]:
    base = f"/org-models/{org_id}"
    return [
        ("post", f"{base}/roles", {"name": "Leitung", "role_id": "lt"}),
        ("post", f"{base}/org-units", {"name": "Kasse", "org_unit_id": "u2"}),
        ("post", f"{base}/org-units/u2/parent", {"parent_id": "u1"}),
        ("post", f"{base}/agents", {"name": "Max", "role_ids": ["sb"], "agent_id": "a2"}),
        ("post", f"{base}/org-units/u1/manager", {"manager_id": "a2"}),
        ("patch", f"{base}/agents/a2", {"name": "Max Muster"}),
        ("post", f"{base}/agents/a1/deputy", {"deputy_id": "a2"}),
        ("put", f"{base}/roles/sb/mailbox", {"mailbox": "sb@example.org"}),
        ("put", f"{base}/units/u1/mailbox", {"mailbox": "amt@example.org"}),
    ]


def _send(method: str, path: str, body: dict[str, object], headers: dict[str, str]) -> Any:
    return getattr(client, method)(path, json=body, headers=headers)


def test_a_modeller_maintains_every_part_of_a_shared_organisation(login: Any) -> None:
    org_id = _shared_org()
    mara = login("mara", ["modeler"])

    for method, path, body in _content_changes(org_id):
        resp = _send(method, path, body, mara)
        assert resp.status_code == 200, (path, resp.text)

    org = client.get(f"/org-models/{org_id}", headers=mara).json()
    assert org["agents"]["a2"]["name"] == "Max Muster"
    assert org["agents"]["a1"]["deputy_id"] == "a2"
    assert org["org_units"]["u2"]["parent_id"] == "u1"


def test_creating_a_shared_organisation_stays_with_administrators(login: Any) -> None:
    mara = login("mara", ["modeler"])
    ada = login("ada", ["admin"])

    refused = client.post("/org-models", json={"name": "Neu", "org_model_id": "org-neu-m"},
                          headers=mara)
    assert refused.status_code == 403
    assert client.get("/org-models/org-neu-m", headers=mara).status_code == 404

    created = client.post("/org-models", json={"name": "Neu", "org_model_id": "org-neu-a"},
                          headers=ada)
    assert created.status_code == 201


@pytest.mark.parametrize(
    ("who", "roles"),
    [("otto", ["operator"]), ("vera", ["viewer"]), ("worker", None)],
    ids=["bearbeiter", "leser", "maschinen-token"],
)
def test_nobody_else_changes_a_shared_organisation(
    login: Any, who: str, roles: list[str] | None
) -> None:
    org_id = _shared_org()
    headers = login(who, roles)
    before = client.get(f"/org-models/{org_id}", headers=login("ada", ["admin"])).json()

    for method, path, body in _content_changes(org_id):
        assert _send(method, path, body, headers).status_code == 403, path

    after = client.get(f"/org-models/{org_id}", headers=login("ada2", ["admin"])).json()
    assert after == before


def test_without_a_login_it_is_401(login: Any) -> None:
    org_id = _shared_org()
    login("ada", ["admin"])  # switch to password mode

    for method, path, body in _content_changes(org_id):
        assert _send(method, path, body, {}).status_code == 401, path


def test_the_checks_against_linked_processes_apply_to_a_modeller(login: Any) -> None:
    """Removing the only holder of a role a linked process needs stays 422 Z2."""

    org_id = _shared_org()
    sid = client.post("/schemas", json={"name": "Antrag (geteilt)"}).json()["id"]
    schema = client.post(f"/schemas/{sid}/serial-insert",
                         json={"label": "Bearbeiten", "after_node_id": "start"}).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Bearbeiten")
    linked = client.post(f"/schemas/{sid}/org-model", json={"org_model_id": org_id})
    assert linked.status_code == 200
    ruled = client.post(f"/schemas/{sid}/staff-rule",
                        json={"node_id": act, "rule": {"kind": "ROLE", "ref": "sb"}})
    assert ruled.status_code == 200
    mara = login("mara", ["modeler"])

    resp = client.patch(f"/org-models/{org_id}/agents/a1", json={"role_ids": []}, headers=mara)

    assert resp.status_code == 422
    assert "Z2.nobody" in [f["code"] for f in resp.json()["detail"]["findings"]]
    agents = client.get(f"/org-models/{org_id}", headers=mara).json()["agents"]
    assert agents["a1"]["role_ids"] == ["sb"]


# --- the interface offers exactly what the role may do ---------------------------

_LINK_DIALOG = r"""
document.body.appendChild(byId("modal-root"));
state.principal = { subject: "x", roles: ROLES };
state.schema = { id: "s1", name: "Antrag", lifecycle_state: "ENTWURF", nodes: {} };
state.schemaId = "s1";
respond((p) => p === "/org-models" ? { body: [{ id: "org-1", name: "Amt" }] } : { status: 404 });
await manageSharedOrg();
const root = byId("modal-root");
return { text: textOf(root), inputs: root.querySelectorAll("input").length };
"""


@needs_node
@pytest.mark.parametrize(("roles", "offers_new"), [(["modeler"], False), (["admin"], True)])
def test_the_link_dialog_offers_a_new_organisation_only_to_administrators(
    tmp_path: Path, roles: list[str], offers_new: bool
) -> None:
    res = run_app_js(_LINK_DIALOG.replace("ROLES", repr(roles).replace("'", '"')), tmp_path)

    assert ("oder neue anlegen" in res["text"]) is offers_new
    assert res["inputs"] == (1 if offers_new else 0)
    assert "Vorhandene Organisation" in res["text"]
    if not offers_new:
        assert "legt die Administration an" in res["text"]
