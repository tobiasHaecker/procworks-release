# SPDX-License-Identifier: BUSL-1.1
"""Aufsichtseingriff: completing a staff-rule step without an agent binding.

The core checks BZR eligibility only when an ``agent_id`` is present. A login
*without* an agent binding (modeler/admin with no person behind it) therefore
completes a step outside the staff rule. Before 1.17.0 that happened silently
and the audit showed "System" instead of the login.

The boundary now makes this an explicit, reasoned **supervision** action and
refuses it entirely while licensing is enforced -- otherwise an unbound login
would be unlimited unlicensed labour. These tests pin each branch *and* its
reason, not merely the status code.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from invisible_text import INVISIBLE_REASONS, assert_reason_required
from staffing import staff_via_api
from web_vm import needs_node, run_app_js

import procworks.api as api_module
from procworks.api import app
from procworks.audit import EventType
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
)
from procworks.model import StaffRule

client = TestClient(app)


def _released_task_schema(name: str) -> tuple[str, str]:
    """Released schema with one activity bound to role 'sb' (agent a1).

    Built in open mode (the fixture swaps the backend afterwards), so setup
    needs no tokens.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Bearbeiten", "after_node_id": "start"},
    ).json()
    act_id = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Bearbeiten")
    client.post(f"/schemas/{sid}/roles", json={"name": "Sachbearbeiter", "role_id": "sb"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"},
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": act_id, "rule": {"kind": "ROLE", "ref": "sb"}},
    )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, act_id


def _login(backend: PasswordAuthBackend, login: str, **kw: object) -> dict[str, str]:
    """Create a ready-to-use user (no forced change) and return auth headers."""

    backend.create_user(subject=login, login=login, **kw)  # type: ignore[arg-type]
    user = backend.store.get_user(login)
    assert user is not None
    backend.store.put_user(
        user.model_copy(
            update={"password_hash": hash_password("secret-pw1"), "must_change": False}
        )
    )
    token = backend.login(login, "secret-pw1").token
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def task() -> Iterator[tuple[str, str, PasswordAuthBackend]]:
    """A running instance on a staffed step, served under password auth."""

    sid, act_id = _released_task_schema("Aufsicht")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    api_module._auth_backend = backend
    try:
        yield iid, act_id, backend
    finally:
        api_module._auth_backend = original


def test_unbound_login_without_reason_is_refused(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])

    resp = client.post(f"/instances/{iid}/complete", json={"node_id": act_id}, headers=headers)

    assert resp.status_code == 422
    assert_reason_required(resp, "supervision", api_module.SUPERVISION_REASON_REQUIRED)
    # Refused before the engine ran: the step is still open.
    inst = client.get(f"/instances/{iid}", headers=headers).json()
    assert inst["node_states"][act_id] != "COMPLETED"


def test_unbound_login_with_blank_reason_is_refused(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "supervision_reason": "   "},
        headers=headers,
    )

    assert resp.status_code == 422
    assert_reason_required(resp, "supervision", api_module.SUPERVISION_REASON_REQUIRED)


def test_unbound_login_with_reason_completes_and_is_audited(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "supervision_reason": "Bearbeiterin krank"},
        headers=headers,
    )

    assert resp.status_code == 200
    events = client.get(f"/instances/{iid}/audit", headers=headers).json()
    done = [e for e in events if e["event_type"] == EventType.ACTIVITY_COMPLETED]
    sup = [e for e in events if e["event_type"] == EventType.ACTIVITY_SUPERVISED]
    # The regular completion names the login instead of leaving it blank ...
    assert done[-1]["agent_id"] is None
    assert done[-1]["detail"]["actor"] == "mara"
    # ... and the exception itself is on record with its reason.
    assert len(sup) == 1
    assert sup[0]["node_id"] == act_id
    assert sup[0]["detail"] == {"actor": "mara", "reason": "Bearbeiterin krank"}


def test_bound_login_needs_no_reason(task: tuple[str, str, PasswordAuthBackend]) -> None:
    iid, act_id, backend = task
    headers = _login(backend, "erika", roles=["operator"], agent_id="a1")

    resp = client.post(f"/instances/{iid}/complete", json={"node_id": act_id}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["performed_by"][act_id] == "a1"
    events = client.get(f"/instances/{iid}/audit", headers=headers).json()
    assert not any(e["event_type"] == EventType.ACTIVITY_SUPERVISED for e in events)
    done = [e for e in events if e["event_type"] == EventType.ACTIVITY_COMPLETED]
    assert "actor" not in done[-1]["detail"]


def test_bound_but_ineligible_login_is_still_refused_by_the_core(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    """A reason never unlocks a *bound* login: the core BZR check stays in charge."""

    iid, act_id, backend = task
    # A modeller role keeps the case visible (a pure operator who is not
    # involved gets 404 before the core is asked) -- so this still
    # reaches the core's eligibility check.
    headers = _login(backend, "fremd", roles=["operator", "modeler"], agent_id="ghost")

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "supervision_reason": "egal"},
        headers=headers,
    )

    assert resp.status_code == 409
    assert "not eligible" in str(resp.json()["detail"])


def test_supervision_is_refused_while_licensing_is_enforced(
    task: tuple[str, str, PasswordAuthBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unbound completion would be unlicensed labour -- no reason can buy it."""

    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])
    monkeypatch.setattr(api_module._license, "_pubkey", "configured")

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "supervision_reason": "Bearbeiterin krank"},
        headers=headers,
    )

    assert resp.status_code == 403
    assert "Lizenzierung" in resp.json()["detail"]
    monkeypatch.undo()
    inst = client.get(f"/instances/{iid}", headers=headers).json()
    assert inst["node_states"][act_id] != "COMPLETED"


def test_open_dev_mode_keeps_working_without_reason() -> None:
    """No identity exists in open mode -- the quickstart must not change."""

    sid, act_id = _released_task_schema("AufsichtOffen")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    resp = client.post(f"/instances/{iid}/complete", json={"node_id": act_id})

    assert resp.status_code == 200


def test_v1_passes_the_supervision_reason_through(
    task: tuple[str, str, PasswordAuthBackend],
) -> None:
    """The integration mirror must offer the same way out, not a dead end."""

    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])
    url = f"/v1/instances/{iid}/nodes/{act_id}/complete"

    refused = client.post(url, json={}, headers=headers)
    assert refused.status_code == 422
    assert_reason_required(refused, "supervision", api_module.SUPERVISION_REASON_REQUIRED)

    ok = client.post(url, json={"supervision_reason": "Nachtlauf"}, headers=headers)
    assert ok.status_code == 200


@pytest.mark.parametrize(
    "invisible", [text for _, text in INVISIBLE_REASONS], ids=[i for i, _ in INVISIBLE_REASONS]
)
def test_unbound_login_with_an_invisible_reason_is_refused(
    task: tuple[str, str, PasswordAuthBackend], invisible: str
) -> None:
    """A reason of zero-width characters says nothing in the history either."""

    iid, act_id, backend = task
    headers = _login(backend, "mara", roles=["modeler"])

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "supervision_reason": invisible},
        headers=headers,
    )

    assert resp.status_code == 422
    assert_reason_required(resp, "supervision", api_module.SUPERVISION_REASON_REQUIRED)
    inst = client.get(f"/instances/{iid}", headers=headers).json()
    assert inst["node_states"][act_id] != "COMPLETED"


@needs_node
def test_the_web_client_recognises_the_supervision_refusal_by_its_code(tmp_path: Path) -> None:
    """The client asks for a supervision reason on the shared code alone.

    Three reason duties are supervision acts and open the reason dialog
    (``supervision``, ``automatic``, ``data-correction``); the failure report
    and the ad-hoc occasion have their own dialogs, and a bare text -- the
    shape the core answered with before -- is no longer taken for one.
    """

    result = run_app_js(
        """
        const refusal = (what) => ({ status: 422, detail: {
          message: "Aufsichtseingriff: …", code: "REQ.reason-required", params: { what } } });
        return {
          supervision: isSupervisionRequired(refusal("supervision")),
          automatic: isSupervisionRequired(refusal("automatic")),
          data: isSupervisionRequired(refusal("data-correction")),
          fail: isSupervisionRequired(refusal("fail")),
          adhoc: isSupervisionRequired(refusal("adhoc")),
          suspend: isSupervisionRequired(refusal("suspend")),
          text: isSupervisionRequired({ status: 422, detail: "Aufsichtseingriff: Bitte …" }),
          other: isSupervisionRequired({ status: 409, detail: { code: "REQ.reason-required",
            params: { what: "supervision" } } }),
          none: isSupervisionRequired(null),
        };
        """,
        tmp_path,
    )
    assert result == {
        "supervision": True, "automatic": True, "data": True,
        "fail": False, "adhoc": False, "suspend": False,
        "text": False, "other": False, "none": False,
    }


def test_every_reason_duty_answers_with_the_shared_code() -> None:
    """All six reason duties build their 422 through one helper."""

    src = Path(api_module.__file__).read_text(encoding="utf-8")
    duties = set(re.findall(r'_reason_required\("([a-z-]+)"', src))
    assert duties == {"fail", "suspend", "supervision", "automatic", "data-correction", "adhoc"}


# --- a later step that refers to this step's performer ---------------------------


def _schema_with_relative_follow_up(name: str, follow_rule: dict[str, object]) -> tuple[str, str]:
    """Two staffed steps; the second's rule is ``follow_rule``, in which the
    placeholder ``FIRST`` stands for the first step's id.

    Returns ``(schema_id, first_step)``; built in open mode, released.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Bearbeiten", "after_node_id": "start"}
    ).json()
    first = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Bearbeiten")
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Genehmigen", "after_node_id": first}
    ).json()
    second = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Genehmigen")
    client.post(f"/schemas/{sid}/roles", json={"name": "Sachbearbeiter", "role_id": "sb"})
    client.post(
        f"/schemas/{sid}/agents", json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"}
    )
    client.post(f"/schemas/{sid}/staff-rule",
                json={"node_id": first, "rule": {"kind": "ROLE", "ref": "sb"}})
    rule = json.loads(json.dumps(follow_rule).replace("FIRST", first))
    resp = client.post(f"/schemas/{sid}/staff-rule", json={"node_id": second, "rule": rule})
    assert resp.status_code == 200, resp.text
    staff_via_api(client, sid)
    released = client.post(f"/schemas/{sid}/release")
    assert released.status_code == 200, released.text
    return sid, first


@pytest.mark.parametrize(
    "follow_rule",
    [
        {"kind": "NODE_PERFORMING_AGENT", "ref": "FIRST"},
        {"kind": "OR", "operands": [
            {"kind": "ROLE", "ref": "sb"},
            {"kind": "NODE_PERFORMING_AGENT", "ref": "FIRST"},
        ]},
    ],
    ids=["direkt", "verschachtelt"],
)
def test_unbound_supervision_is_refused_when_a_later_step_needs_its_performer(
    follow_rule: dict[str, object],
) -> None:
    """After an unbound supervision the step has no performer, so a rule
    "the person who did step 1" would find nobody. The supervision is refused
    before it changes anything, with the code and the steps that depend on it."""

    sid, first = _schema_with_relative_follow_up("Aufsicht – relativ", follow_rule)
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    api_module._auth_backend = backend
    try:
        headers = _login(backend, "mara", roles=["modeler"])
        resp = client.post(
            f"/instances/{iid}/complete",
            json={"node_id": first, "supervision_reason": "Bearbeiterin krank"},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "SUP.performer-needed"
        assert detail["params"] == {"step": "Bearbeiten", "dependants": "Genehmigen"}
        inst = client.get(f"/instances/{iid}", headers=headers).json()
        assert inst["node_states"][first] != "COMPLETED"
        events = [e.event_type for e in api_module._audit.for_instance(iid)]
        assert EventType.ACTIVITY_SUPERVISED not in events
    finally:
        api_module._auth_backend = original


# --- the four-eyes rule stays hard and says so -----------------------------------


_FOUR_EYES = {"kind": "EXCEPT", "operands": [
    {"kind": "ROLE", "ref": "sb"},
    {"kind": "NODE_PERFORMING_AGENT", "ref": "FIRST"},
]}


def test_supervision_over_a_four_eyes_step_is_refused_naming_the_rule() -> None:
    """"Genehmigen: Sachbearbeiter except whoever did Bearbeiten" -- after a
    supervision nobody did Bearbeiten, so the rule could not be checked. The
    refusal stays (compliance), but it names the four-eyes rule rather than
    claiming that Genehmigen would find nobody (it would: every Sachbearbeiter)."""

    sid, first = _schema_with_relative_follow_up("Aufsicht – Vier Augen", _FOUR_EYES)
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    api_module._auth_backend = backend
    try:
        headers = _login(backend, "mara4", roles=["modeler"])
        resp = client.post(
            f"/instances/{iid}/complete",
            json={"node_id": first, "supervision_reason": "Bearbeiterin krank"},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "SUP.four-eyes"
        assert detail["params"] == {"step": "Bearbeiten", "dependants": "Genehmigen"}
        inst = client.get(f"/instances/{iid}", headers=headers).json()
        assert inst["node_states"][first] != "COMPLETED"
        events = [e.event_type for e in api_module._audit.for_instance(iid)]
        assert EventType.ACTIVITY_SUPERVISED not in events
    finally:
        api_module._auth_backend = original


def _rule(raw: dict[str, object]) -> StaffRule:
    return StaffRule.model_validate(json.loads(json.dumps(raw).replace("FIRST", "n1")))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"kind": "NODE_PERFORMING_AGENT", "ref": "FIRST"}, (True, False)),
        (_FOUR_EYES, (False, True)),
        # Excluded from an exclusion is a positive use again.
        ({"kind": "EXCEPT", "operands": [
            {"kind": "ROLE", "ref": "sb"},
            {"kind": "EXCEPT", "operands": [
                {"kind": "ROLE", "ref": "lt"},
                {"kind": "NODE_PERFORMING_AGENT_SUPERVISOR", "ref": "FIRST"},
            ]},
        ]}, (True, False)),
        ({"kind": "OR", "operands": [
            {"kind": "NODE_PERFORMING_AGENT", "ref": "FIRST"}, _FOUR_EYES,
        ]}, (True, True)),
        ({"kind": "ROLE", "ref": "sb"}, (False, False)),
    ],
    ids=["direkt", "vier-augen", "doppelt-ausgenommen", "beides", "ohne-bezug"],
)
def test_performer_uses_tell_a_needed_performer_from_an_exclusion(
    raw: dict[str, object], expected: tuple[bool, bool]
) -> None:
    assert api_module._performer_uses(_rule(raw), "n1") == expected


# --- an automatic step finished by hand: the same performer check ----------------


def _automatic_then_relative(name: str, follow_rule: dict[str, object]) -> tuple[str, str]:
    """``Automatisch`` (external task) followed by ``Nachbearbeiten`` whose
    rule is ``follow_rule`` (``FIRST`` = the automatic step). Released."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Automatisch", "after_node_id": "start"}
    ).json()
    auto = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Automatisch")
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Nachbearbeiten", "after_node_id": auto}
    ).json()
    after = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Nachbearbeiten")
    client.post(f"/schemas/{sid}/service",
                json={"node_id": auto, "name": "Automatisch", "automatic": True})
    automated = client.post(f"/schemas/{sid}/automation", json={
        "node_id": auto, "automation": "EXTERNAL_TASK", "topic": f"hand-{sid}"})
    assert automated.status_code == 200, automated.text
    client.post(f"/schemas/{sid}/roles", json={"name": "Sachbearbeiter", "role_id": "sb"})
    client.post(
        f"/schemas/{sid}/agents", json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"}
    )
    rule = json.loads(json.dumps(follow_rule).replace("FIRST", auto))
    resp = client.post(f"/schemas/{sid}/staff-rule", json={"node_id": after, "rule": rule})
    assert resp.status_code == 200, resp.text
    released = client.post(f"/schemas/{sid}/release")
    assert released.status_code == 200, released.text
    return sid, auto


@pytest.mark.parametrize(
    ("follow_rule", "code"),
    [
        ({"kind": "NODE_PERFORMING_AGENT", "ref": "FIRST"}, "SUP.performer-needed"),
        (_FOUR_EYES, "SUP.four-eyes"),
    ],
    ids=["bezug", "vier-augen"],
)
def test_finishing_an_automatic_step_by_hand_checks_the_performer_reference(
    follow_rule: dict[str, object], code: str
) -> None:
    """A modeller without a person behind the login finishes the automatic
    step by hand: afterwards nobody performed it, so a later step that takes
    its staff from that performer would find nobody (or could not show the
    separation of duties). Refused before anything changes, as for an
    interactive step."""

    sid, auto = _automatic_then_relative(f"Hand {code}", follow_rule)
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    api_module._auth_backend = backend
    try:
        headers = _login(backend, f"mara-{code}", roles=["modeler"])
        resp = client.post(
            f"/instances/{iid}/complete",
            json={"node_id": auto, "supervision_reason": "System ausgefallen"},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == code
        assert detail["params"] == {"step": "Automatisch", "dependants": "Nachbearbeiten"}
        inst = client.get(f"/instances/{iid}", headers=headers).json()
        assert inst["node_states"][auto] != "COMPLETED"
        events = [e.event_type for e in api_module._audit.for_instance(iid)]
        assert EventType.ACTIVITY_SUPERVISED not in events
    finally:
        api_module._auth_backend = original
        # The case keeps waiting for its system: create its task here, so a
        # later test's fetch does not announce it in that test's events.
        api_module._external_runtime().sync()


def test_finishing_an_automatic_step_without_a_reference_still_works_by_hand() -> None:
    sid, auto = _automatic_then_relative("Hand ohne Bezug", {"kind": "ROLE", "ref": "sb"})
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    api_module._auth_backend = backend
    try:
        headers = _login(backend, "mara-ohne", roles=["modeler"])
        resp = client.post(
            f"/instances/{iid}/complete",
            json={"node_id": auto, "supervision_reason": "System ausgefallen"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["node_states"][auto] == "COMPLETED"
    finally:
        api_module._auth_backend = original
