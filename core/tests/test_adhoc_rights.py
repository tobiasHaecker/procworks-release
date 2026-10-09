# SPDX-License-Identifier: BUSL-1.1
"""Who may change or move a running case.

An ad-hoc change or a migration decides how a running case continues, so only
those entitled to it may do it, and the history names who did it and why:
* ad-hoc insert/rename/delete: modeller/admin only; a real instance needs a
  reason; actor and reason are in the audit event (test instances: optional);
* migration: an operator still may migrate -- the instance view offers it --
  but only a case it is involved in (the involvement rule); report and bulk
  assistant show and move nothing else.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import ADHOC_REASON_REQUIRED, app
from procworks.audit import EventType
from procworks.auth_password import InMemoryCredentialStore, PasswordAuthBackend, hash_password

client = TestClient(app)


def _released(name: str, *, release: bool = True) -> tuple[str, str, str]:
    """Released "Erfassen" (role sb: a1) -> "Genehmigen" (role sb: a1), open mode.

    Returns ``(schema_id, erfassen_id, genehmigen_id)``; ``a2`` (role other)
    is never responsible. ``release=False`` leaves a staffed draft -- starting
    it gives a test instance.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    erfassen = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Genehmigen", "after_node_id": erfassen}
    ).json()
    genehmigen = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Genehmigen")
    for role in ("sb", "other"):
        client.post(f"/schemas/{sid}/roles", json={"name": role, "role_id": role})
    for agent_id, role in (("a1", "sb"), ("a2", "other")):
        client.post(
            f"/schemas/{sid}/agents",
            json={"name": agent_id, "role_ids": [role], "agent_id": agent_id},
        )
    for node in (erfassen, genehmigen):
        rule = {"kind": "ROLE", "ref": "sb"}
        client.post(f"/schemas/{sid}/staff-rule", json={"node_id": node, "rule": rule})
    staff_via_api(client, sid)
    if release:
        assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, erfassen, genehmigen


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


def _adhoc_calls(erfassen: str, genehmigen: str) -> list[tuple[str, dict[str, Any], EventType]]:
    return [
        ("insert", {"after_node_id": erfassen, "label": "Zusatz",
                    "staff_rule": {"kind": "ROLE", "ref": "sb"}}, EventType.ADHOC_INSERTED),
        ("rename", {"node_id": genehmigen, "label": "Genehmigt (automatisch)"},
         EventType.ADHOC_RENAMED),
        ("delete", {"node_id": genehmigen}, EventType.ADHOC_DELETED),
    ]


def test_operators_may_not_change_a_running_case(login: Any) -> None:
    """Even the responsible operator: no ad-hoc change of the own case."""

    sid, erfassen, genehmigen = _released("Ad-hoc Bearbeiter")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    before = client.get(f"/instances/{iid}").json()
    admin = login("root", ["admin"])
    for headers in (login("erika", ["operator"], "a1"), login("paul", ["operator"], "a2")):
        for action, body, _ in _adhoc_calls(erfassen, genehmigen):
            resp = client.post(
                f"/instances/{iid}/adhoc/{action}",
                json={**body, "reason": "eigenmächtig"},
                headers=headers,
            )
            assert resp.status_code == 403, (action, resp.json())
    after = client.get(f"/instances/{iid}", headers=admin).json()
    assert after["ad_hoc_schema"] is None and after == before


def test_modeller_needs_a_reason_and_is_named_in_the_history(login: Any) -> None:
    sid, erfassen, genehmigen = _released("Ad-hoc Modellierer")
    mara = login("mara", ["modeler"])
    for action, body, event in _adhoc_calls(erfassen, genehmigen):
        iid = client.post(f"/schemas/{sid}/instances", headers=mara).json()["id"]
        missing = client.post(f"/instances/{iid}/adhoc/{action}", json=body, headers=mara)
        blank = client.post(
            f"/instances/{iid}/adhoc/{action}", json={**body, "reason": "  "}, headers=mara
        )
        assert missing.status_code == blank.status_code == 422, action
        assert missing.json()["detail"] == ADHOC_REASON_REQUIRED
        assert client.get(f"/instances/{iid}", headers=mara).json()["ad_hoc_schema"] is None

        ok = client.post(
            f"/instances/{iid}/adhoc/{action}",
            json={**body, "reason": "Kunde hat angerufen"},
            headers=mara,
        )
        assert ok.status_code == 200, (action, ok.json())
        events = [e for e in api_module._audit.for_instance(iid) if e.event_type is event]
        assert len(events) == 1, action
        assert events[0].detail["actor"] == "mara"
        assert events[0].detail["reason"] == "Kunde hat angerufen"


def test_test_instances_need_no_reason() -> None:
    """A test instance writes no history, so a reason would go nowhere (open mode)."""

    sid, erfassen, _ = _released("Ad-hoc Prüfinstanz", release=False)
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    assert client.get(f"/instances/{iid}").json()["is_test"]
    resp = client.post(
        f"/instances/{iid}/adhoc/insert",
        json={"after_node_id": erfassen, "label": "Zusatz",
              "staff_rule": {"kind": "ROLE", "ref": "sb"}},
    )
    assert resp.status_code == 200, resp.json()


def _revision(sid: str) -> str:
    target = client.post(f"/schemas/{sid}/revision", json={}).json()["id"]
    staff_via_api(client, target)
    assert client.post(f"/schemas/{target}/release").status_code == 200
    return target


def test_uninvolved_operator_cannot_migrate_or_even_see_a_foreign_case(login: Any) -> None:
    sid, _, _ = _released("Ad-hoc Migration fremd")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    target = _revision(sid)
    paul = login("paul", ["operator"], "a2")

    single = client.post(
        f"/instances/{iid}/migrate", json={"target_schema_id": target}, headers=paul
    )
    check = client.post(
        f"/instances/{iid}/migration-check", json={"target_schema_id": target}, headers=paul
    )
    assert single.status_code == check.status_code == 404
    report = client.get(f"/schemas/{target}/migration-report", headers=paul).json()
    assert iid not in [c["instance_id"] for c in report["candidates"]]
    bulk = client.post(
        f"/schemas/{target}/migrate-instances",
        json={"instance_ids": [iid], "execute": True},
        headers=paul,
    ).json()
    assert [r["migrated"] for r in bulk["results"]] == [False]
    assert bulk["results"][0]["findings"][0]["code"] == "M0.not-candidate"
    admin = login("root", ["admin"])
    assert client.get(f"/instances/{iid}", headers=admin).json()["schema_id"] == sid


def test_involved_operator_and_modeller_may_migrate(login: Any) -> None:
    sid, _, _ = _released("Ad-hoc Migration eigen")
    own = client.post(f"/schemas/{sid}/instances").json()["id"]
    other = client.post(f"/schemas/{sid}/instances").json()["id"]
    target = _revision(sid)
    erika = login("erika", ["operator"], "a1")  # responsible for the open step
    mara = login("mara", ["modeler"])

    report = client.get(f"/schemas/{target}/migration-report", headers=mara).json()
    assert {own, other} <= {c["instance_id"] for c in report["candidates"]}
    moved = client.post(
        f"/instances/{own}/migrate", json={"target_schema_id": target}, headers=erika
    )
    assert moved.status_code == 200, moved.json()
    assert moved.json()["schema_id"] == target
    bulk = client.post(
        f"/schemas/{target}/migrate-instances",
        json={"instance_ids": [other], "execute": True},
        headers=mara,
    ).json()
    assert [r["migrated"] for r in bulk["results"]] == [True]
