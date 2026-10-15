# SPDX-License-Identifier: BUSL-1.1
"""The schema selection loads with one request, however many schemas exist.

``GET /schemas`` lists only ids, so the web client asked for every schema on
its own after each login (700 schemas: 704 requests, 4.4 s to the first
view). ``GET /schema-summaries`` returns id, name, version, lifecycle state
and lineage (``revision_of``) of every schema in one answer -- read from the
columns the store keeps anyway, not from the full documents -- with the same
rights as ``GET /schemas``. Additive: the id list stays as it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from staffing import staff_via_api
from web_vm import needs_node, run_app_js

import procworks.api as api_module
from procworks.api import app
from procworks.store import InMemorySchemaStore

client = TestClient(app)


def _family() -> tuple[str, str]:
    """A released v1 and its draft v2 over the API: ``(v1, v2)``."""

    v1 = client.post("/schemas", json={"name": "Übersicht"}).json()["id"]
    client.post(f"/schemas/{v1}/serial-insert", json={"label": "A", "after_node_id": "start"})
    staff_via_api(client, v1)
    assert client.post(f"/schemas/{v1}/release").status_code == 200
    v2 = client.post(f"/schemas/{v1}/revision", json={}).json()["id"]
    return v1, v2


def _check(summaries: list[dict[str, object]], v1: str, v2: str) -> None:
    by_id = {s["id"]: s for s in summaries}
    assert by_id[v1] == {"id": v1, "name": "Übersicht", "version": 1,
                         "lifecycle_state": "RELEASED", "revision_of": None}
    assert by_id[v2] == {"id": v2, "name": "Übersicht", "version": 2,
                         "lifecycle_state": "ENTWURF", "revision_of": v1}


def test_in_memory_every_schema_comes_with_name_version_state_and_lineage() -> None:
    v1, v2 = _family()

    resp = client.get("/schema-summaries")

    assert resp.status_code == 200
    _check(resp.json(), v1, v2)
    assert [s["id"] for s in resp.json()] == client.get("/schemas").json()


def test_on_the_database_the_same(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_sql_backend(monkeypatch, tmp_path)
    v1, v2 = _family()

    resp = client.get("/schema-summaries")

    assert resp.status_code == 200
    _check(resp.json(), v1, v2)
    assert sorted(s["id"] for s in resp.json()) == sorted(client.get("/schemas").json())


def test_an_empty_store_gives_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module, "_store", InMemorySchemaStore())
    assert client.get("/schema-summaries").json() == []


def test_the_summaries_need_the_same_read_right_as_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negativ: without a login (password mode) both answer 401."""

    from procworks.auth_password import InMemoryCredentialStore, PasswordAuthBackend

    backend = PasswordAuthBackend(InMemoryCredentialStore())
    monkeypatch.setattr(api_module, "_auth_backend", backend)
    assert client.get("/schemas").status_code == 401
    assert client.get("/schema-summaries").status_code == 401


@needs_node
def test_the_client_loads_the_selection_with_one_request(tmp_path: Path) -> None:
    """700 schemas: one request, names, versions and states known, no draft
    preselected for a login without the modelling role."""

    res = run_app_js(
        r"""
state.principal = { subject: "erika", roles: ["operator"] };
const calls = [];
const rows = [];
for (let i = 0; i < 700; i++) {
  rows.push({ id: "s" + i, name: "P" + i, version: 1 + (i % 3),
              lifecycle_state: i === 0 ? "ENTWURF" : "RELEASED",
              revision_of: i % 3 ? "s" + (i - 1) : null });
}
respond((p) => {
  calls.push(p);
  return p === "/schema-summaries" ? { body: rows } : { status: 404 };
});
await loadSchemas();
return { calls, count: state.schemaIds.length, name: state.schemaNames.s5,
         version: state.schemaVersions.s5, state0: state.schemaLifecycle.s0,
         lineage: state.schemaRevisionOf.s5, chosen: state.schemaId };
""",
        tmp_path,
    )
    assert res["calls"] == ["/schema-summaries"]
    assert res["count"] == 700
    assert (res["name"], res["version"], res["state0"]) == ("P5", 3, "ENTWURF")
    assert res["lineage"] == "s4"
    assert res["chosen"] == "s1"  # not the draft s0


def _plant_out_of_order() -> list[str]:
    """Store schemas whose creation order differs from their id order.

    :returns: the ids in creation order.
    """

    from procworks import create_empty_schema

    ids = ["zz-later-created-first", "mm-middle", "aa-created-last"]
    for sid in ids:
        api_module._store.put(create_empty_schema(f"Ordnung {sid}", schema_id=sid))
    return ids


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_both_lists_come_in_the_same_order(
    backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``GET /schema-summaries`` promises the order of ``GET /schemas``.

    On SQLite (as on PostgreSQL) a plain scan of the table returns rows in
    storage order, while the id list is read from the primary key index --
    the two drifted apart without an explicit order.
    """

    if backend == "sqlite":
        use_sql_backend(monkeypatch, tmp_path)
    else:
        monkeypatch.setattr(api_module, "_store", InMemorySchemaStore())
    _plant_out_of_order()

    ids = client.get("/schemas").json()
    summaries = [s["id"] for s in client.get("/schema-summaries").json()]

    assert summaries == ids


def test_on_the_database_the_order_is_by_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A defined order, not the storage order: ascending id."""

    use_sql_backend(monkeypatch, tmp_path)
    created = _plant_out_of_order()

    summaries = [s["id"] for s in client.get("/schema-summaries").json()]

    assert summaries == sorted(created)
    assert summaries != created  # the storage order would have been this


@needs_node
def test_the_selection_is_sorted_for_people_and_the_preselection_is_stable(
    tmp_path: Path,
) -> None:
    """Name (numbers by value), then version -- whatever order the server sends."""

    res = run_app_js(
        r"""
state.principal = { subject: "erika", roles: ["operator"] };
const row = (id, name, version, lifecycle_state) =>
  ({ id, name, version, lifecycle_state, revision_of: null });
const rows = [
  row("schema_9", "Urlaubsantrag", 2, "RELEASED"),
  row("schema_12", "beschaffung", 1, "ENTWURF"),
  row("schema_3", "Urlaubsantrag", 1, "RELEASED"),
  row("schema_10", "Prozess 10", 1, "RELEASED"),
  row("schema_11", "Prozess 2", 1, "RELEASED"),
];
const orders = [];
const chosen = [];
for (const order of [rows, rows.slice().reverse(), [rows[2], rows[4], rows[0], rows[1], rows[3]]]) {
  state.schemaId = null;
  respond((p) => (p === "/schema-summaries" ? { body: order } : { status: 404 }));
  await loadSchemas();
  orders.push(state.schemaIds.slice());
  chosen.push(state.schemaId);
}
state.principal = { subject: "mara", roles: ["modeler"] };
state.schemaId = null;
await loadSchemas();
return { orders, chosen, modeler: state.schemaId };
""",
        tmp_path,
    )
    expected = ["schema_12", "schema_11", "schema_10", "schema_3", "schema_9"]
    assert res["orders"] == [expected, expected, expected]
    # Without the modelling role the first released one, never the draft.
    assert res["chosen"] == ["schema_11", "schema_11", "schema_11"]
    assert res["modeler"] == "schema_12"  # the first one, also a draft
