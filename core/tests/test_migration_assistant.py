# SPDX-License-Identifier: BUSL-1.1
"""Migration assistant: bulk migration of running instances onto a new revision.

Covers ``GET /schemas/{id}/migration-report``,
``POST /schemas/{id}/migrate-instances`` and
``GET /instances/{id}/migration-target``, plus the pure lineage helper
:func:`procworks.migration.predecessor_ids`.

The assistant adds no migration logic of its own: every instance goes through
the core's M1-M5 check and the same ``_migrate_and_record`` as the single
endpoint. The tests therefore pin what the assistant *adds* -- the candidate
selection, the dry-run default, per-instance atomicity with partial success,
and the shared start values (M4) -- and that a refused instance stays exactly
as it was.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks.api import app
from procworks.migration import predecessor_ids
from procworks.model import LifecycleState, ProcessSchema

client = TestClient(app)


def _labels(schema: dict[str, object]) -> dict[str, str]:
    nodes = schema["nodes"]
    assert isinstance(nodes, dict)
    return {n["label"]: n["id"] for n in nodes.values() if n.get("label")}


def _released_v1(name: str) -> tuple[str, dict[str, str]]:
    """``start -> W -> A -> B -> end``, staffed and released."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    for label in ("B", "A", "W"):
        client.post(
            f"/schemas/{sid}/serial-insert", json={"label": label, "after_node_id": "start"}
        )
    staff_via_api(client, sid)
    schema = client.get(f"/schemas/{sid}").json()
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, _labels(schema)


def _revision(sid: str) -> str:
    rev = client.post(f"/schemas/{sid}/revision", json={}).json()
    assert rev["revision_of"] == sid
    return str(rev["id"])


def _release(sid: str) -> None:
    staff_via_api(client, sid)
    resp = client.post(f"/schemas/{sid}/release")
    assert resp.status_code == 200, resp.text


def _start(sid: str) -> str:
    return str(client.post(f"/schemas/{sid}/instances").json()["id"])


def _complete(iid: str, node_id: str) -> None:
    resp = client.post(f"/instances/{iid}/complete", json={"node_id": node_id})
    assert resp.status_code == 200, resp.text


def _add_element(sid: str, element_id: str) -> None:
    resp = client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Beleg", "data_type": "STRING", "element_id": element_id},
    )
    assert resp.status_code == 200, resp.text


def _bind(sid: str, node_id: str, element_id: str, mode: str) -> None:
    resp = client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": node_id, "element_id": element_id, "mode": mode},
    )
    assert resp.status_code == 200, resp.text


def _target_with_new_input(sid: str, ids: dict[str, str]) -> str:
    """v2: W writes a new mandatory element that B reads (M4 for instances past W)."""

    v2 = _revision(sid)
    _add_element(v2, "doc")
    _bind(v2, ids["W"], "doc", "WRITE")
    _bind(v2, ids["B"], "doc", "READ")
    _release(v2)
    return v2


def test_report_lists_running_instances_of_earlier_revisions_only() -> None:
    sid, ids = _released_v1("Assistent-Liste")
    running = _start(sid)
    finished = _start(sid)
    for label in ("W", "A", "B"):
        _complete(finished, ids[label])
    # Test instances only arise on drafts; plant one directly to pin the filter.
    probe = api_module._instances.get(running)
    assert probe is not None
    test_inst = probe.model_copy(update={"id": running + "-test", "is_test": True})
    api_module._instances.put(test_inst)
    v2 = _revision(sid)
    client.post(f"/schemas/{v2}/serial-insert", json={"label": "C", "after_node_id": ids["B"]})
    _release(v2)

    report = client.get(f"/schemas/{v2}/migration-report").json()
    listed = {c["instance_id"]: c for c in report["candidates"]}
    assert running in listed and finished not in listed
    assert test_inst.id not in listed
    assert listed[running]["migratable"] is True
    assert listed[running]["missing_data"] == []
    assert report["target_version"] == 2


def test_dry_run_is_the_default_and_changes_nothing() -> None:
    sid, ids = _released_v1("Assistent-Trocken")
    iid = _start(sid)
    v2 = _revision(sid)
    client.post(f"/schemas/{v2}/serial-insert", json={"label": "C", "after_node_id": ids["B"]})
    _release(v2)

    report = client.post(f"/schemas/{v2}/migrate-instances", json={}).json()
    assert report["executed"] is False
    assert [r["instance_id"] for r in report["results"]] == [iid]
    assert report["results"][0]["migrated"] is False
    assert client.get(f"/instances/{iid}").json()["schema_id"] == sid


def test_execute_migrates_with_audit_and_partial_success() -> None:
    """One instance needs a start value (M4), the other does not.

    Without the value the first stays on v1 *unchanged* while the second moves;
    with the shared value the first follows and carries exactly that value.
    """

    sid, ids = _released_v1("Assistent-Teil")
    past_w = _start(sid)
    _complete(past_w, ids["W"])
    fresh = _start(sid)
    v2 = _target_with_new_input(sid, ids)

    report = client.get(f"/schemas/{v2}/migration-report").json()
    by_id = {c["instance_id"]: c for c in report["candidates"]}
    assert by_id[past_w]["migratable"] is False
    assert by_id[past_w]["missing_data"] == ["doc"]
    assert [f["rule"] for f in by_id[past_w]["findings"]] == ["M4"]
    assert by_id[fresh]["migratable"] is True

    before = client.get(f"/instances/{past_w}").json()
    first = client.post(f"/schemas/{v2}/migrate-instances", json={"execute": True}).json()
    result = {r["instance_id"]: r for r in first["results"]}
    assert result[fresh]["migrated"] is True
    assert result[past_w]["migrated"] is False
    assert [f["rule"] for f in result[past_w]["findings"]] == ["M4"]
    assert client.get(f"/instances/{past_w}").json() == before
    assert client.get(f"/instances/{fresh}").json()["schema_id"] == v2
    events = client.get(f"/instances/{fresh}/audit").json()
    assert any(e["event_type"] == "INSTANCE_MIGRATED" for e in events)

    second = client.post(
        f"/schemas/{v2}/migrate-instances",
        json={"execute": True, "instance_ids": [past_w], "data_mapping": {"doc": "B-001"}},
    ).json()
    assert second["results"] == [{"instance_id": past_w, "migrated": True, "findings": []}]
    moved = client.get(f"/instances/{past_w}").json()
    assert moved["schema_id"] == v2
    assert moved["data_values"]["doc"] == "B-001"


def test_start_values_are_type_checked_before_anything_moves() -> None:
    sid, ids = _released_v1("Assistent-Typ")
    past_w = _start(sid)
    _complete(past_w, ids["W"])
    fresh = _start(sid)
    v2 = _target_with_new_input(sid, ids)

    resp = client.post(
        f"/schemas/{v2}/migrate-instances",
        json={"execute": True, "data_mapping": {"doc": 42}},
    )
    assert resp.status_code == 422
    assert [f["rule"] for f in resp.json()["detail"]["findings"]] == ["D3"]
    assert client.get(f"/instances/{fresh}").json()["schema_id"] == sid


def test_shared_values_never_overwrite_existing_instance_data() -> None:
    """The assistant fills gaps; it does not rewrite what an instance recorded."""

    sid = client.post("/schemas", json={"name": "Assistent-Bestand"}).json()["id"]
    for label in ("B", "W"):
        client.post(
            f"/schemas/{sid}/serial-insert", json={"label": label, "after_node_id": "start"}
        )
    ids = _labels(client.get(f"/schemas/{sid}").json())
    _add_element(sid, "doc")
    _bind(sid, ids["W"], "doc", "WRITE")
    _bind(sid, ids["B"], "doc", "READ")
    _release(sid)
    iid = _start(sid)
    resp = client.post(
        f"/instances/{iid}/complete", json={"node_id": ids["W"], "data": {"doc": "original"}}
    )
    assert resp.status_code == 200, resp.text
    v2 = _revision(sid)
    client.post(f"/schemas/{v2}/serial-insert", json={"label": "C", "after_node_id": ids["B"]})
    _release(v2)

    report = client.post(
        f"/schemas/{v2}/migrate-instances",
        json={"execute": True, "data_mapping": {"doc": "ueberschrieben"}},
    ).json()
    assert report["results"][0]["migrated"] is True
    moved = client.get(f"/instances/{iid}").json()
    assert moved["schema_id"] == v2
    assert moved["data_values"]["doc"] == "original"


def test_unknown_or_foreign_instances_are_reported_not_migrated() -> None:
    sid, ids = _released_v1("Assistent-Fremd")
    other_sid, _ = _released_v1("Assistent-Anderes")
    foreign = _start(other_sid)
    v2 = _revision(sid)
    _release(v2)

    report = client.post(
        f"/schemas/{v2}/migrate-instances",
        json={"execute": True, "instance_ids": [foreign, "gibt-es-nicht"]},
    ).json()
    assert [r["migrated"] for r in report["results"]] == [False, False]
    assert all(r["findings"][0]["rule"] == "M0" for r in report["results"])
    assert client.get(f"/instances/{foreign}").json()["schema_id"] == other_sid


def test_migration_target_names_the_newest_released_revision() -> None:
    sid, _ = _released_v1("Assistent-Ziel")
    iid = _start(sid)
    assert client.get(f"/instances/{iid}/migration-target").json()["schema_id"] is None

    v2 = _revision(sid)
    _release(v2)
    v3 = _revision(v2)  # draft: must not be offered
    target = client.get(f"/instances/{iid}/migration-target").json()
    assert target["schema_id"] == v2 and target["version"] == 2
    _release(v3)
    assert client.get(f"/instances/{iid}/migration-target").json()["schema_id"] == v3


def _schema(sid: str, name: str, version: int, revision_of: str | None) -> ProcessSchema:
    return ProcessSchema(
        id=sid, name=name, version=version, revision_of=revision_of,
        lifecycle_state=LifecycleState.RELEASED,
    )


def test_predecessors_follow_the_lineage_not_the_name() -> None:
    """Two schemas from the same template share name and node ids -- the chain,
    not the name, decides which instances belong to a revision."""

    v1 = _schema("s1", "Urlaub", 1, None)
    v2 = _schema("s2", "Urlaub", 2, "s1")
    twin = _schema("t1", "Urlaub", 1, None)  # unrelated, same name
    assert predecessor_ids(v2, [v1, v2, twin]) == {"s1"}


def test_predecessors_fall_back_to_name_for_legacy_revisions() -> None:
    """Revisions made before ``revision_of`` existed still find their sources."""

    v1 = _schema("s1", "Alt", 1, None)
    v2 = _schema("s2", "Alt", 2, None)  # legacy: no lineage recorded
    v3 = _schema("s3", "Alt", 3, "s2")
    other = _schema("x9", "Anders", 1, None)
    assert predecessor_ids(v3, [v1, v2, v3, other]) == {"s1", "s2"}
    assert predecessor_ids(v1, [v1, v2, v3]) == set()


def test_templates_start_a_new_lineage() -> None:
    sid, _ = _released_v1("Assistent-Vorlage")
    v2 = _revision(sid)
    _release(v2)
    tpl = client.post("/templates", json={"schema_id": v2, "name": "Aus Revision"}).json()
    fresh = client.post(f"/templates/{tpl['id']}/instantiate", json={}).json()
    assert fresh["revision_of"] is None and fresh["version"] == 1
