# SPDX-License-Identifier: BUSL-1.1
"""A shared organisation stays maintainable next to findings it did not cause.

A change of a shared organisation is checked against every linked process:
it must not break one of them (for example leave a step without anybody who
may do it, Z2). Rules get stricter over time, though -- a stored, released
process can violate a rule that did not exist when it was released (a time
value above the upper bound, an input pattern the complexity check now
refuses). Such a finding has nothing to do with the organisation, and a
released process cannot be changed to remove it. If it counted against an
organisation change, nobody -- not even an administrator -- could maintain
the organisation any more.

So an organisation change is refused only for the findings it *adds* to a
linked process: each linked process is validated with the stored and with
the candidate organisation, and a finding counts when it appears with the
candidate and not before. A finding is identified by rule, code, step and
parameters -- never by its English text.
"""

from __future__ import annotations

import itertools
from typing import Any

from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks import TimeConstraint, ValidationFinding, findings_added

client = TestClient(api_module.app)
_ids = (f"org-legacy-{n}" for n in itertools.count(1))

#: An address pattern customers commonly used; the complexity check of input
#: patterns refuses it today, a process released earlier may still carry it.
_OLD_PATTERN = r"^.+@.+\..+$"


def _shared_org() -> str:
    """Shared organisation: role ``sb`` held only by ``a1``, role ``lt`` by ``a2``."""

    org_id = next(_ids)
    created = client.post("/org-models", json={"name": "Amt", "org_model_id": org_id})
    assert created.status_code == 201
    client.post(f"/org-models/{org_id}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(f"/org-models/{org_id}/roles", json={"name": "LT", "role_id": "lt"})
    client.post(f"/org-models/{org_id}/agents",
                json={"name": "Erika", "role_ids": ["sb"], "agent_id": "a1"})
    client.post(f"/org-models/{org_id}/agents",
                json={"name": "Lena", "role_ids": ["lt"], "agent_id": "a2"})
    return org_id


def _step(sid: str, label: str, after: str) -> str:
    schema = client.post(f"/schemas/{sid}/serial-insert",
                         json={"label": label, "after_node_id": after}).json()
    return next(n["id"] for n in schema["nodes"].values() if n["label"] == label)


def _released_process(org_id: str, name: str) -> tuple[str, str, str]:
    """A released process linked to ``org_id``: step ``Erfassen`` (role ``sb``,
    a mask with a pattern) and step ``Pruefen`` (role ``lt``).

    :returns: ``(schema id, id of Erfassen, id of Pruefen)``.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    first = _step(sid, "Erfassen", "start")
    second = _step(sid, "Pruefen", first)
    client.post(f"/schemas/{sid}/data-elements",
                json={"name": "Mail", "data_type": "STRING", "element_id": "mail"})
    form = client.post(f"/schemas/{sid}/nodes/{first}/form",
                       json={"fields": [{"element_id": "mail", "widget": "TEXT",
                                         "pattern": "^[a-z]+$"}]})
    assert form.status_code == 200, form.text
    assert client.post(f"/schemas/{sid}/org-model",
                       json={"org_model_id": org_id}).status_code == 200
    for node, role in ((first, "sb"), (second, "lt")):
        ruled = client.post(f"/schemas/{sid}/staff-rule",
                            json={"node_id": node, "rule": {"kind": "ROLE", "ref": role}})
        assert ruled.status_code == 200, ruled.text
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, first, second


def _age(sid: str, node: str) -> None:
    """Give the stored process two findings of rules stricter than at its
    release: a duration above the upper bound on ``node`` and the old address
    pattern on its mask -- written straight into the store, as an update
    would find them."""

    stored = api_module._store.get(sid)
    assert stored is not None
    form = stored.forms[node]
    old_field = form.fields[0].model_copy(update={"pattern": _OLD_PATTERN})
    aged = stored.model_copy(update={
        "time_constraints": {node: TimeConstraint(max_duration_seconds=1e12)},
        "forms": {node: form.model_copy(update={"fields": [old_field]})},
    })
    api_module._store.put(aged)
    codes = {f["code"] for f in client.get(f"/schemas/{sid}/validation").json()["findings"]}
    assert {"T1.duration-too-large", "U2.pattern-too-complex"} <= codes


def _codes(resp: Any) -> list[str]:
    return sorted(f["code"] for f in resp.json()["detail"]["findings"])


# --- positive: findings the change did not cause do not block it ---------------


def test_old_findings_of_a_linked_process_do_not_block_an_org_change() -> None:
    org_id = _shared_org()
    sid, first, _ = _released_process(org_id, "Altprozess")
    _age(sid, first)

    changes = [
        ("post", f"/org-models/{org_id}/roles", {"name": "Neu", "role_id": "neu"}),
        ("post", f"/org-models/{org_id}/org-units", {"name": "Kasse", "org_unit_id": "u1"}),
        ("post", f"/org-models/{org_id}/agents",
         {"name": "Max", "role_ids": ["sb"], "agent_id": "a3", "org_unit_id": "u1"}),
        ("patch", f"/org-models/{org_id}/agents/a3", {"name": "Max Muster"}),
        ("post", f"/org-models/{org_id}/org-units/u1/manager", {"manager_id": "a3"}),
        ("post", f"/org-models/{org_id}/agents/a1/deputy", {"deputy_id": "a3"}),
        ("put", f"/org-models/{org_id}/roles/sb/mailbox", {"mailbox": "sb@example.org"}),
    ]
    for method, path, body in changes:
        resp = getattr(client, method)(path, json=body)
        assert resp.status_code == 200, (path, resp.text)

    org = client.get(f"/org-models/{org_id}").json()
    assert org["agents"]["a3"]["name"] == "Max Muster"
    assert "neu" in org["roles"]


# --- negative: a change that breaks a linked process stays refused ------------


def test_a_change_that_breaks_staffing_is_refused_with_only_its_own_finding() -> None:
    org_id = _shared_org()
    sid, first, second = _released_process(org_id, "Altprozess Z2")
    _age(sid, first)

    resp = client.patch(f"/org-models/{org_id}/agents/a2", json={"role_ids": []})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["message"] == "org change would break referencing schemas"
    assert _codes(resp) == ["Z2.nobody"]
    assert detail["findings"][0]["node_id"] == second
    agents = client.get(f"/org-models/{org_id}").json()["agents"]
    assert agents["a2"]["role_ids"] == ["lt"]


def test_breaking_one_of_two_linked_processes_names_only_that_breakage() -> None:
    """The old findings of one process do not leak into the refusal caused in
    another -- each process is compared with itself."""

    org_id = _shared_org()
    aged, first, _ = _released_process(org_id, "Alt A")
    _age(aged, first)
    sid = client.post("/schemas", json={"name": "Neu B"}).json()["id"]
    step = _step(sid, "Freigeben", "start")
    client.post(f"/schemas/{sid}/org-model", json={"org_model_id": org_id})
    client.post(f"/schemas/{sid}/staff-rule",
                json={"node_id": step, "rule": {"kind": "AGENT", "ref": "a2"}})

    resp = client.patch(f"/org-models/{org_id}/agents/a1", json={"role_ids": []})

    assert resp.status_code == 422
    assert _codes(resp) == ["Z2.nobody"]
    assert resp.json()["detail"]["findings"][0]["node_id"] == first


# --- edge cases ---------------------------------------------------------------


def test_old_finding_on_the_very_step_the_change_breaks_is_not_repeated() -> None:
    """The change takes the only holder off the role of ``Erfassen`` -- the
    step that also carries both old findings. Refused, and only for Z2."""

    org_id = _shared_org()
    sid, first, _ = _released_process(org_id, "Altprozess gleicher Schritt")
    _age(sid, first)

    resp = client.patch(f"/org-models/{org_id}/agents/a1", json={"role_ids": []})

    assert resp.status_code == 422
    findings = resp.json()["detail"]["findings"]
    assert [(f["code"], f["node_id"]) for f in findings] == [("Z2.nobody", first)]


def test_an_org_finding_that_already_existed_does_not_block_an_unrelated_change() -> None:
    """A stored process whose step nobody may do any more (stored that way,
    e.g. by an older version) keeps its finding unchanged through an unrelated
    change -- the change is admitted. Fixing it is admitted as well."""

    org_id = _shared_org()
    sid, _, _ = _released_process(org_id, "Altprozess Z2 vorher")
    stored_org = api_module._org_store.get(org_id)
    assert stored_org is not None
    lena = stored_org.agents["a2"].model_copy(update={"role_ids": []})
    api_module._org_store.put(
        stored_org.model_copy(update={"agents": {**stored_org.agents, "a2": lena}})
    )

    unrelated = client.post(f"/org-models/{org_id}/roles", json={"name": "X", "role_id": "x"})
    assert unrelated.status_code == 200, unrelated.text
    repaired = client.patch(f"/org-models/{org_id}/agents/a2", json={"role_ids": ["lt"]})
    assert repaired.status_code == 200, repaired.text
    codes = {f["code"] for f in client.get(f"/schemas/{sid}/validation").json()["findings"]}
    assert "Z2.nobody" not in codes


def test_findings_added_compares_identity_not_text() -> None:
    """Unit check of the comparison: same rule, code, step and parameters is
    the same finding even if the text differs; a second identical finding is
    new (multiset), and a changed parameter makes it new."""

    def f(code: str, node: str | None, message: str = "m", **params: str) -> ValidationFinding:
        return ValidationFinding(rule=code.split(".")[0], message=message, node_id=node,
                                 code=code, params=params)

    old = [f("T1.duration-too-large", "n1", value="1e12"), f("Z2.nobody", "n2", step="B")]
    new = [
        f("T1.duration-too-large", "n1", message="other text", value="1e12"),
        f("Z2.nobody", "n2", step="B"),
        f("Z2.nobody", "n2", step="B"),
        f("Z2.nobody", "n3", step="C"),
        f("T1.duration-too-large", "n1", value="2e12"),
    ]

    added = findings_added(old, new)

    assert [(x.code, x.node_id, x.params) for x in added] == [
        ("Z2.nobody", "n2", {"step": "B"}),
        ("Z2.nobody", "n3", {"step": "C"}),
        ("T1.duration-too-large", "n1", {"value": "2e12"}),
    ]
    assert findings_added(new, new) == []
    assert findings_added([], old) == old
