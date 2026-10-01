# SPDX-License-Identifier: BUSL-1.1
"""Engine refusals and BPMN import errors carry a code.

The web client words every refusal from ``code``/``params`` in its catalogue
``FINDING_TEXTS``. Engine refusals (HTTP 409) and BPMN import errors (422) came
with an English ``message`` only -- a colleague who claimed a task a second
earlier was shown "activity 'act_1' is already claimed by 'a-tom' (W1)". The
catalogue coverage itself is guarded by
``test_catalog_covers_every_code_the_core_emits``; this module pins that the
codes actually reach the HTTP answer, with the step *name*.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from staffing import staff_via_api

from procworks import execution
from procworks.api import app

client = TestClient(app)


def _running(name: str) -> tuple[str, str]:
    """Running instance of a released one-step schema (role sb: a1, a2)."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    step = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    client.post(f"/schemas/{sid}/roles", json={"name": "sb", "role_id": "sb"})
    for agent in ("a1", "a2"):
        client.post(
            f"/schemas/{sid}/agents", json={"name": agent, "role_ids": ["sb"], "agent_id": agent}
        )
    client.post(
        f"/schemas/{sid}/staff-rule", json={"node_id": step, "rule": {"kind": "ROLE", "ref": "sb"}}
    )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return client.post(f"/schemas/{sid}/instances").json()["id"], step


def test_claim_conflict_names_the_step_and_the_holder() -> None:
    iid, step = _running("Meldung Übernahme")
    assert client.post(
        f"/instances/{iid}/claim", json={"node_id": step, "agent_id": "a1"}
    ).status_code == 200

    resp = client.post(f"/instances/{iid}/claim", json={"node_id": step, "agent_id": "a2"})

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "EX.claimed-by-other"
    assert detail["params"] == {"step": "Erfassen", "agent": "a1"}
    assert "already claimed" in detail["message"]  # technical base unchanged


def test_every_engine_refusal_has_a_specific_code() -> None:
    """No ``raise ExecutionError`` without ``code=`` -- the default is a last resort."""

    from pathlib import Path

    src = Path(execution.__file__).read_text(encoding="utf-8")
    raises = src.count("raise ExecutionError(")
    coded = src.count('code="EX.')
    assert raises and raises == coded, (raises, coded)


def test_unsupported_bpmn_element_is_named() -> None:
    xml = (
        '<?xml version="1.0"?><bpmn:definitions '
        'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="d" targetNamespace="x">'
        '<bpmn:process id="p"><bpmn:startEvent id="s"/>'
        '<bpmn:inclusiveGateway id="g"/></bpmn:process></bpmn:definitions>'
    )
    resp = client.post("/bpmn-import", json={"xml": xml})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "BPMN.unsupported"
    assert detail["params"] == {"element": "inclusiveGateway"}


def test_broken_xml_has_its_own_code() -> None:
    resp = client.post("/bpmn-import", json={"xml": "<kaputt"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "BPMN.invalid-xml"
