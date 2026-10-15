# SPDX-License-Identifier: BUSL-1.1
"""Mandatory values and mask checks hold on every path, not only in the form.

The web form refused an empty mandatory field and a value outside the mask
checks, but the core only checked the values *sent with* a completion:

* a step with a mandatory write could be completed without any value (D1
  promises "no read without a write" -- the next step then read nothing);
* a value stored beforehand via ``PUT …/data`` was never checked against the
  mask, and a completion without data then took it over unchecked.

The rule now (one shared check, ``validator.completion_value_findings`` and
``validator.element_value_findings``):

* a completion -- web, ``/v1``, external task, supervision -- needs a value for
  every mandatory write of the step, sent or stored (``D1.write-missing``;
  from the second round of a loop a stored value of an earlier round no
  longer counts, see ``test_mandatory_write_per_activation.py``);
* the step's mask checks (min/max, length, pattern) apply to the value the step
  ends up with, sent or stored (``U4.*``);
* ``PUT …/data`` checks every value against every mask that enters its element
  (``U4.*``), for integrations and supervision alike. Supervision only
  overrides who may act, never what a value must look like.
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

client = TestClient(app)

_topics = (f"value-rules-topic-{n}" for n in itertools.count(1))


def _codes(resp: Any) -> list[str]:
    return [f["code"] for f in resp.json()["detail"]["findings"]]


def _instance(name: str) -> tuple[str, str]:
    """Running instance: start → "Erfassen" → "Prüfen" → end.

    "Erfassen" (role ``sb``, agent ``a1``) carries a mask: ``tage`` INTEGER
    (mandatory, 1..30), ``kuerzel`` STRING (mandatory, ``[A-Z]+``, at most 5
    characters) and ``notiz`` STRING (optional). "Prüfen" reads ``tage``.
    Returns ``(instance_id, id of "Erfassen")``.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Prüfen", "after_node_id": act}
    ).json()
    check = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Prüfen")
    for elem, dtype in (("tage", "INTEGER"), ("kuerzel", "STRING"), ("notiz", "STRING")):
        client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": elem, "data_type": dtype, "element_id": elem},
        )
    form = client.post(
        f"/schemas/{sid}/nodes/{act}/form",
        json={
            "fields": [
                {"element_id": "tage", "widget": "NUMBER", "min_value": 1, "max_value": 30},
                {"element_id": "kuerzel", "widget": "TEXT", "pattern": "[A-Z]+", "max_length": 5},
                {"element_id": "notiz", "widget": "TEXT", "required": False},
            ]
        },
    )
    assert form.status_code == 200, form.text
    read = client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": check, "element_id": "tage", "mode": "READ"},
    )
    assert read.status_code == 200, read.text
    client.post(f"/schemas/{sid}/roles", json={"name": "Sachbearbeitung", "role_id": "sb"})
    client.post(
        f"/schemas/{sid}/agents", json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"}
    )
    client.post(
        f"/schemas/{sid}/staff-rule", json={"node_id": act, "rule": {"kind": "ROLE", "ref": "sb"}}
    )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    resp = client.post(f"/schemas/{sid}/instances")
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], act


def _complete(iid: str, node: str, data: dict[str, object], **extra: Any) -> Any:
    return client.post(
        f"/instances/{iid}/complete", json={"node_id": node, "data": data}, **extra
    )


def _state(iid: str, node: str) -> str:
    return str(client.get(f"/instances/{iid}").json()["node_states"][node])


# --- mandatory writes ------------------------------------------------------------


def test_completion_without_a_mandatory_value_is_refused_naming_the_element() -> None:
    iid, act = _instance("Pflicht – fehlt")

    resp = _complete(iid, act, {"tage": 3})

    assert resp.status_code == 422
    [finding] = resp.json()["detail"]["findings"]
    assert finding["code"] == "D1.write-missing"
    assert finding["params"] == {"element": "kuerzel", "step": "Erfassen"}
    assert _state(iid, act) in ("ACTIVATED", "RUNNING")
    assert client.get(f"/instances/{iid}").json()["data_values"] == {}


def test_every_missing_mandatory_value_is_named_and_none_counts_as_missing() -> None:
    iid, act = _instance("Pflicht – leer")

    resp = _complete(iid, act, {"tage": None, "kuerzel": "  "})

    # ``None`` is no INTEGER (D3 answers first); without it both are missing.
    assert _codes(resp) == ["D3.wrong-type"]
    resp = _complete(iid, act, {"kuerzel": "  "})
    assert resp.status_code == 422
    assert [(f["code"], f["params"]["element"]) for f in resp.json()["detail"]["findings"]] == [
        ("D1.write-missing", "tage"),
        ("D1.write-missing", "kuerzel"),
    ]


def test_v1_completion_without_a_mandatory_value_is_refused() -> None:
    iid, act = _instance("Pflicht – v1")

    resp = client.post(f"/v1/instances/{iid}/nodes/{act}/complete", json={"data": {}})

    assert resp.status_code == 422
    assert _codes(resp) == ["D1.write-missing", "D1.write-missing"]
    assert _state(iid, act) in ("ACTIVATED", "RUNNING")


def test_a_stored_mandatory_value_counts() -> None:
    iid, act = _instance("Pflicht – gespeichert")
    stored = client.put(f"/instances/{iid}/data", json={"values": {"tage": 30, "kuerzel": "AB"}})
    assert stored.status_code == 200, stored.text

    resp = _complete(iid, act, {})

    assert resp.status_code == 200, resp.text
    assert resp.json()["data_values"] == {"tage": 30, "kuerzel": "AB"}


def test_optional_write_without_value_and_exact_bounds_pass() -> None:
    iid, act = _instance("Pflicht – Grenzen genau")

    resp = _complete(iid, act, {"tage": 1, "kuerzel": "ABCDE"})

    assert resp.status_code == 200, resp.text
    assert "notiz" not in resp.json()["data_values"]


def test_upper_bound_exactly_passes() -> None:
    iid, act = _instance("Pflicht – Obergrenze genau")
    assert _complete(iid, act, {"tage": 30, "kuerzel": "A"}).status_code == 200


# --- mask checks on stored values ------------------------------------------------


def test_data_write_outside_the_mask_is_refused_for_that_field() -> None:
    iid, _act = _instance("Maske – Datenkorrektur")

    resp = client.put(
        f"/instances/{iid}/data", json={"values": {"tage": 999, "kuerzel": "kleinbuchstaben"}}
    )

    assert resp.status_code == 422
    assert sorted(_codes(resp)) == ["U4.above-max", "U4.pattern", "U4.too-long"]
    assert client.get(f"/instances/{iid}").json()["data_values"] == {}


def test_a_stored_value_outside_the_mask_blocks_the_completion_until_corrected() -> None:
    """A value stored before the check (or by an old version) is not taken
    over silently: the completion names the field, a corrected value passes."""

    iid, act = _instance("Maske – Altwert")
    stored = api_module._instances.get(iid)
    assert stored is not None
    legacy = stored.model_copy(deep=True)
    legacy.data_values.update({"tage": 999, "kuerzel": "AB"})
    api_module._instances.put(legacy)

    refused = _complete(iid, act, {})
    assert refused.status_code == 422
    [finding] = refused.json()["detail"]["findings"]
    assert (finding["code"], finding["params"]["field"]) == ("U4.above-max", "tage")
    assert _state(iid, act) in ("ACTIVATED", "RUNNING")

    corrected = _complete(iid, act, {"tage": 12})
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["data_values"]["tage"] == 12


# --- supervision overrides only the staff rule --------------------------------------


@pytest.fixture
def password() -> Iterator[Any]:
    """``login(name, roles, agent_id)`` → auth headers of a password user."""

    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())

    def _login(login: str, roles: list[str], agent_id: str | None = None) -> dict[str, str]:
        api_module._auth_backend = backend
        backend.create_user(subject=login, login=login, roles=roles, agent_id=agent_id)
        user = backend.store.get_user(login)
        assert user is not None
        backend.store.put_user(
            user.model_copy(
                update={"password_hash": hash_password("secret-pw1"), "must_change": False}
            )
        )
        return {"Authorization": f"Bearer {backend.login(login, 'secret-pw1').token}"}

    try:
        yield _login
    finally:
        api_module._auth_backend = original


def test_supervision_completion_still_needs_the_mandatory_values(password: Any) -> None:
    iid, act = _instance("Aufsicht – Pflicht")
    mara = password("mara", ["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act, "data": {"tage": 3}, "supervision_reason": "Vertretung"},
        headers=mara,
    )

    assert resp.status_code == 422
    assert _codes(resp) == ["D1.write-missing"]
    done = client.post(
        f"/instances/{iid}/complete",
        json={
            "node_id": act,
            "data": {"tage": 3, "kuerzel": "AB"},
            "supervision_reason": "Vertretung",
        },
        headers=mara,
    )
    assert done.status_code == 200, done.text


def test_supervision_data_correction_obeys_the_mask(password: Any) -> None:
    iid, act = _instance("Aufsicht – Maske")
    erika = password("erika", ["operator"], "a1")
    mara = password("mara", ["modeler"])
    assert _complete(iid, act, {"tage": 3, "kuerzel": "AB"}, headers=erika).status_code == 200

    refused = client.put(
        f"/instances/{iid}/data",
        json={"values": {"tage": 999}, "reason": "Korrektur laut Antrag"},
        headers=mara,
    )
    assert refused.status_code == 422
    assert _codes(refused) == ["U4.above-max"]
    accepted = client.put(
        f"/instances/{iid}/data",
        json={"values": {"tage": 30}, "reason": "Korrektur laut Antrag"},
        headers=mara,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["tage"] == 30


# --- external task ------------------------------------------------------------------


def _external(topic: str) -> str:
    """Released automatic step that must write ``approved`` (mandatory)."""

    sid = client.post("/schemas", json={"name": f"ext-{topic}"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Run", "after_node_id": "start"})
    schema = client.get(f"/schemas/{sid}").json()
    node = next(nid for nid, n in schema["nodes"].items() if n["type"] == "ACTIVITY")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Freigabe", "data_type": "BOOLEAN", "element_id": "approved"},
    )
    client.post(f"/schemas/{sid}/service", json={"node_id": node, "name": "Run", "automatic": True})
    client.post(
        f"/schemas/{sid}/automation",
        json={"node_id": node, "automation": "EXTERNAL_TASK", "topic": topic},
    )
    bound = client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": node, "element_id": "approved", "mode": "WRITE", "mandatory": True},
    )
    assert bound.status_code == 200, bound.text
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return str(client.post(f"/v1/schemas/{sid}/instances").json()["id"])


def test_external_task_without_its_mandatory_output_is_refused() -> None:
    topic = next(_topics)
    iid = _external(topic)
    task = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [topic], "lock_ms": 300_000},
    ).json()[0]

    refused = client.post(
        f"/v1/external-tasks/{task['id']}/complete", json={"worker_id": "w1", "variables": {}}
    )

    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "D1.write-missing"
    assert refused.json()["detail"]["params"] == {"element": "Freigabe", "step": "Run"}
    assert client.get(f"/v1/instances/{iid}").json()["state"] == "RUNNING"
    done = client.post(
        f"/v1/external-tasks/{task['id']}/complete",
        json={"worker_id": "w1", "variables": {"approved": False}},
    )
    assert done.status_code == 200, done.text
    assert client.get(f"/v1/instances/{iid}").json()["data_values"] == {"approved": False}
