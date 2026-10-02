# SPDX-License-Identifier: BUSL-1.1
"""Stalled instances show up in the monitoring.

Instances whose open step nobody may work -- an empty rule, an ad-hoc step
without rule, a four-eyes step after a supervision act -- appeared with
"overdue 0, escalated 0"; only the detail view said "niemand zuständig".
``assignment.unstaffed_steps`` and ``GET /monitoring/unstaffed`` are the last
line of defence, independent of the modelling-time guards (Z2, ad-hoc B2).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from staffing import staff_via_api, staffed

from procworks import (
    ExecutionContext,
    complete_activity,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
)
from procworks.api import app
from procworks.assignment import unstaffed_steps
from procworks.model import NodeState
from procworks.store import InMemoryInstanceStore

client = TestClient(app)


def _ids(schema, *labels):  # type: ignore[no-untyped-def]
    return [next(n.id for n in schema.nodes.values() if n.label == lab) for lab in labels]


def test_step_without_rule_is_reported_as_no_rule() -> None:
    schema = serial_insert(create_empty_schema("U", schema_id="unst1"), "A", after_node_id="start")
    schema = release(staffed(schema))
    [act] = _ids(schema, "A")
    context = ExecutionContext(lambda *_: None, InMemoryInstanceStore())
    instance = instantiate(schema, context=context)
    assert instance.node_states[act] is NodeState.ACTIVATED
    legacy = schema.model_copy(deep=True)
    del legacy.staff_rules[act]  # e.g. an ad-hoc step from before the B2 gate

    [step] = unstaffed_steps(legacy, instance)

    assert (step.node_id, step.reason) == (act, "no_rule")
    assert unstaffed_steps(schema, instance) == []  # counter-example: staffed


def _four_eyes_after_supervision() -> tuple[str, str]:
    """API instance: "Erfassen" done without a performer, "Freigeben" = its performer."""

    sid = client.post("/schemas", json={"name": "Niemand zuständig"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    erfassen = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Freigeben", "after_node_id": erfassen}
    ).json()
    freigeben = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Freigeben")
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(
        f"/schemas/{sid}/agents", json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"}
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": erfassen, "rule": {"kind": "ROLE", "ref": "sb"}},
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": freigeben, "rule": {"kind": "NODE_PERFORMING_AGENT", "ref": erfassen}},
    )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    # Open mode: completed without a person -> no performer is recorded.
    done = client.post(f"/instances/{iid}/complete", json={"node_id": erfassen})
    assert done.status_code == 200, done.text
    return iid, freigeben


def test_monitoring_lists_a_step_whose_rule_finds_nobody() -> None:
    iid, freigeben = _four_eyes_after_supervision()

    rows = [r for r in client.get("/monitoring/unstaffed").json() if r["instance_id"] == iid]

    assert [(r["node_id"], r["label"], r["reason"]) for r in rows] == [
        (freigeben, "Freigeben", "nobody")
    ]


def test_monitoring_leaves_staffed_steps_and_test_instances_out() -> None:
    sid = client.post("/schemas", json={"name": "Besetzt"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    staff_via_api(client, sid)
    test_iid = client.post(f"/schemas/{sid}/instances").json()["id"]  # draft -> test instance
    client.post(f"/schemas/{sid}/release")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    reported = {r["instance_id"] for r in client.get("/monitoring/unstaffed").json()}

    assert iid not in reported and test_iid not in reported


def test_completing_the_step_away_clears_the_report() -> None:
    schema = serial_insert(create_empty_schema("U2", schema_id="unst2"), "A", after_node_id="start")
    schema = release(staffed(schema))
    [act] = _ids(schema, "A")
    context = ExecutionContext(lambda *_: None, InMemoryInstanceStore())
    instance = instantiate(schema, context=context)
    legacy = schema.model_copy(deep=True)
    del legacy.staff_rules[act]
    assert unstaffed_steps(legacy, instance)

    instance = complete_activity(instance, legacy, act, context=context)

    assert unstaffed_steps(legacy, instance) == []


# --- Wer kann den Schritt tatsaechlich bearbeiten? ----------------------------
# Eine Regel, die Personen findet, hilft nichts, wenn keine davon sich anmelden
# kann oder alle ohne anwesende Vertretung abwesend sind. Beides meldet der
# Bericht -- mit Grund und den betroffenen Personen.


def _one_step_instance(*, with_deputy: bool = False):  # type: ignore[no-untyped-def]
    """Released one-step schema, staffed by the test person (+ optional deputy)."""
    from staffing import TEST_AGENT_ID

    from procworks import add_agent, set_agent_deputy

    schema = serial_insert(create_empty_schema("W", schema_id="unst-w"), "A", after_node_id="start")
    schema = staffed(schema)
    if with_deputy:
        schema = add_agent(schema, "Vera Vertretung", agent_id="a-dep")
        schema = set_agent_deputy(schema, TEST_AGENT_ID, "a-dep")
    schema = release(schema)
    context = ExecutionContext(lambda *_: None, InMemoryInstanceStore())
    return schema, instantiate(schema, context=context), TEST_AGENT_ID


def test_step_whose_only_person_has_no_login_is_reported() -> None:
    schema, instance, person = _one_step_instance()

    [step] = unstaffed_steps(schema, instance, agents_with_login=frozenset({"someone-else"}))

    assert (step.reason, step.agent_ids) == ("no_login", [person])


def test_step_whose_only_person_is_absent_without_deputy_is_reported_weaker() -> None:
    schema, instance, person = _one_step_instance()
    absent = frozenset({person})

    [step] = unstaffed_steps(
        schema, instance, absent_agents=absent, agents_with_login=frozenset({person})
    )

    assert (step.reason, step.agent_ids) == ("only_absent", [person])


def test_present_person_with_login_staffs_the_step() -> None:
    """Negativfall: anwesend und mit Login -> nichts zu melden."""
    schema, instance, person = _one_step_instance()

    assert unstaffed_steps(schema, instance, agents_with_login=frozenset({person})) == []


def test_absent_person_with_present_deputy_staffs_the_step() -> None:
    """Grenzfall: Die anwesende Vertretung (mit Login) uebernimmt -> nicht gemeldet."""
    schema, instance, person = _one_step_instance(with_deputy=True)

    found = unstaffed_steps(
        schema,
        instance,
        absent_agents=frozenset({person}),
        agents_with_login=frozenset({person, "a-dep"}),
    )

    assert found == []


def test_absent_deputy_too_is_only_absent_naming_both() -> None:
    """Grenzfall: Ist auch die Vertretung abwesend, stehen beide im Bericht."""
    schema, instance, person = _one_step_instance(with_deputy=True)

    [step] = unstaffed_steps(
        schema,
        instance,
        absent_agents=frozenset({person, "a-dep"}),
        agents_with_login=frozenset({person, "a-dep"}),
    )

    assert (step.reason, step.agent_ids) == ("only_absent", sorted([person, "a-dep"]))


def test_absent_person_without_login_counts_as_no_login() -> None:
    """Grenzfall: kein Login wiegt schwerer als abwesend."""
    schema, instance, person = _one_step_instance()

    [step] = unstaffed_steps(
        schema, instance, absent_agents=frozenset({person}), agents_with_login=frozenset()
    )

    assert step.reason == "no_login"


def test_unknown_logins_keep_the_previous_behaviour() -> None:
    """Ohne Kenntnis der Logins (Token/JWT) wird der Login nicht geprueft."""
    schema, instance, _person = _one_step_instance()

    assert unstaffed_steps(schema, instance) == []
    assert unstaffed_steps(schema, instance, agents_with_login=None) == []


def test_api_reports_no_login_in_password_mode_and_clears_with_a_login() -> None:
    """Passwort-Modus: Die einzige Zustaendige hat keinen Login -> gemeldet;
    bekommt sie einen, verschwindet die Meldung."""
    import procworks.api as api_module
    from procworks.auth_password import InMemoryCredentialStore, PasswordAuthBackend, hash_password

    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    user, _ = backend.create_user(subject="admin", roles=["admin"], login="admin")
    backend.store.put_user(
        user.model_copy(update={"password_hash": hash_password("admin-pw1"), "must_change": False})
    )
    api_module._auth_backend = backend
    try:
        h = {"Authorization": f"Bearer {backend.login('admin', 'admin-pw1').token}"}
        sid = client.post("/schemas", json={"name": "Ohne Login"}, headers=h).json()["id"]
        client.post(
            f"/schemas/{sid}/serial-insert",
            json={"label": "Pruefen", "after_node_id": "start"},
            headers=h,
        )
        client.post(f"/schemas/{sid}/roles", json={"name": "PT", "role_id": "pt"}, headers=h)
        client.post(
            f"/schemas/{sid}/agents",
            json={"name": "Petra Pruef", "role_ids": ["pt"], "agent_id": "a-petra-nl"},
            headers=h,
        )
        step = next(
            n["id"]
            for n in client.get(f"/schemas/{sid}", headers=h).json()["nodes"].values()
            if n["label"] == "Pruefen"
        )
        client.post(
            f"/schemas/{sid}/staff-rule",
            json={"node_id": step, "rule": {"kind": "ROLE", "ref": "pt"}},
            headers=h,
        )
        assert client.post(f"/schemas/{sid}/release", headers=h).status_code == 200
        iid = client.post(f"/schemas/{sid}/instances", headers=h).json()["id"]

        rows = [r for r in client.get("/monitoring/unstaffed", headers=h).json()
                if r["instance_id"] == iid]
        assert [(r["reason"], r["agent_ids"]) for r in rows] == [("no_login", ["a-petra-nl"])]

        client.post("/users", json={"roles": ["operator"], "agent_id": "a-petra-nl"}, headers=h)
        rows = [r for r in client.get("/monitoring/unstaffed", headers=h).json()
                if r["instance_id"] == iid]
        assert rows == []
    finally:
        api_module._auth_backend = original
