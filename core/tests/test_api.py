# SPDX-License-Identifier: BUSL-1.1
"""API tests using FastAPI's TestClient (httpx-based)."""

from __future__ import annotations

from fastapi.testclient import TestClient

import procworks
from procworks.api import app

client = TestClient(app)


def test_health() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # The API reports the installed package version (single source of truth).
    assert body["version"] == procworks.__version__
    assert body["version"] and body["version"] != "0.0.0+unknown"


def test_openapi_reports_package_version() -> None:
    # The OpenAPI schema version tracks the package, not a hard-coded string.
    assert app.version == procworks.__version__


def test_create_and_build_schema_via_api() -> None:
    resp = client.post("/schemas", json={"name": "Urlaubsantrag"})
    assert resp.status_code == 201
    schema = resp.json()
    sid = schema["id"]
    assert schema["lifecycle_state"] == "ENTWURF"

    resp = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Antrag prüfen", "after_node_id": "start"},
    )
    assert resp.status_code == 200
    pruefen = next(
        nid
        for nid, n in client.get(f"/schemas/{sid}").json()["nodes"].items()
        if n.get("label") == "Antrag prüfen"
    )

    resp = client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "betrag", "data_type": "INTEGER", "element_id": "betrag"},
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": pruefen, "element_id": "betrag", "mode": "WRITE"},
    )
    assert resp.status_code == 200

    resp = client.post(
        f"/schemas/{sid}/conditional-insert",
        json={
            "after_node_id": pruefen,
            "discriminator": "betrag",
            "branches": [
                {"label": "Freigabe Leitung", "upper": 1001},
                {"label": "Freigabe Team"},
            ],
        },
    )
    assert resp.status_code == 200

    resp = client.get(f"/schemas/{sid}/validation")
    assert resp.status_code == 200
    assert resp.json()["correct"] is True


def test_invalid_operation_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "X"}).json()["id"]
    resp = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "X", "after_node_id": "end"},
    )
    assert resp.status_code == 422
    assert "findings" in resp.json()["detail"]


def test_rename_and_delete_node_via_api() -> None:
    sid = client.post("/schemas", json={"name": "Bearbeiten"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Alt", "after_node_id": "start"})
    schema = client.get(f"/schemas/{sid}").json()
    act_id = next(nid for nid, n in schema["nodes"].items() if n["type"] == "ACTIVITY")

    resp = client.patch(f"/schemas/{sid}/nodes/{act_id}", json={"label": "Neu"})
    assert resp.status_code == 200
    assert resp.json()["nodes"][act_id]["label"] == "Neu"

    resp = client.delete(f"/schemas/{sid}/nodes/{act_id}")
    assert resp.status_code == 200
    assert act_id not in resp.json()["nodes"]
    assert client.get(f"/schemas/{sid}/validation").json()["correct"] is True


def test_delete_join_via_api_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "JoinDel"}).json()["id"]
    client.post(
        f"/schemas/{sid}/parallel-insert",
        json={"branch_labels": ["A", "B"], "after_node_id": "start"},
    )
    schema = client.get(f"/schemas/{sid}").json()
    join_id = next(nid for nid, n in schema["nodes"].items() if n["type"] == "AND_JOIN")
    resp = client.delete(f"/schemas/{sid}/nodes/{join_id}")
    assert resp.status_code == 422
    assert "findings" in resp.json()["detail"]


def test_delete_split_via_api_removes_block() -> None:
    sid = client.post("/schemas", json={"name": "BlockDel"}).json()["id"]
    client.post(
        f"/schemas/{sid}/parallel-insert",
        json={"branch_labels": ["A", "B"], "after_node_id": "start"},
    )
    schema = client.get(f"/schemas/{sid}").json()
    split_id = next(nid for nid, n in schema["nodes"].items() if n["type"] == "AND_SPLIT")
    resp = client.delete(f"/schemas/{sid}/nodes/{split_id}")
    assert resp.status_code == 200
    types = {n["type"] for n in resp.json()["nodes"].values()}
    assert types == {"START", "END"}


def test_delete_branch_via_api_dissolves_gateway() -> None:
    sid = client.post("/schemas", json={"name": "ZweigDel"}).json()["id"]
    client.post(
        f"/schemas/{sid}/parallel-insert",
        json={"branch_labels": ["A", "B"], "after_node_id": "start"},
    )
    schema = client.get(f"/schemas/{sid}").json()
    branch_a = next(nid for nid, n in schema["nodes"].items() if n.get("label") == "A")
    resp = client.delete(f"/schemas/{sid}/nodes/{branch_a}")
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    types = {n["type"] for n in nodes.values()}
    assert "AND_SPLIT" not in types and "AND_JOIN" not in types
    labels = {n.get("label") for n in nodes.values() if n["type"] == "ACTIVITY"}
    assert labels == {"B"}
    assert client.get(f"/schemas/{sid}/validation").json()["correct"] is True


def test_release_via_api_then_immutable() -> None:
    sid = client.post("/schemas", json={"name": "R"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "S", "after_node_id": "start"})
    resp = client.post(f"/schemas/{sid}/release")
    assert resp.status_code == 200
    assert resp.json()["lifecycle_state"] == "RELEASED"

    resp = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Y", "after_node_id": "start"},
    )
    assert resp.status_code == 422


def test_unknown_schema_returns_404() -> None:
    resp = client.get("/schemas/nope")
    assert resp.status_code == 404


def test_data_flow_via_api() -> None:
    sid = client.post("/schemas", json={"name": "Daten"}).json()["id"]
    writer = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    ).json()
    writer_id = next(n["id"] for n in writer["nodes"].values() if n["label"] == "Erfassen")
    reader = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Pruefen", "after_node_id": writer_id},
    ).json()
    reader_id = next(n["id"] for n in reader["nodes"].values() if n["label"] == "Pruefen")

    resp = client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "betrag", "data_type": "FLOAT", "element_id": "betrag"},
    )
    assert resp.status_code == 200

    resp = client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": writer_id, "element_id": "betrag", "mode": "WRITE"},
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": reader_id, "element_id": "betrag", "mode": "READ"},
    )
    assert resp.status_code == 200
    assert client.get(f"/schemas/{sid}/validation").json()["correct"] is True


def test_data_flow_read_before_write_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "D1"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Liest", "after_node_id": "start"},
    ).json()
    reader_id = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Liest")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "x", "data_type": "INTEGER", "element_id": "x"},
    )
    resp = client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": reader_id, "element_id": "x", "mode": "READ"},
    )
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "D1" in rules


def test_staff_rule_via_api() -> None:
    sid = client.post("/schemas", json={"name": "BZR"}).json()["id"]
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
    resp = client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": act_id, "rule": {"kind": "ROLE", "ref": "sb"}},
    )
    assert resp.status_code == 200
    assert client.get(f"/schemas/{sid}/validation").json()["correct"] is True


def test_update_agent_via_api() -> None:
    sid = client.post("/schemas", json={"name": "EditAgent"}).json()["id"]
    client.post(f"/schemas/{sid}/roles", json={"name": "Sachbearbeiter", "role_id": "sb"})
    client.post(f"/schemas/{sid}/roles", json={"name": "Manager", "role_id": "mgr"})
    client.post(
        f"/schemas/{sid}/org-units", json={"name": "Einkauf", "org_unit_id": "einkauf"}
    )
    client.post(
        f"/schemas/{sid}/agents",
        json={
            "name": "Erika",
            "role_ids": ["sb"],
            "org_unit_id": "einkauf",
            "agent_id": "a1",
        },
    )

    # Rename + change roles; org unit omitted -> kept.
    resp = client.patch(
        f"/schemas/{sid}/agents/a1",
        json={"name": "Erika Mustermann", "role_ids": ["mgr"]},
    )
    assert resp.status_code == 200
    agent = resp.json()["org_model"]["agents"]["a1"]
    assert agent["name"] == "Erika Mustermann"
    assert agent["role_ids"] == ["mgr"]
    assert agent["org_unit_id"] == "einkauf"

    # Explicit null detaches the org unit.
    resp = client.patch(f"/schemas/{sid}/agents/a1", json={"org_unit_id": None})
    assert resp.status_code == 200
    assert resp.json()["org_model"]["agents"]["a1"]["org_unit_id"] is None


def test_update_unknown_agent_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "EditNoAgent"}).json()["id"]
    resp = client.patch(f"/schemas/{sid}/agents/ghost", json={"name": "X"})
    assert resp.status_code == 422
    assert "findings" in resp.json()["detail"]


def test_staff_rule_unknown_role_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "BZRbad"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Bearbeiten", "after_node_id": "start"},
    ).json()
    act_id = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Bearbeiten")
    resp = client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": act_id, "rule": {"kind": "ROLE", "ref": "ghost"}},
    )
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "Z1" in rules


def test_instance_run_via_api() -> None:
    sid = client.post("/schemas", json={"name": "Lauf"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "S", "after_node_id": "start"})
    client.post(f"/schemas/{sid}/release")

    resp = client.post(f"/schemas/{sid}/instances")
    assert resp.status_code == 201
    instance = resp.json()
    iid = instance["id"]
    assert instance["state"] == "RUNNING"

    wl = client.get(f"/instances/{iid}/worklist").json()
    assert len(wl["ready_activities"]) == 1
    node_id = wl["ready_activities"][0]

    resp = client.post(f"/instances/{iid}/complete", json={"node_id": node_id})
    assert resp.status_code == 200
    assert resp.json()["state"] == "COMPLETED"


def test_put_instance_data_right_after_start() -> None:
    # Data can be entered immediately after starting an instance, before the
    # first activity is worked on -- no activity completion required.
    sid = client.post("/schemas", json={"name": "FrueheDaten"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "S", "after_node_id": "start"})
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "betrag", "data_type": "INTEGER", "element_id": "betrag"},
    )
    client.post(f"/schemas/{sid}/release")

    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    # No activity has been completed yet.
    assert client.get(f"/instances/{iid}").json()["data_values"] == {}

    resp = client.put(f"/instances/{iid}/data", json={"values": {"betrag": 1200}})
    assert resp.status_code == 200
    assert resp.json()["betrag"] == 1200
    assert client.get(f"/instances/{iid}").json()["data_values"] == {"betrag": 1200}


def test_put_instance_data_rejects_unknown_and_mistyped() -> None:
    sid = client.post("/schemas", json={"name": "FrueheDatenBad"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "S", "after_node_id": "start"})
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "betrag", "data_type": "INTEGER", "element_id": "betrag"},
    )
    client.post(f"/schemas/{sid}/release")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    unknown = client.put(f"/instances/{iid}/data", json={"values": {"ghost": 1}})
    assert unknown.status_code == 422
    assert "D3" in {f["rule"] for f in unknown.json()["detail"]["findings"]}

    mistyped = client.put(f"/instances/{iid}/data", json={"values": {"betrag": "viel"}})
    assert mistyped.status_code == 422
    assert "D3" in {f["rule"] for f in mistyped.json()["detail"]["findings"]}


def test_set_form_via_api_and_reject_mismatch() -> None:
    sid = client.post("/schemas", json={"name": "MaskeApi"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    )
    node = next(
        nid
        for nid, n in client.get(f"/schemas/{sid}").json()["nodes"].items()
        if n.get("label") == "Erfassen"
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Name", "data_type": "STRING", "element_id": "name"},
    )

    ok = client.post(
        f"/schemas/{sid}/nodes/{node}/form",
        json={"title": "Antrag", "fields": [{"element_id": "name", "widget": "TEXT"}]},
    )
    assert ok.status_code == 200
    assert node in ok.json()["forms"]

    # A checkbox cannot present a STRING element -> 422 with a U2 finding.
    bad = client.post(
        f"/schemas/{sid}/nodes/{node}/form",
        json={"fields": [{"element_id": "name", "widget": "CHECKBOX"}]},
    )
    assert bad.status_code == 422
    assert "U2" in {f["rule"] for f in bad.json()["detail"]["findings"]}

    deleted = client.request("DELETE", f"/schemas/{sid}/nodes/{node}/form")
    assert deleted.status_code == 200
    assert node not in deleted.json()["forms"]


def test_monitoring_revision_tracks_progress() -> None:
    # The lightweight revision counter backs the web client's auto-refresh: it
    # must strictly increase whenever runtime progress is recorded so a poll can
    # detect "something changed" without diffing the whole monitoring payload.
    sid = client.post("/schemas", json={"name": "RevLauf"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "S", "after_node_id": "start"})
    client.post(f"/schemas/{sid}/release")

    before = client.get("/monitoring/revision")
    assert before.status_code == 200
    rev_before = before.json()["revision"]
    assert isinstance(rev_before, int)

    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    rev_started = client.get("/monitoring/revision").json()["revision"]
    assert rev_started > rev_before

    node_id = client.get(f"/instances/{iid}/worklist").json()["ready_activities"][0]
    client.post(f"/instances/{iid}/complete", json={"node_id": node_id})
    rev_completed = client.get("/monitoring/revision").json()["revision"]
    assert rev_completed > rev_started

    # In open dev mode the principal holds every role (incl. modeler/admin), so
    # a draft schema can be started as a flagged, KPI-excluded *test* instance.
    sid = client.post("/schemas", json={"name": "NochEntwurf"}).json()["id"]
    resp = client.post(f"/schemas/{sid}/instances")
    assert resp.status_code == 201
    instance = resp.json()
    assert instance["is_test"] is True
    # Test instances record no audit events -> they never reach the KPIs.
    kpis = client.get(f"/monitoring/kpis?schema_id={sid}").json()
    assert kpis["total_instances"] == 0

def test_test_instance_stays_out_of_audit_through_completion() -> None:
    # A throw-away test instance of a draft (started e.g. from the Prüfinstanz
    # cockpit) must never pollute the monitoring KPIs or the audit log -- not
    # only at creation but for its *whole* lifecycle, incl. step completion.
    sid = client.post("/schemas", json={"name": "PruefinstanzLauf"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "S", "after_node_id": "start"})
    # Intentionally NOT released -> instantiation yields a flagged test instance.

    rev_before = client.get("/monitoring/revision").json()["revision"]
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    # Drive the test instance to completion.
    while True:
        wl = client.get(f"/instances/{iid}/worklist").json()
        if wl["state"] == "COMPLETED":
            break
        node_id = wl["ready_activities"][0]
        resp = client.post(f"/instances/{iid}/complete", json={"node_id": node_id})
        assert resp.status_code == 200

    # The instance really progressed to COMPLETED ...
    assert client.get(f"/instances/{iid}").json()["state"] == "COMPLETED"
    # ... yet left no trace in the audit log, the KPIs, or the revision counter.
    assert client.get(f"/instances/{iid}/audit").json() == []
    assert client.get(f"/monitoring/kpis?schema_id={sid}").json()["total_instances"] == 0
    assert client.get("/monitoring/revision").json()["revision"] == rev_before

def test_subprocess_via_api() -> None:
    # released target schema
    tid = client.post("/schemas", json={"name": "Teilprozess"}).json()["id"]
    client.post(f"/schemas/{tid}/serial-insert", json={"label": "T", "after_node_id": "start"})
    client.post(f"/schemas/{tid}/release")

    pid = client.post("/schemas", json={"name": "Haupt"}).json()["id"]
    resp = client.post(
        f"/schemas/{pid}/subprocess",
        json={"after_node_id": "start", "target_schema_id": tid, "target_version": 1},
    )
    assert resp.status_code == 200
    types = {n["type"] for n in resp.json()["nodes"].values()}
    assert "SUBPROCESS" in types
    assert client.get(f"/schemas/{pid}/validation").json()["correct"] is True


def test_subprocess_unreleased_target_returns_422() -> None:
    tid = client.post("/schemas", json={"name": "Entwurfsziel"}).json()["id"]
    pid = client.post("/schemas", json={"name": "Haupt2"}).json()["id"]
    resp = client.post(
        f"/schemas/{pid}/subprocess",
        json={"after_node_id": "start", "target_schema_id": tid, "target_version": 1},
    )
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "H1" in rules


def test_subprocess_execution_via_api() -> None:
    # released target schema with one activity
    tid = client.post("/schemas", json={"name": "Kindprozess"}).json()["id"]
    client.post(f"/schemas/{tid}/serial-insert", json={"label": "T", "after_node_id": "start"})
    client.post(f"/schemas/{tid}/release")

    # parent with a SUBPROCESS node right after start, then released
    pid = client.post("/schemas", json={"name": "Elternlauf"}).json()["id"]
    client.post(
        f"/schemas/{pid}/subprocess",
        json={"after_node_id": "start", "target_schema_id": tid, "target_version": 1},
    )
    client.post(f"/schemas/{pid}/release")

    inst = client.post(f"/schemas/{pid}/instances").json()
    assert inst["state"] == "RUNNING"
    assert inst["child_instances"]  # the sub-process spawned a child
    child_id = next(iter(inst["child_instances"].values()))

    wl = client.get(f"/instances/{child_id}/worklist").json()
    assert len(wl["ready_activities"]) == 1
    node_id = wl["ready_activities"][0]

    resp = client.post(f"/instances/{child_id}/complete", json={"node_id": node_id})
    assert resp.status_code == 200
    assert resp.json()["state"] == "COMPLETED"

    # completing the child joins back and drives the parent to completion
    parent = client.get(f"/instances/{inst['id']}").json()
    assert parent["state"] == "COMPLETED"


def _released_producer_via_api(name: str, schema_hint: str) -> str:
    """Released sub-process guaranteeing to write ``ergebnis``; returns its id."""

    tid = client.post("/schemas", json={"name": name}).json()["id"]
    r = client.post(f"/schemas/{tid}/serial-insert", json={"label": "R", "after_node_id": "start"})
    act = next(n["id"] for n in r.json()["nodes"].values() if n["type"] == "ACTIVITY")
    client.post(
        f"/schemas/{tid}/data-elements",
        json={"name": "ergebnis", "data_type": "FLOAT", "element_id": "ergebnis"},
    )
    client.post(
        f"/schemas/{tid}/data-access",
        json={"node_id": act, "element_id": "ergebnis", "mode": "WRITE"},
    )
    client.post(f"/schemas/{tid}/release")
    return tid


def test_convert_activity_to_subprocess_via_api() -> None:
    tid = _released_producer_via_api("KonvZiel", "conv")
    pid = client.post("/schemas", json={"name": "KonvHaupt"}).json()["id"]
    r = client.post(
        f"/schemas/{pid}/serial-insert", json={"label": "Schritt", "after_node_id": "start"}
    )
    node_id = next(n["id"] for n in r.json()["nodes"].values() if n["type"] == "ACTIVITY")
    client.post(
        f"/schemas/{pid}/data-elements",
        json={"name": "summe", "data_type": "FLOAT", "element_id": "summe"},
    )
    resp = client.post(
        f"/schemas/{pid}/convert-to-subprocess",
        json={
            "node_id": node_id,
            "target_schema_id": tid,
            "target_version": 1,
            "output_mapping": {"ergebnis": "summe"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["nodes"][node_id]["type"] == "SUBPROCESS"
    assert client.get(f"/schemas/{pid}/validation").json()["correct"] is True


def test_convert_activity_unproduced_output_returns_422() -> None:
    # target that declares but never guarantees to write ``ergebnis``
    tid = client.post("/schemas", json={"name": "LoseZiel"}).json()["id"]
    client.post(f"/schemas/{tid}/serial-insert", json={"label": "T", "after_node_id": "start"})
    client.post(
        f"/schemas/{tid}/data-elements",
        json={"name": "ergebnis", "data_type": "FLOAT", "element_id": "ergebnis"},
    )
    client.post(f"/schemas/{tid}/release")
    pid = client.post("/schemas", json={"name": "LoseHaupt"}).json()["id"]
    r = client.post(
        f"/schemas/{pid}/serial-insert", json={"label": "Schritt", "after_node_id": "start"}
    )
    node_id = next(n["id"] for n in r.json()["nodes"].values() if n["type"] == "ACTIVITY")
    client.post(
        f"/schemas/{pid}/data-elements",
        json={"name": "summe", "data_type": "FLOAT", "element_id": "summe"},
    )
    resp = client.post(
        f"/schemas/{pid}/convert-to-subprocess",
        json={
            "node_id": node_id,
            "target_schema_id": tid,
            "target_version": 1,
            "output_mapping": {"ergebnis": "summe"},
        },
    )
    assert resp.status_code == 422
    assert "H2" in {f["rule"] for f in resp.json()["detail"]["findings"]}


def test_subprocess_library_flag_and_listing() -> None:
    tid = _released_producer_via_api("BibZiel", "lib")
    # not listed until flagged
    ids_before = {e["id"] for e in client.get("/subprocess-library").json()}
    assert tid not in ids_before
    resp = client.post(f"/schemas/{tid}/library-flag", json={"is_library": True})
    assert resp.status_code == 200
    assert resp.json()["is_library_subprocess"] is True
    library = client.get("/subprocess-library").json()
    entry = next(e for e in library if e["id"] == tid)
    assert entry["version"] == 1
    assert any(el["id"] == "ergebnis" for el in entry["data_elements"])


def _released_two_step(name: str) -> str:
    """Create + release a start -> A -> B -> end schema; return its id."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "B", "after_node_id": "start"})
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    client.post(f"/schemas/{sid}/release")
    return sid


def test_adhoc_insert_via_api_runs_through_variant() -> None:
    sid = _released_two_step("AdhocLauf")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    wl = client.get(f"/instances/{iid}/worklist").json()
    a_id = wl["ready_activities"][0]

    resp = client.post(
        f"/instances/{iid}/adhoc/insert",
        json={"after_node_id": a_id, "label": "Zusatz"},
    )
    assert resp.status_code == 200
    instance = resp.json()
    assert instance["ad_hoc_schema"] is not None
    assert instance["ad_hoc_deltas"]

    # Drive the instance to completion through its variant.
    client.post(f"/instances/{iid}/complete", json={"node_id": a_id})
    while True:
        wl = client.get(f"/instances/{iid}/worklist").json()
        if wl["state"] == "COMPLETED":
            break
        node_id = wl["ready_activities"][0]
        client.post(f"/instances/{iid}/complete", json={"node_id": node_id})
    assert client.get(f"/instances/{iid}").json()["state"] == "COMPLETED"


def test_adhoc_insert_after_executed_node_returns_422() -> None:
    sid = _released_two_step("AdhocFehler")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    resp = client.post(
        f"/instances/{iid}/adhoc/insert",
        json={"after_node_id": "start", "label": "ZuSpaet"},
    )
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "R1" in rules


def test_adhoc_rename_via_api_updates_variant_label() -> None:
    sid = _released_two_step("AdhocUmbenennen")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    # B is the not-yet-reached activity (start -> A -> B -> end).
    b_id = next(
        nid
        for nid, n in client.get(f"/schemas/{sid}").json()["nodes"].items()
        if n.get("label") == "B"
    )

    resp = client.post(
        f"/instances/{iid}/adhoc/rename",
        json={"node_id": b_id, "label": "B (angepasst)"},
    )
    assert resp.status_code == 200
    instance = resp.json()
    assert instance["ad_hoc_schema"]["nodes"][b_id]["label"] == "B (angepasst)"
    assert instance["ad_hoc_deltas"]

    audit = client.get(f"/instances/{iid}/audit").json()
    assert any(ev["event_type"] == "ADHOC_RENAMED" for ev in audit)


def test_adhoc_rename_reached_node_returns_422() -> None:
    sid = _released_two_step("AdhocUmbenennenFehler")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    a_id = client.get(f"/instances/{iid}/worklist").json()["ready_activities"][0]

    resp = client.post(
        f"/instances/{iid}/adhoc/rename",
        json={"node_id": a_id, "label": "Zu spaet"},
    )
    assert resp.status_code == 422
    assert "R1" in {f["rule"] for f in resp.json()["detail"]["findings"]}


def test_revision_via_api_bumps_version() -> None:
    sid = _released_two_step("Revision")
    resp = client.post(f"/schemas/{sid}/revision", json={})
    assert resp.status_code == 200
    revision = resp.json()
    assert revision["id"] != sid
    assert revision["version"] == 2
    assert revision["lifecycle_state"] == "ENTWURF"


def test_migration_check_and_migrate_via_api() -> None:
    sid = _released_two_step("MigrationQuelle")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    wl = client.get(f"/instances/{iid}/worklist").json()
    a_id = wl["ready_activities"][0]
    client.post(f"/instances/{iid}/complete", json={"node_id": a_id})

    # Build a released revision that adds a step ahead of the front.
    revision = client.post(f"/schemas/{sid}/revision", json={}).json()
    rid = revision["id"]
    b_id = next(
        n["id"]
        for n in revision["nodes"].values()
        if n["type"] == "ACTIVITY" and n["label"] == "B"
    )
    client.post(f"/schemas/{rid}/serial-insert", json={"label": "C", "after_node_id": b_id})
    client.post(f"/schemas/{rid}/release")

    check = client.post(
        f"/instances/{iid}/migration-check", json={"target_schema_id": rid}
    )
    assert check.status_code == 200
    assert check.json()["migratable"] is True

    resp = client.post(f"/instances/{iid}/migrate", json={"target_schema_id": rid})
    assert resp.status_code == 200
    migrated = resp.json()
    assert migrated["schema_id"] == rid
    assert migrated["schema_version"] == 2


def test_migration_rewiring_returns_422() -> None:
    sid = _released_two_step("MigrationBlock")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    wl = client.get(f"/instances/{iid}/worklist").json()
    a_id = wl["ready_activities"][0]
    client.post(f"/instances/{iid}/complete", json={"node_id": a_id})

    revision = client.post(f"/schemas/{sid}/revision", json={}).json()
    rid = revision["id"]
    # Insert after the already-completed A -> rewires a completed node (M3).
    client.post(f"/schemas/{rid}/serial-insert", json={"label": "C", "after_node_id": a_id})
    client.post(f"/schemas/{rid}/release")

    resp = client.post(f"/instances/{iid}/migrate", json={"target_schema_id": rid})
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "M3" in rules


def test_activity_template_binding_via_api() -> None:
    sid = client.post("/schemas", json={"name": "RepoApi"}).json()["id"]
    node = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    ).json()
    act_id = next(n["id"] for n in node["nodes"].values() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "betrag", "data_type": "FLOAT", "element_id": "betrag"},
    )
    resp = client.post(
        f"/schemas/{sid}/activity-templates",
        json={
            "name": "Pruefen",
            "executor": "SERVICE",
            "inputs": [{"name": "wert", "data_type": "FLOAT"}],
            "template_id": "t1",
        },
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/schemas/{sid}/service",
        json={
            "node_id": act_id,
            "name": "Pruefen",
            "template_id": "t1",
            "parameter_mapping": {"wert": "betrag"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["service_bindings"][act_id]["automatic"] is True
    assert client.get(f"/schemas/{sid}/validation").json()["correct"] is True


def test_activity_template_type_mismatch_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "RepoApiBad"}).json()["id"]
    node = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    ).json()
    act_id = next(n["id"] for n in node["nodes"].values() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "name", "data_type": "STRING", "element_id": "name"},
    )
    client.post(
        f"/schemas/{sid}/activity-templates",
        json={
            "name": "Pruefen",
            "executor": "SERVICE",
            "inputs": [{"name": "wert", "data_type": "FLOAT"}],
            "template_id": "t1",
        },
    )
    resp = client.post(
        f"/schemas/{sid}/service",
        json={
            "node_id": act_id,
            "name": "Pruefen",
            "template_id": "t1",
            "parameter_mapping": {"wert": "name"},
        },
    )
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "A3" in rules


def test_external_data_binding_via_api() -> None:
    sid = client.post("/schemas", json={"name": "ConnApi"}).json()["id"]
    client.post(
        f"/schemas/{sid}/connectors",
        json={"name": "ERP", "kind": "MS_SQL", "connector_id": "erp"},
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "kunden_nr", "data_type": "STRING", "element_id": "key"},
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "kunde", "data_type": "STRING", "element_id": "kunde"},
    )
    resp = client.post(
        f"/schemas/{sid}/data-elements/kunde/external",
        json={"connector_id": "erp", "entity": "Kunde", "key_element_id": "key"},
    )
    assert resp.status_code == 200
    assert resp.json()["data_elements"]["kunde"]["source"] == "EXTERNAL"
    assert client.get(f"/schemas/{sid}/validation").json()["correct"] is True


def test_external_data_unknown_connector_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "ConnApiBad"}).json()["id"]
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "kunden_nr", "data_type": "STRING", "element_id": "key"},
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "kunde", "data_type": "STRING", "element_id": "kunde"},
    )
    resp = client.post(
        f"/schemas/{sid}/data-elements/kunde/external",
        json={"connector_id": "nope", "entity": "Kunde", "key_element_id": "key"},
    )
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "C1" in rules


def test_bpmn_export_via_api_returns_xml() -> None:
    sid = client.post("/schemas", json={"name": "BpmnExport"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "S", "after_node_id": "start"})

    resp = client.get(f"/schemas/{sid}/bpmn")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert "<bpmn:definitions" in resp.text or "definitions" in resp.text


def test_bpmn_import_round_trip_via_api() -> None:
    sid = client.post("/schemas", json={"name": "BpmnImport"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "S", "after_node_id": "start"})
    xml = client.get(f"/schemas/{sid}/bpmn").text

    resp = client.post("/bpmn-import", json={"xml": xml, "name": "Reimport"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "Reimport"


def test_bpmn_import_malformed_returns_422() -> None:
    resp = client.post("/bpmn-import", json={"xml": "<definitions>kaputt"})
    assert resp.status_code == 422
    assert "message" in resp.json()["detail"]


def test_cors_header_present_for_browser_client() -> None:
    resp = client.get("/health", headers={"Origin": "http://localhost:5500"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"


def _released_task_schema(name: str) -> tuple[str, str]:
    """Released schema with one activity bound to role 'sb' (agent a1)."""
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
    client.post(f"/schemas/{sid}/release")
    return sid, act_id


def test_agent_tasks_via_api() -> None:
    sid, act_id = _released_task_schema("AufgabenApi")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    resp = client.get("/agents/a1/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert any(t["instance_id"] == iid and t["node_id"] == act_id for t in tasks)
    # Each task reports the schema revision (version) it belongs to so the
    # worklist can show it next to the process name.
    task = next(t for t in tasks if t["instance_id"] == iid and t["node_id"] == act_id)
    assert task["schema_version"] == 1

    resp_inst = client.get(f"/instances/{iid}/tasks")
    assert resp_inst.status_code == 200
    assert any(t["node_id"] == act_id for t in resp_inst.json())


def test_complete_with_ineligible_agent_returns_409() -> None:
    sid, act_id = _released_task_schema("AufgabenApiBlock")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "agent_id": "ghost"},
    )
    assert resp.status_code == 409
    assert "message" in resp.json()["detail"]


def test_complete_with_eligible_agent_records_performer() -> None:
    sid, act_id = _released_task_schema("AufgabenApiOk")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act_id, "agent_id": "a1"},
    )
    assert resp.status_code == 200
    assert resp.json()["performed_by"][act_id] == "a1"


def test_set_org_unit_manager_via_api() -> None:
    sid = client.post("/schemas", json={"name": "ManagerApi"}).json()["id"]
    client.post(f"/schemas/{sid}/org-units", json={"name": "Einkauf", "org_unit_id": "ek"})
    client.post(f"/schemas/{sid}/agents", json={"name": "Chef", "agent_id": "a1"})
    resp = client.post(
        f"/schemas/{sid}/org-units/ek/manager", json={"manager_id": "a1"}
    )
    assert resp.status_code == 200
    assert resp.json()["org_model"]["org_units"]["ek"]["manager_id"] == "a1"


def test_set_org_unit_manager_unknown_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "ManagerApiBad"}).json()["id"]
    client.post(f"/schemas/{sid}/org-units", json={"name": "Einkauf", "org_unit_id": "ek"})
    resp = client.post(
        f"/schemas/{sid}/org-units/ek/manager", json={"manager_id": "ghost"}
    )
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "Z1" in rules


def test_set_org_unit_parent_via_api() -> None:
    sid = client.post("/schemas", json={"name": "ParentApi"}).json()["id"]
    client.post(f"/schemas/{sid}/org-units", json={"name": "Bereich", "org_unit_id": "br"})
    client.post(f"/schemas/{sid}/org-units", json={"name": "Team", "org_unit_id": "tm"})
    resp = client.post(f"/schemas/{sid}/org-units/tm/parent", json={"parent_id": "br"})
    assert resp.status_code == 200
    assert resp.json()["org_model"]["org_units"]["tm"]["parent_id"] == "br"


def test_set_org_unit_parent_cycle_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "ParentApiBad"}).json()["id"]
    client.post(f"/schemas/{sid}/org-units", json={"name": "Bereich", "org_unit_id": "br"})
    client.post(
        f"/schemas/{sid}/org-units",
        json={"name": "Team", "parent_id": "br", "org_unit_id": "tm"},
    )
    resp = client.post(f"/schemas/{sid}/org-units/br/parent", json={"parent_id": "tm"})
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "OP" in rules


def test_set_agent_deputy_via_api() -> None:
    sid = client.post("/schemas", json={"name": "DeputyApi"}).json()["id"]
    client.post(f"/schemas/{sid}/agents", json={"name": "Erika", "agent_id": "a1"})
    client.post(f"/schemas/{sid}/agents", json={"name": "Vertreter", "agent_id": "a2"})
    resp = client.post(f"/schemas/{sid}/agents/a1/deputy", json={"deputy_id": "a2"})
    assert resp.status_code == 200
    assert resp.json()["org_model"]["agents"]["a1"]["deputy_id"] == "a2"


def test_set_agent_self_deputy_returns_422() -> None:
    sid = client.post("/schemas", json={"name": "DeputyApiBad"}).json()["id"]
    client.post(f"/schemas/{sid}/agents", json={"name": "Erika", "agent_id": "a1"})
    resp = client.post(f"/schemas/{sid}/agents/a1/deputy", json={"deputy_id": "a1"})
    assert resp.status_code == 422
    rules = {f["rule"] for f in resp.json()["detail"]["findings"]}
    assert "Z1" in rules


def test_instance_audit_records_lifecycle_events() -> None:
    sid = _released_two_step("AuditLauf")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]

    # Drive the instance to completion.
    while True:
        wl = client.get(f"/instances/{iid}/worklist").json()
        if wl["state"] == "COMPLETED":
            break
        node_id = wl["ready_activities"][0]
        client.post(f"/instances/{iid}/complete", json={"node_id": node_id})

    resp = client.get(f"/instances/{iid}/audit")
    assert resp.status_code == 200
    events = resp.json()
    types = [e["event_type"] for e in events]
    assert types[0] == "INSTANCE_CREATED"
    assert types[-1] == "INSTANCE_COMPLETED"
    assert "ACTIVITY_COMPLETED" in types
    # Events are scoped to the instance and chronologically ordered.
    assert all(e["instance_id"] == iid for e in events)
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


def test_instance_audit_unknown_instance_returns_404() -> None:
    resp = client.get("/instances/nope/audit")
    assert resp.status_code == 404


def test_complete_with_agent_records_performer_in_audit() -> None:
    sid, act_id = _released_task_schema("AuditBearbeiter")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    client.post(f"/instances/{iid}/complete", json={"node_id": act_id, "agent_id": "a1"})

    events = client.get(f"/instances/{iid}/audit").json()
    completed = [e for e in events if e["event_type"] == "ACTIVITY_COMPLETED"]
    assert any(e["node_id"] == act_id and e["agent_id"] == "a1" for e in completed)


def test_monitoring_kpis_via_api() -> None:
    sid = _released_two_step("KpiLauf")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    while True:
        wl = client.get(f"/instances/{iid}/worklist").json()
        if wl["state"] == "COMPLETED":
            break
        client.post(f"/instances/{iid}/complete", json={"node_id": wl["ready_activities"][0]})

    resp = client.get("/monitoring/kpis", params={"schema_id": sid})
    assert resp.status_code == 200
    report = resp.json()
    assert report["schema_id"] == sid
    assert report["total_instances"] >= 1
    assert report["completed"] >= 1
    assert any(s["completed"] >= 1 for s in report["activity_stats"])


def test_monitoring_process_map_via_api() -> None:
    sid = _released_two_step("MapLauf")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    while True:
        wl = client.get(f"/instances/{iid}/worklist").json()
        if wl["state"] == "COMPLETED":
            break
        client.post(f"/instances/{iid}/complete", json={"node_id": wl["ready_activities"][0]})

    resp = client.get("/monitoring/process-map", params={"schema_id": sid})
    assert resp.status_code == 200
    pmap = resp.json()
    assert pmap["schema_id"] == sid
    # A -> B was completed in order, so a directly-follows edge must exist.
    assert any(e["frequency"] >= 1 for e in pmap["edges"])


def test_update_and_delete_data_element_via_api() -> None:
    sid = client.post("/schemas", json={"name": "DatenEdit"}).json()["id"]
    resp = client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "betrag", "data_type": "INTEGER", "element_id": "betrag"},
    )
    assert resp.status_code == 200

    # PATCH renames and retypes.
    resp = client.patch(
        f"/schemas/{sid}/data-elements/betrag",
        json={"name": "Betrag", "data_type": "FLOAT"},
    )
    assert resp.status_code == 200
    element = resp.json()["data_elements"]["betrag"]
    assert element["name"] == "Betrag"
    assert element["data_type"] == "FLOAT"

    # DELETE removes it entirely.
    resp = client.delete(f"/schemas/{sid}/data-elements/betrag")
    assert resp.status_code == 200
    assert "betrag" not in resp.json()["data_elements"]


def test_reset_data_element_source_via_api() -> None:
    sid = client.post("/schemas", json={"name": "QuelleReset"}).json()["id"]
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "kunden_nr", "data_type": "INTEGER", "element_id": "kunden_nr"},
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "kunde", "data_type": "STRING", "element_id": "kunde"},
    )
    client.post(
        f"/schemas/{sid}/connectors",
        json={"name": "ERP", "kind": "MS_SQL", "connector_id": "erp"},
    )
    resp = client.post(
        f"/schemas/{sid}/data-elements/kunde/external",
        json={"connector_id": "erp", "entity": "kunden", "key_element_id": "kunden_nr"},
    )
    assert resp.status_code == 200
    assert resp.json()["data_elements"]["kunde"]["source"] == "EXTERNAL"

    resp = client.post(f"/schemas/{sid}/data-elements/kunde/reset-source")
    assert resp.status_code == 200
    element = resp.json()["data_elements"]["kunde"]
    assert element["source"] == "INSTANCE"
    assert element["external"] is None


def test_delete_data_element_rejected_when_discriminator_via_api() -> None:
    sid = client.post("/schemas", json={"name": "DiscDel"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    )
    erfassen = next(
        nid
        for nid, n in client.get(f"/schemas/{sid}").json()["nodes"].items()
        if n.get("label") == "Erfassen"
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "flag", "data_type": "INTEGER", "element_id": "flag"},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": erfassen, "element_id": "flag", "mode": "WRITE"},
    )
    client.post(
        f"/schemas/{sid}/conditional-insert",
        json={
            "after_node_id": erfassen,
            "discriminator": "flag",
            "branches": [{"label": "A", "upper": 1}, {"label": "B"}],
        },
    )
    # 'flag' drives the XOR partition -> deletion is rejected (K7), 422.
    resp = client.delete(f"/schemas/{sid}/data-elements/flag")
    assert resp.status_code == 422


def test_delete_data_access_via_api() -> None:
    sid = client.post("/schemas", json={"name": "UnbindData"}).json()["id"]
    writer = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    ).json()
    writer_id = next(n["id"] for n in writer["nodes"].values() if n["label"] == "Erfassen")
    reader = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Pruefen", "after_node_id": writer_id},
    ).json()
    reader_id = next(n["id"] for n in reader["nodes"].values() if n["label"] == "Pruefen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "x", "data_type": "INTEGER", "element_id": "x"},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": writer_id, "element_id": "x", "mode": "WRITE"},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": reader_id, "element_id": "x", "mode": "READ", "mandatory": True},
    )

    # Removing the writer behind the mandatory read is rejected (D1), 422.
    resp = client.delete(f"/schemas/{sid}/data-access/{writer_id}/x")
    assert resp.status_code == 422
    assert "D1" in {f["rule"] for f in resp.json()["detail"]["findings"]}

    # Removing the read first is fine; then the write can be removed too.
    resp = client.delete(f"/schemas/{sid}/data-access/{reader_id}/x")
    assert resp.status_code == 200
    resp = client.delete(f"/schemas/{sid}/data-access/{writer_id}/x")
    assert resp.status_code == 200
    schema = client.get(f"/schemas/{sid}").json()
    assert schema["data_accesses"] == []


def test_delete_staff_rule_via_api() -> None:
    sid = client.post("/schemas", json={"name": "UnbindBZR"}).json()["id"]
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

    resp = client.delete(f"/schemas/{sid}/staff-rule/{act_id}")
    assert resp.status_code == 200
    assert act_id not in resp.json()["staff_rules"]

    # Removing a rule that no longer exists is rejected (OP), 422.
    resp = client.delete(f"/schemas/{sid}/staff-rule/{act_id}")
    assert resp.status_code == 422












def test_delete_service_via_api() -> None:
    sid = client.post("/schemas", json={"name": "UnbindService"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    ).json()
    act_id = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/service",
        json={"node_id": act_id, "name": "Formular", "automatic": False},
    )

    resp = client.delete(f"/schemas/{sid}/service/{act_id}")
    assert resp.status_code == 200
    assert act_id not in resp.json()["service_bindings"]

    # Removing a service that no longer exists is rejected (OP), 422.
    resp = client.delete(f"/schemas/{sid}/service/{act_id}")
    assert resp.status_code == 422
