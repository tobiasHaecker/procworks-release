# SPDX-License-Identifier: BUSL-1.1
"""Real SQL data-connector tests (P3, concept §7).

Covers three layers:

* the :class:`procworks.dal.SqlAlchemyConnector` against a file-backed SQLite
  database -- parameterized read/write/query plus the identifier whitelist that
  closes the injection surface;
* the :class:`procworks.connections.ConnectionRegistry` / secret store --
  ``${ENV}`` resolution, lazy build, read-only ``test`` ping and ``sample_read``,
  and ``build_connection_registry`` from JSON; and
* the bidirectional Pre-Fetch / Post-Flush wiring of the external-task runtime
  against a SQLite-backed connector, plus the ``/v1/connectors`` API.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from procworks import (
    AccessMode,
    ConnectionConfig,
    ConnectionRegistry,
    ConnectorKind,
    DataAccessError,
    DataAccessLayer,
    DataType,
    ExternalTaskState,
    NodeType,
    ProcessInstance,
    ProcessSchema,
    SqlAlchemyConnector,
    add_data_element,
    assign_service,
    bind_external_data,
    build_connection_registry,
    connect_data,
    create_empty_schema,
    instantiate,
    register_connector,
    release,
    serial_insert,
    set_automation,
)
from procworks.api import _connections, app
from procworks.execution import ExecutionContext
from procworks.integration_runtime import ExternalTaskError, ExternalTaskRuntime
from procworks.model import AutomationKind
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'erp.db'}"


def _seed_customer_db(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Kunde (id TEXT PRIMARY KEY, name TEXT, ort TEXT)"))
        conn.execute(text("CREATE TABLE Ergebnis (id TEXT PRIMARY KEY, status TEXT)"))
        conn.execute(
            text("INSERT INTO Kunde (id, name, ort) VALUES (:i, :n, :o)"),
            {"i": "K1", "n": "Acme", "o": "Bonn"},
        )
    engine.dispose()


# --- SqlAlchemyConnector --------------------------------------------------


def test_sqlalchemy_connector_read(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    connector = SqlAlchemyConnector(create_engine(url))
    assert connector.read("Kunde", "K1") == {"id": "K1", "name": "Acme", "ort": "Bonn"}


def test_sqlalchemy_connector_read_missing_raises(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    connector = SqlAlchemyConnector(create_engine(url))
    with pytest.raises(DataAccessError):
        connector.read("Kunde", "nope")


def test_sqlalchemy_connector_write_inserts_then_updates(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    connector = SqlAlchemyConnector(create_engine(url))
    connector.write("Ergebnis", "K1", {"status": "ok"})
    assert connector.read("Ergebnis", "K1") == {"id": "K1", "status": "ok"}
    connector.write("Ergebnis", "K1", {"status": "done"})
    assert connector.read("Ergebnis", "K1") == {"id": "K1", "status": "done"}


def test_sqlalchemy_connector_query_filters(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    connector = SqlAlchemyConnector(create_engine(url))
    connector.write("Kunde", "K2", {"name": "Beta", "ort": "Bonn"})
    rows = connector.query("Kunde", {"ort": "Bonn"})
    assert {r["id"] for r in rows} == {"K1", "K2"}


def test_sqlalchemy_connector_custom_key_column(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Artikel (nr TEXT PRIMARY KEY, bez TEXT)"))
        conn.execute(text("INSERT INTO Artikel (nr, bez) VALUES ('A1', 'Bolt')"))
    connector = SqlAlchemyConnector(engine, key_column="nr")
    assert connector.read("Artikel", "A1") == {"nr": "A1", "bez": "Bolt"}


def test_sqlalchemy_connector_rejects_unsafe_entity(tmp_path: Path) -> None:
    connector = SqlAlchemyConnector(create_engine(_sqlite_url(tmp_path)))
    with pytest.raises(DataAccessError):
        connector.read("Kunde; DROP TABLE Kunde", "K1")


def test_sqlalchemy_connector_rejects_unsafe_column(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    connector = SqlAlchemyConnector(create_engine(url))
    with pytest.raises(DataAccessError):
        connector.query("Kunde", {"ort = 'Bonn' OR 1=1 --": "x"})


# --- ConnectionRegistry / secret store ------------------------------------


def test_registry_resolves_secret_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    monkeypatch.setenv("ERP_URL", url)
    registry = ConnectionRegistry()
    registry.register(
        ConnectionConfig(connector_id="erp", kind=ConnectorKind.MS_SQL, url="${ERP_URL}")
    )
    assert registry.connector("erp").read("Kunde", "K1")["name"] == "Acme"


def test_registry_missing_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT_SECRET", raising=False)
    registry = ConnectionRegistry()
    registry.register(ConnectionConfig(connector_id="x", url="${ABSENT_SECRET}"))
    with pytest.raises(DataAccessError):
        registry.connector("x")


def test_registry_test_and_sample_read(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    registry = ConnectionRegistry()
    registry.register(ConnectionConfig(connector_id="erp", url=url))
    registry.test("erp")  # read-only ping, no exception
    sample = registry.sample_read("erp", "Kunde", limit=1)
    assert len(sample) == 1 and sample[0]["id"] == "K1"


def test_registry_unknown_connector_raises() -> None:
    registry = ConnectionRegistry()
    with pytest.raises(DataAccessError):
        registry.connector("ghost")


def test_build_connection_registry_from_inline_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    payload = json.dumps([{"connector_id": "erp", "kind": "MS_SQL", "url": url}])
    monkeypatch.setenv("PROCWORKS_CONNECTIONS", payload)
    registry = build_connection_registry()
    assert registry.has("erp")
    assert registry.connector("erp").read("Kunde", "K1")["name"] == "Acme"


def test_build_connection_registry_empty_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROCWORKS_CONNECTIONS", raising=False)
    assert build_connection_registry().configs() == []


# --- bidirectional Pre-Fetch / Post-Flush via the runtime -----------------


def _external_schema(connector_id: str = "erp") -> tuple[ProcessSchema, str]:
    schema = create_empty_schema("Conn", schema_id="c1")
    schema = serial_insert(schema, "Bearbeiten", after_node_id="start")
    node_id = next(
        nid for nid, node in schema.nodes.items() if node.type is NodeType.ACTIVITY
    )
    schema = register_connector(
        schema, "ERP", ConnectorKind.MS_SQL, connector_id=connector_id
    )
    schema = add_data_element(schema, "Kundennr", DataType.STRING, element_id="kunden_nr")
    schema = add_data_element(schema, "Kunde", DataType.STRING, element_id="kunde")
    schema = add_data_element(schema, "Ergebnis", DataType.STRING, element_id="ergebnis")
    schema = bind_external_data(
        schema, "kunde", connector_id=connector_id, entity="Kunde", key_element_id="kunden_nr"
    )
    schema = bind_external_data(
        schema, "ergebnis", connector_id=connector_id, entity="Ergebnis",
        key_element_id="kunden_nr",
    )
    schema = assign_service(schema, node_id, "Bearbeiten", automatic=True)
    schema = set_automation(schema, node_id, AutomationKind.EXTERNAL_TASK, topic="erp")
    schema = connect_data(schema, node_id, "kunde", AccessMode.READ, mandatory=False)
    schema = connect_data(schema, node_id, "ergebnis", AccessMode.WRITE, mandatory=False)
    schema = release(schema)
    return schema, node_id


def _runtime_with_dal(
    url: str, *, set_key: bool = True
) -> tuple[ExternalTaskRuntime, str, InMemoryInstanceStore]:
    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    schema, _node_id = _external_schema()
    schemas.put(schema)
    context = ExecutionContext(make_resolver(schemas), instances)
    instance = instantiate(schema, context=context)
    if set_key:
        instance.data_values["kunden_nr"] = "K1"
    instances.put(instance)

    dal = DataAccessLayer()
    dal.register("erp", SqlAlchemyConnector(create_engine(url)))

    def schema_for(inst: ProcessInstance) -> ProcessSchema:
        resolved = schemas.get(inst.schema_id)
        assert resolved is not None
        return resolved

    runtime = ExternalTaskRuntime(
        InMemoryExternalTaskStore(), instances, schema_for, context, dal=dal
    )
    return runtime, instance.id, instances


def test_prefetch_reads_external_into_input(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    runtime, _iid, _instances = _runtime_with_dal(url)
    task = runtime.fetch_and_lock("w1", ["erp"], lock_ms=10_000)[0]
    assert task.input_variables["kunde"] == {"id": "K1", "name": "Acme", "ort": "Bonn"}


def test_postflush_writes_external_on_complete(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    runtime, _iid, _instances = _runtime_with_dal(url)
    task = runtime.fetch_and_lock("w1", ["erp"], lock_ms=10_000)[0]
    completed = runtime.complete(task.id, "w1", {"ergebnis": {"status": "ok"}})
    assert completed.state is ExternalTaskState.COMPLETED
    connector = SqlAlchemyConnector(create_engine(url))
    assert connector.read("Ergebnis", "K1") == {"id": "K1", "status": "ok"}


def test_external_write_must_be_a_record(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    runtime, _iid, _instances = _runtime_with_dal(url)
    task = runtime.fetch_and_lock("w1", ["erp"], lock_ms=10_000)[0]
    with pytest.raises(ExternalTaskError) as err:
        runtime.complete(task.id, "w1", {"ergebnis": "not-a-record"})
    assert err.value.status == 422


def test_prefetch_missing_key_is_bad_gateway(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    runtime, _iid, _instances = _runtime_with_dal(url, set_key=False)
    with pytest.raises(ExternalTaskError) as err:
        runtime.fetch_and_lock("w1", ["erp"], lock_ms=10_000)
    assert err.value.status == 502


# --- /v1/connectors API ---------------------------------------------------


client = TestClient(app)


@pytest.fixture
def registered_connector(tmp_path: Path):  # type: ignore[no-untyped-def]
    url = _sqlite_url(tmp_path)
    _seed_customer_db(url)
    _connections.register(ConnectionConfig(connector_id="api-erp", url=url))
    yield "api-erp"
    _connections._configs.pop("api-erp", None)
    _connections._cache.pop("api-erp", None)


def test_api_list_connectors(registered_connector: str) -> None:
    body = client.get("/v1/connectors").json()
    assert any(c["connector_id"] == registered_connector for c in body)


def test_api_test_connector(registered_connector: str) -> None:
    res = client.post(f"/v1/connectors/{registered_connector}/test")
    assert res.status_code == 200
    assert res.json() == {"connector_id": registered_connector, "ok": True}


def test_api_test_unknown_connector_is_404() -> None:
    assert client.post("/v1/connectors/ghost/test").status_code == 404


def test_api_sample_read(registered_connector: str) -> None:
    res = client.post(
        f"/v1/connectors/{registered_connector}/sample-read",
        json={"entity": "Kunde", "limit": 5},
    )
    assert res.status_code == 200
    assert res.json()[0]["id"] == "K1"
