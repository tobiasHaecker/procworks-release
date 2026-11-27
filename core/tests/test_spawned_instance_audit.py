# SPDX-License-Identifier: BUSL-1.1
"""Instances the engine starts on its own are recorded like any other start.

Sub-process children and follow-up instances come into being *inside* the
engine, not through a start request -- so the boundary's start record never ran
for them: no ``INSTANCE_CREATED``, and the KPIs, which count instances from the
audit, did not match the instance list. Each spawned instance now gets exactly
one ``INSTANCE_CREATED`` naming the instance that triggered it. Throw-away test
instances and everything they spawn stay out of the audit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from staffing import TEST_AGENT_ID, staff_via_api

import procworks.api as api_module
from procworks.api import app
from procworks.audit import EventType

client = TestClient(app)


def _node_id(schema_id: str, label: str) -> str:
    nodes = client.get(f"/schemas/{schema_id}").json()["nodes"]
    return next(n["id"] for n in nodes.values() if n["label"] == label)


def _one_step_schema(name: str, label: str) -> str:
    """Released schema with a single interactive step ``label``."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": label, "after_node_id": "start"})
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid


def _composed(name: str, *, release: bool = True, condition: str | None = None) -> dict[str, str]:
    """Parent "Vorher" -> SUBPROCESS(child) with a follow-up to another schema.

    The follow-up fires on completion, or only when ``condition`` holds. With
    ``release=False`` the parent stays a draft (for test instances). Returns the
    schema ids ``parent``, ``child``, ``follow`` and the node ``vorher``.
    """

    child = _one_step_schema(f"{name}-Kind", "Kindschritt")
    follow = _one_step_schema(f"{name}-Folge", "Folgeschritt")
    pid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{pid}/serial-insert", json={"label": "Vorher", "after_node_id": "start"})
    vorher = _node_id(pid, "Vorher")
    assert client.post(f"/schemas/{pid}/subprocess", json={
        "label": "Teilprozess", "after_node_id": vorher,
        "target_schema_id": child, "target_version": 1}).status_code == 200
    link: dict[str, object] = {"target_schema_id": follow, "target_version": 1}
    if condition is not None:
        client.post(
            f"/schemas/{pid}/data-elements",
            json={"name": "Betrag", "data_type": "INTEGER", "element_id": "betrag"},
        )
        client.post(
            f"/schemas/{pid}/data-access",
            json={"node_id": vorher, "element_id": "betrag", "mode": "WRITE",
                  "mandatory": True},
        )
        link.update(trigger="CONDITIONAL", condition=condition)
    linked = client.post(f"/schemas/{pid}/follow-up", json=link)
    assert linked.status_code == 200, linked.text
    staff_via_api(client, pid)
    if release:
        assert client.post(f"/schemas/{pid}/release").status_code == 200
    return {"parent": pid, "child": child, "follow": follow, "vorher": vorher}


def _created(instance_id: str) -> list[dict[str, str] | None]:
    """Details of all ``INSTANCE_CREATED`` events of ``instance_id``."""

    return [
        e.detail
        for e in api_module._audit.for_instance(instance_id)
        if e.event_type is EventType.INSTANCE_CREATED
    ]


def _run_to_end(ids: dict[str, str], data: dict[str, object] | None = None) -> tuple[str, str]:
    """Start the parent, work it and its child to the end; return (parent, child)."""

    iid = client.post(f"/schemas/{ids['parent']}/instances").json()["id"]
    done = client.post(
        f"/instances/{iid}/complete", json={"node_id": ids["vorher"], "data": data or {}}
    )
    assert done.status_code == 200, done.text
    child = next(iter(client.get(f"/instances/{iid}").json()["child_instances"].values()))
    finished = client.post(
        f"/instances/{child}/complete",
        json={"node_id": _node_id(ids["child"], "Kindschritt")},
    )
    assert finished.status_code == 200, finished.text
    return iid, child


def test_child_and_follow_up_each_get_exactly_one_creation_event() -> None:
    ids = _composed("Erzeugung")

    iid, child = _run_to_end(ids)

    parent = client.get(f"/instances/{iid}").json()
    assert parent["state"] == "COMPLETED"
    [follow_up] = parent["follow_up_instances"]
    assert _created(child) == [{"origin_instance": iid, "trigger": "subprocess"}]
    assert _created(follow_up) == [{"origin_instance": iid, "trigger": "follow_up"}]


def test_kpis_count_every_real_instance() -> None:
    ids = _composed("Kennzahlen")

    _run_to_end(ids)

    child_kpis = client.get("/monitoring/kpis", params={"schema_id": ids["child"]}).json()
    follow_kpis = client.get("/monitoring/kpis", params={"schema_id": ids["follow"]}).json()
    assert (child_kpis["total_instances"], child_kpis["completed"]) == (1, 1)
    assert (follow_kpis["total_instances"], follow_kpis["running"]) == (1, 1)
    # Every counted instance has a known start, so its cycle time is measurable.
    assert child_kpis["avg_cycle_seconds"] is not None


def test_a_follow_up_whose_condition_fails_records_nothing() -> None:
    ids = _composed("Bedingung", condition="betrag > 1000")

    iid, _child = _run_to_end(ids, {"betrag": 10})

    assert client.get(f"/instances/{iid}").json()["follow_up_instances"] == []
    kpis = client.get("/monitoring/kpis", params={"schema_id": ids["follow"]}).json()
    assert kpis["total_instances"] == 0


def test_a_follow_up_whose_condition_holds_is_recorded() -> None:
    ids = _composed("Bedingung erfuellt", condition="betrag > 1000")

    iid, _child = _run_to_end(ids, {"betrag": 5000})

    [follow_up] = client.get(f"/instances/{iid}").json()["follow_up_instances"]
    assert _created(follow_up) == [{"origin_instance": iid, "trigger": "follow_up"}]


def test_a_test_instance_and_what_it_spawns_stay_out_of_the_audit() -> None:
    ids = _composed("Pruefinstanz", release=False)

    iid, child = _run_to_end(ids)

    parent = client.get(f"/instances/{iid}").json()
    assert parent["is_test"] is True
    assert parent["state"] == "COMPLETED"
    [follow_up] = parent["follow_up_instances"]
    for instance_id in (iid, child, follow_up):
        assert _created(instance_id) == [], instance_id


def test_a_child_that_finishes_at_once_is_recorded_as_started_and_completed() -> None:
    """A sub-process without an interactive step ends while it is spawned.

    No request ever advances that child, so its completion would otherwise
    never reach the audit and the KPIs would count it as running forever.
    """

    empty = client.post("/schemas", json={"name": "Sofort-Kind"}).json()["id"]
    assert client.post(f"/schemas/{empty}/release").status_code == 200
    pid = client.post("/schemas", json={"name": "Sofort-Eltern"}).json()["id"]
    client.post(f"/schemas/{pid}/serial-insert", json={"label": "Vorher", "after_node_id": "start"})
    vorher = _node_id(pid, "Vorher")
    assert client.post(f"/schemas/{pid}/subprocess", json={
        "label": "Teilprozess", "after_node_id": vorher,
        "target_schema_id": empty, "target_version": 1}).status_code == 200
    staff_via_api(client, pid)
    assert client.post(f"/schemas/{pid}/release").status_code == 200
    iid = client.post(f"/schemas/{pid}/instances").json()["id"]

    client.post(f"/instances/{iid}/complete", json={"node_id": vorher})

    child = next(iter(client.get(f"/instances/{iid}").json()["child_instances"].values()))
    types = [e.event_type for e in api_module._audit.for_instance(child)]
    assert types == [EventType.INSTANCE_CREATED, EventType.INSTANCE_COMPLETED]
    kpis = client.get("/monitoring/kpis", params={"schema_id": empty}).json()
    assert (kpis["total_instances"], kpis["completed"], kpis["running"]) == (1, 1, 0)


# --- what a test instance spawns is itself a test instance --------------------
#
# A test instance (a draft played through by a modeller) must have no effect on
# real operations. Its sub-process children and follow-ups target *released*
# schemas, so before they inherited the flag they looked like real cases: they
# stood in personal worklists, their completion wrote audit events and fired
# the ``instance.completed`` webhook, and the KPIs disagreed with the instance
# list. Inheriting ``is_test`` at birth makes every existing test-instance rule
# apply to them without a single extra lineage check.


def _targets(name: str, *, nested: bool = False) -> dict[str, str]:
    """Released targets shared by a real and a test parent.

    ``child`` (one step "Kindschritt", with ``nested`` followed by a
    sub-process to ``grandchild``), ``async`` and ``sync`` (one step each,
    used as ASYNC and SYNC follow-up targets).
    """

    ids: dict[str, str] = {}
    if nested:
        ids["grandchild"] = _one_step_schema(f"{name}-Enkel", "Enkelschritt")
        cid = client.post("/schemas", json={"name": f"{name}-Kind"}).json()["id"]
        client.post(
            f"/schemas/{cid}/serial-insert",
            json={"label": "Kindschritt", "after_node_id": "start"},
        )
        sub = client.post(f"/schemas/{cid}/subprocess", json={
            "label": "Enkelteil", "after_node_id": _node_id(cid, "Kindschritt"),
            "target_schema_id": ids["grandchild"], "target_version": 1})
        assert sub.status_code == 200, sub.text
        staff_via_api(client, cid)
        assert client.post(f"/schemas/{cid}/release").status_code == 200
        ids["child"] = cid
    else:
        ids["child"] = _one_step_schema(f"{name}-Kind", "Kindschritt")
    ids["async"] = _one_step_schema(f"{name}-Folge-async", "Folgeschritt")
    ids["sync"] = _one_step_schema(f"{name}-Folge-sync", "Folgeschritt")
    return ids


def _parent(name: str, targets: dict[str, str], *, release: bool) -> dict[str, str]:
    """Parent "Vorher" -> SUBPROCESS(child), with an ASYNC and a SYNC follow-up.

    Without ``release`` it stays a draft, so starting it yields a test instance.
    Returns the parent schema id and its node ``vorher``.
    """

    pid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{pid}/serial-insert", json={"label": "Vorher", "after_node_id": "start"})
    vorher = _node_id(pid, "Vorher")
    assert client.post(f"/schemas/{pid}/subprocess", json={
        "label": "Teilprozess", "after_node_id": vorher,
        "target_schema_id": targets["child"], "target_version": 1}).status_code == 200
    for mode in ("ASYNC", "SYNC"):
        linked = client.post(f"/schemas/{pid}/follow-up", json={
            "target_schema_id": targets[mode.lower()], "target_version": 1, "mode": mode})
        assert linked.status_code == 200, linked.text
    staff_via_api(client, pid)
    if release:
        assert client.post(f"/schemas/{pid}/release").status_code == 200
    return {"parent": pid, "vorher": vorher}


def _instance(instance_id: str) -> dict[str, object]:
    return client.get(f"/instances/{instance_id}").json()  # type: ignore[no-any-return]


def _start_and_spawn_child(parent: dict[str, str]) -> tuple[str, str]:
    """Start the parent and finish "Vorher"; return (parent, sub-process child)."""

    iid = client.post(f"/schemas/{parent['parent']}/instances").json()["id"]
    done = client.post(f"/instances/{iid}/complete", json={"node_id": parent["vorher"]})
    assert done.status_code == 200, done.text
    [child] = _instance(iid)["child_instances"].values()  # type: ignore[union-attr]
    return iid, str(child)


def _complete_only_step(instance_id: str, label: str) -> None:
    schema_id = str(_instance(instance_id)["schema_id"])
    done = client.post(
        f"/instances/{instance_id}/complete", json={"node_id": _node_id(schema_id, label)}
    )
    assert done.status_code == 200, done.text


def _follow_ups(parent_id: str, targets: dict[str, str]) -> dict[str, str]:
    """The parent's follow-up instances keyed by mode (``async``/``sync``)."""

    by_schema = {
        str(_instance(f)["schema_id"]): f
        for f in _instance(parent_id)["follow_up_instances"]  # type: ignore[union-attr]
    }
    return {mode: by_schema[targets[mode]] for mode in ("async", "sync")}


def _worklist() -> set[str]:
    """Instance ids in the personal worklist of the staffing helper's agent."""

    tasks = client.get(f"/agents/{TEST_AGENT_ID}/tasks").json()
    return {t["instance_id"] for t in tasks}


def _run_family(parent: dict[str, str], targets: dict[str, str]) -> dict[str, str]:
    """Run parent and child to the end; return the ids of the whole family."""

    iid, child = _start_and_spawn_child(parent)
    _complete_only_step(child, "Kindschritt")
    assert _instance(iid)["state"] == "COMPLETED"
    return {"parent": iid, "child": child, **_follow_ups(iid, targets)}


def test_sub_process_child_and_both_follow_ups_of_a_test_instance_are_test_instances() -> None:
    targets = _targets("Erbe")
    family = _run_family(_parent("Erbe-Entwurf", targets, release=False), targets)

    flags = {role: _instance(iid)["is_test"] for role, iid in family.items()}
    assert flags == {"parent": True, "child": True, "async": True, "sync": True}
    # The ASYNC follow-up has no back-reference -- the flag is all that ties
    # it to the test instance, so it must have been set at birth.
    assert _instance(family["async"])["parent_instance_id"] is None


def test_the_same_family_started_for_real_stays_real() -> None:
    targets = _targets("Echt")
    family = _run_family(_parent("Echt-Freigabe", targets, release=True), targets)

    flags = {role: _instance(iid)["is_test"] for role, iid in family.items()}
    assert flags == {"parent": False, "child": False, "async": False, "sync": False}
    worklist = _worklist()
    assert {family["async"], family["sync"]} <= worklist


def test_spawned_test_instances_stand_in_no_personal_worklist() -> None:
    targets = _targets("Liste")
    test = _parent("Liste-Entwurf", targets, release=False)
    real = _parent("Liste-Freigabe", targets, release=True)

    _test_parent, test_child = _start_and_spawn_child(test)
    _real_parent, real_child = _start_and_spawn_child(real)

    worklist = _worklist()
    # Same target schema, same staff rule, same agent: only the flag differs.
    assert real_child in worklist
    assert test_child not in worklist
    test_family = _run_family(test, targets)
    assert not {test_family["async"], test_family["sync"]} & _worklist()


def test_completing_what_a_test_instance_spawned_records_and_announces_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        api_module, "_emit_event", lambda kind, payload: sent.append((kind, dict(payload)))
    )
    targets = _targets("Stumm")
    family = _run_family(_parent("Stumm-Entwurf", targets, release=False), targets)

    for mode in ("async", "sync"):
        _complete_only_step(family[mode], "Folgeschritt")
        assert _instance(family[mode])["state"] == "COMPLETED"

    for role, iid in family.items():
        assert api_module._audit.for_instance(iid) == [], role
    announced = {p.get("instance_id") for _kind, p in sent}
    assert not announced & set(family.values()), sent


def test_kpis_match_the_instance_list_without_test_instances() -> None:
    targets = _targets("Abgleich")
    real = _run_family(_parent("Abgleich-Freigabe", targets, release=True), targets)
    test = _run_family(_parent("Abgleich-Entwurf", targets, release=False), targets)
    for family in (real, test):
        for mode in ("async", "sync"):
            _complete_only_step(family[mode], "Folgeschritt")

    listed = [_instance(i) for i in client.get("/instances").json()]
    for key in ("child", "async", "sync"):
        real_ones = [
            i for i in listed if i["schema_id"] == targets[key] and not i["is_test"]
        ]
        kpis = client.get("/monitoring/kpis", params={"schema_id": targets[key]}).json()
        assert len(real_ones) == 1, key
        assert (kpis["total_instances"], kpis["completed"], kpis["running"]) == (1, 1, 0), key


def test_a_grandchild_of_a_test_instance_is_a_test_instance_too() -> None:
    targets = _targets("Enkel", nested=True)
    test = _parent("Enkel-Entwurf", targets, release=False)
    real = _parent("Enkel-Freigabe", targets, release=True)

    grandchildren: dict[str, str] = {}
    for kind, parent in (("test", test), ("real", real)):
        _iid, child = _start_and_spawn_child(parent)
        _complete_only_step(child, "Kindschritt")
        [grandchild] = _instance(child)["child_instances"].values()  # type: ignore[union-attr]
        grandchildren[kind] = str(grandchild)

    assert _instance(grandchildren["test"])["is_test"] is True
    assert _instance(grandchildren["real"])["is_test"] is False
    worklist = _worklist()
    assert grandchildren["real"] in worklist
    assert grandchildren["test"] not in worklist
    assert api_module._audit.for_instance(grandchildren["test"]) == []
