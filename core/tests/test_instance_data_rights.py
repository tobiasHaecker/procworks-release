# SPDX-License-Identifier: BUSL-1.1
"""Setting instance data directly: only one's own work, never without a trace.

``PUT /instances/{id}/data`` used to accept any value on any instance from
every operator -- and wrote no audit event, "so it never pollutes the KPIs".
An operator could thereby raise the amount of an already approved purchase
request, or change a colleague's leave request, without anybody noticing
(Validierung aus Außensicht 2026-09-25, VAL-01).

The boundary rule (``api._authorize_data_write``):

* a bound caller sets exactly the elements its own open steps write;
* modeller/admin may correct anything else, but only as a supervision act
  with a mandatory reason;
* everyone else gets 403, a completed instance 409 for everybody;
* machine identities (open mode, static tokens, ``integration``) keep the
  documented integration path, test instances stay free.

Every change writes ``INSTANCE_DATA_SET`` with old and new value, and that event
type leaves the KPIs untouched. Each negative test pins the reason (status and
detail text), the counter-examples keep the rule from blocking legitimate work.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app
from procworks.audit import AuditEvent, EventType
from procworks.auth import SCOPE_DATA_WRITE, AuthBackend
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)
from procworks.auth_token import TokenAuthBackend

client = TestClient(app)

#: Start of the 403 detail for an operator outside its own work.
_NOT_YOURS = "Diese Daten gehören zu keinem Schritt"


def _instance(name: str, *, release: bool = True) -> tuple[str, str]:
    """Running instance of: start → "Erfassen" → "Genehmigen" → end.

    Built in open mode. "Erfassen" (role ``sb``, agent ``a1``) writes
    ``betrag``; "Genehmigen" (role ``other``, agent ``a2``) writes nothing -- the
    four-eyes shape of VAL-01. ``notiz`` is an element no step writes. Returns
    ``(instance_id, id of "Erfassen")``.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    schema = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Genehmigen", "after_node_id": act},
    ).json()
    approve = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Genehmigen")
    for elem, dtype in (("betrag", "FLOAT"), ("notiz", "STRING")):
        client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": elem, "data_type": dtype, "element_id": elem},
        )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": act, "element_id": "betrag", "mode": "WRITE", "mandatory": False},
    )
    client.post(f"/schemas/{sid}/roles", json={"name": "Sachbearbeitung", "role_id": "sb"})
    client.post(f"/schemas/{sid}/roles", json={"name": "Andere", "role_id": "other"})
    for name_, role, agent_id in (("Erika", "sb", "a1"), ("Paul", "other", "a2")):
        client.post(
            f"/schemas/{sid}/agents",
            json={"name": name_, "role_ids": [role], "agent_id": agent_id},
        )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": act, "rule": {"kind": "ROLE", "ref": "sb"}},
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": approve, "rule": {"kind": "ROLE", "ref": "other"}},
    )
    staff_via_api(client, sid)
    if release:
        assert client.post(f"/schemas/{sid}/release").status_code == 200
    resp = client.post(f"/schemas/{sid}/instances")
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], act


def _put(iid: str, values: dict[str, object], headers: dict[str, str], **extra: object) -> Any:
    """``PUT /instances/{iid}/data`` with ``values`` (and e.g. ``reason``)."""

    return client.put(f"/instances/{iid}/data", json={"values": values, **extra}, headers=headers)


def _data_events(iid: str) -> list[AuditEvent]:
    return [
        e for e in api_module._audit.for_instance(iid)
        if e.event_type is EventType.INSTANCE_DATA_SET
    ]


@pytest.fixture
def use_backend() -> Iterator[Any]:
    """Swap the module's auth backend for the test and restore it afterwards."""

    original = api_module._auth_backend

    def _set(backend: AuthBackend) -> None:
        api_module._auth_backend = backend

    try:
        yield _set
    finally:
        api_module._auth_backend = original


@pytest.fixture
def password(use_backend: Any) -> Any:
    """Returns ``login(name, roles, agent_id)`` → auth headers of a password user.

    The password backend is switched on at the **first** login, so a test builds
    its schema in open mode first (``_instance``) and logs in afterwards.
    """

    backend = PasswordAuthBackend(InMemoryCredentialStore())

    def _login(login: str, roles: list[str], agent_id: str | None = None) -> dict[str, str]:
        use_backend(backend)
        backend.create_user(subject=login, login=login, roles=roles, agent_id=agent_id)
        user = backend.store.get_user(login)
        assert user is not None
        backend.store.put_user(
            user.model_copy(
                update={"password_hash": hash_password("secret-pw1"), "must_change": False}
            )
        )
        return {"Authorization": f"Bearer {backend.login(login, 'secret-pw1').token}"}

    return _login


# --- operators: only the data of their own open step ----------------------------


def test_operator_sets_the_data_of_its_own_open_step(password: Any) -> None:
    iid, _ = _instance("Daten – eigener Schritt")
    erika = password("erika", ["operator"], "a1")

    resp = _put(iid, {"betrag": 499.99}, erika)

    assert resp.status_code == 200, resp.text
    [event] = _data_events(iid)
    assert event.agent_id == "a1"
    assert event.label == "betrag"
    assert event.detail["element"] == "betrag"
    assert json.loads(event.detail["old"]) is None
    assert json.loads(event.detail["new"]) == 499.99
    assert "actor" not in event.detail  # the bound agent *is* the actor
    assert "reason" not in event.detail


def test_operator_not_responsible_for_any_open_step_is_refused(password: Any) -> None:
    iid, _ = _instance("Daten – fremder Vorgang")
    paul = password("paul", ["operator"], "a2")

    resp = _put(iid, {"betrag": 5_000_000}, paul)

    assert resp.status_code == 403
    assert resp.json()["detail"].startswith(_NOT_YOURS)
    assert "betrag" not in api_module._get_instance_or_404(iid).data_values
    assert _data_events(iid) == []


def test_operator_may_not_set_elements_its_step_does_not_write(password: Any) -> None:
    iid, _ = _instance("Daten – fremdes Element")
    erika = password("erika", ["operator"], "a1")

    resp = _put(iid, {"notiz": "x"}, erika)

    assert resp.status_code == 403
    assert resp.json()["detail"].startswith(_NOT_YOURS)


def test_operator_cannot_change_data_after_its_step_is_done(password: Any) -> None:
    # The four-eyes scenario of VAL-01: "Erfassen" is done, its amount is the
    # basis of the approval -- neither the author nor the approver may change it.
    iid, act = _instance("Daten – nach Abschluss des Schritts")
    erika = password("erika", ["operator"], "a1")
    paul = password("paul", ["operator"], "a2")
    done = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act, "data": {"betrag": 499.99}},
        headers=erika,
    )
    assert done.status_code == 200, done.text

    by_author = _put(iid, {"betrag": 5e6}, erika)
    by_approver = _put(iid, {"betrag": 5e6}, paul)

    for resp in (by_author, by_approver):
        assert resp.status_code == 403
        assert resp.json()["detail"].startswith(_NOT_YOURS)
    assert api_module._get_instance_or_404(iid).data_values["betrag"] == 499.99


def test_completed_instance_is_closed_for_everybody(password: Any) -> None:
    iid, act = _instance("Daten – abgeschlossener Vorgang")
    mara = password("mara", ["modeler"])
    for node, who in ((act, password("erika", ["operator"], "a1")),
                      (None, password("paul", ["operator"], "a2"))):
        node_id = node or next(
            t["node_id"] for t in client.get(f"/instances/{iid}/tasks", headers=mara).json()
        )
        assert client.post(
            f"/instances/{iid}/complete", json={"node_id": node_id, "data": {}}, headers=who
        ).status_code == 200
    assert api_module._get_instance_or_404(iid).state == "COMPLETED"

    resp = client.put(
        f"/instances/{iid}/data", json={"values": {"notiz": "x"}, "reason": "nachträglich"},
        headers=mara,
    )

    assert resp.status_code == 409
    assert "abgeschlossen" in resp.json()["detail"]


def test_step_claimed_by_somebody_else_does_not_count(password: Any) -> None:
    iid, act = _instance("Daten – von anderer Person übernommen")
    erika = password("erika", ["operator"], "a1")
    claimed = api_module._get_instance_or_404(iid).model_copy(deep=True)
    claimed.claimed_by[act] = "someone-else"
    api_module._instances.put(claimed)

    resp = _put(iid, {"betrag": 1.0}, erika)

    assert resp.status_code == 403
    assert resp.json()["detail"].startswith(_NOT_YOURS)


def test_viewer_cannot_set_data(password: Any) -> None:
    iid, _ = _instance("Daten – Leser")
    vera = password("vera", ["viewer"])

    resp = _put(iid, {"betrag": 1.0}, vera)

    assert resp.status_code == 403


# --- modeller/admin: corrections only as supervision with a reason -------------


def test_modeler_correction_needs_a_reason(password: Any) -> None:
    iid, _ = _instance("Daten – Korrektur ohne Begründung")
    mara = password("mara", ["modeler"])

    resp = _put(iid, {"notiz": "x"}, mara)

    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("Aufsichtseingriff")
    assert _data_events(iid) == []


def test_modeler_correction_with_reason_is_audited_with_actor_and_reason(password: Any) -> None:
    iid, _ = _instance("Daten – Korrektur mit Begründung")
    mara = password("mara", ["modeler"])
    _put(iid, {"notiz": "alt"}, mara, reason="Erstanlage")

    resp = client.put(
        f"/instances/{iid}/data",
        json={"values": {"notiz": "neu"}, "reason": "  Tippfehler im Antrag  "},
        headers=mara,
    )

    assert resp.status_code == 200, resp.text
    event = _data_events(iid)[-1]
    assert event.agent_id is None
    assert event.detail["actor"] == "mara"
    assert event.detail["reason"] == "Tippfehler im Antrag"
    assert json.loads(event.detail["old"]) == "alt"
    assert json.loads(event.detail["new"]) == "neu"


def test_unchanged_value_writes_no_event(password: Any) -> None:
    iid, _ = _instance("Daten – unverändert")
    erika = password("erika", ["operator"], "a1")
    _put(iid, {"betrag": 10.0}, erika)

    resp = _put(iid, {"betrag": 10.0}, erika)

    assert resp.status_code == 200
    assert len(_data_events(iid)) == 1


def test_type_errors_are_reported_before_the_rights_check(password: Any) -> None:
    # A malformed request is a 422 (D3) whoever sends it -- the rights check
    # must not mask it, and it must not leak whether the caller would be allowed.
    iid, _ = _instance("Daten – Typfehler")
    paul = password("paul", ["operator"], "a2")

    resp = _put(iid, {"betrag": "viel"}, paul)

    assert resp.status_code == 422
    assert "D3" in {f["rule"] for f in resp.json()["detail"]["findings"]}


# --- paths that stay open ---------------------------------------------------------


def test_static_integration_token_keeps_the_v1_path_and_is_named_in_the_audit(
    use_backend: Any,
) -> None:
    iid, _ = _instance("Daten – Integration")
    use_backend(TokenAuthBackend(
        {"erp-token": {"subject": "erp", "roles": ["operator"], "scopes": [SCOPE_DATA_WRITE]}}
    ))

    resp = client.put(
        f"/v1/instances/{iid}/data",
        json={"values": {"notiz": "aus dem ERP"}},
        headers={"Authorization": "Bearer erp-token"},
    )

    assert resp.status_code == 200, resp.text
    [event] = _data_events(iid)
    assert event.detail["actor"] == "erp"


def test_test_instances_stay_free_and_unaudited(password: Any) -> None:
    iid, _ = _instance("Daten – Prüfinstanz", release=False)
    paul = password("paul", ["operator"], "a2")

    resp = _put(iid, {"notiz": "x"}, paul)

    assert resp.status_code == 200, resp.text
    assert _data_events(iid) == []


def test_data_events_leave_the_kpis_unchanged(password: Any) -> None:
    iid, _ = _instance("Daten – KPI neutral")
    schema_id = client.get(f"/instances/{iid}").json()["schema_id"]
    before = client.get("/monitoring/kpis", params={"schema_id": schema_id}).json()
    erika = password("erika", ["operator"], "a1")
    assert client.put(
        f"/instances/{iid}/data", json={"values": {"betrag": 3.0}}, headers=erika
    ).status_code == 200
    assert _data_events(iid)

    after = client.get(
        "/monitoring/kpis", params={"schema_id": schema_id}, headers=erika
    ).json()

    assert after == before
