# SPDX-License-Identifier: BUSL-1.1
"""Runtime + API tests for the structured scalar SQL select (roadmap Q1/Q2).

Covers the layers that need a real database (SQLite, file-backed):

* :meth:`SqlAlchemyConnector.select_scalar` -- typed single-value resolution for
  every cardinality guarantee and operator, plus column introspection;
* :meth:`DataAccessLayer.read_scalar` and the external-task **Pre-Fetch** of a
  scalar value into a worker's input package;
* the :func:`bind_sql_select` operation and its API endpoint, the
  ``/v1/connectors/{id}/columns`` introspection endpoint, and the BPMN
  round-trip of a scalar select binding.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from procworks import (
    AggregateKind,
    Cardinality,
    ConnectionConfig,
    ConnectorKind,
    DataAccessError,
    DataAccessLayer,
    DataSourceKind,
    DataType,
    FilterOperator,
    OrderBy,
    ProcessInstance,
    ProcessSchema,
    QueryFilter,
    SqlAlchemyConnector,
    SqlSelectBinding,
    SqlWriteBinding,
    add_data_element,
    assign_service,
    bind_sql_select,
    bind_sql_write,
    complete_activity,
    connect_data,
    create_empty_schema,
    export_bpmn,
    import_bpmn,
    instantiate,
    register_connector,
    release,
    serial_insert,
    set_automation,
)
from procworks.api import _connections, _store, app
from procworks.execution import ExecutionContext
from procworks.integration_runtime import ExternalTaskRuntime
from procworks.model import AccessMode, AutomationKind
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)
from procworks.validator import CorrectnessError


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'erp.db'}"


def _seed(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE Kunde "
                "(kd_id INTEGER PRIMARY KEY, name TEXT, ort TEXT, umsatz REAL, status TEXT)"
            )
        )
        for kd_id, name, ort, umsatz in [
            (1, "Acme", "Bonn", 100.0),
            (2, "Beta", "Bonn", 250.0),
            (3, "Gamma", "Koeln", 50.0),
        ]:
            conn.execute(
                text("INSERT INTO Kunde (kd_id, name, ort, umsatz) VALUES (:i,:n,:o,:u)"),
                {"i": kd_id, "n": name, "o": ort, "u": umsatz},
            )
    engine.dispose()


def _activity(schema, label):  # type: ignore[no-untyped-def]
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _key_unique_binding() -> SqlSelectBinding:
    return SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="name",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        cardinality=Cardinality.KEY_UNIQUE,
        unique_column="kd_id",
    )


# --- SqlAlchemyConnector.select_scalar -----------------------------------


def test_select_scalar_key_unique(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    assert connector.select_scalar(_key_unique_binding(), {"kunden_nr": 1}) == "Acme"


def test_select_scalar_returns_none_without_match(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    assert connector.select_scalar(_key_unique_binding(), {"kunden_nr": 99}) is None


def test_select_scalar_count_aggregate(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    binding = SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="kd_id",
        column_type=DataType.INTEGER,
        aggregate=AggregateKind.COUNT,
        filters=[
            QueryFilter(
                column="ort",
                column_type=DataType.STRING,
                operator=FilterOperator.EQ,
                key_element_id="stadt",
            )
        ],
        cardinality=Cardinality.AGGREGATE,
    )
    assert connector.select_scalar(binding, {"stadt": "Bonn"}) == 2


def test_select_scalar_avg_aggregate(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    binding = SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="umsatz",
        column_type=DataType.FLOAT,
        aggregate=AggregateKind.AVG,
        filters=[
            QueryFilter(
                column="ort",
                column_type=DataType.STRING,
                operator=FilterOperator.EQ,
                key_element_id="stadt",
            )
        ],
        cardinality=Cardinality.AGGREGATE,
    )
    assert connector.select_scalar(binding, {"stadt": "Bonn"}) == 175.0


def test_select_scalar_first_ordered(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    binding = SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="name",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="ort",
                column_type=DataType.STRING,
                operator=FilterOperator.EQ,
                key_element_id="stadt",
            )
        ],
        cardinality=Cardinality.FIRST_ORDERED,
        order_by=[OrderBy(column="umsatz", descending=True)],
    )
    assert connector.select_scalar(binding, {"stadt": "Bonn"}) == "Beta"


def test_select_scalar_in_operator(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    binding = SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="name",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.IN,
                key_element_id="ids",
            )
        ],
        cardinality=Cardinality.FIRST_ORDERED,
        order_by=[OrderBy(column="kd_id")],
    )
    assert connector.select_scalar(binding, {"ids": [3, 2]}) == "Beta"


def test_select_scalar_like_operator(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    binding = SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="name",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="name",
                column_type=DataType.STRING,
                operator=FilterOperator.LIKE,
                key_element_id="pattern",
            )
        ],
        cardinality=Cardinality.FIRST_ORDERED,
        order_by=[OrderBy(column="kd_id")],
    )
    assert connector.select_scalar(binding, {"pattern": "A%"}) == "Acme"


# --- column introspection ------------------------------------------------


def test_columns_reflect_and_map_types(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    mapped = {c["column"]: c["data_type"] for c in connector.columns("Kunde")}
    assert mapped["kd_id"] is DataType.INTEGER
    assert mapped["name"] is DataType.STRING
    assert mapped["ort"] is DataType.STRING
    assert mapped["umsatz"] is DataType.FLOAT


def test_columns_unknown_entity_raises(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    with pytest.raises(DataAccessError):
        connector.columns("Nonexistent")


# --- DataAccessLayer.read_scalar -----------------------------------------


def _bound_schema(schema_id: str = "sqlq") -> ProcessSchema:
    schema = create_empty_schema("SQL", schema_id=schema_id)
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "kundenname", DataType.STRING, element_id="kundenname")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = _activity(schema, "Erfassen")
    schema = serial_insert(schema, "Pruefen", after_node_id=writer)
    reader = _activity(schema, "Pruefen")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    schema = bind_sql_select(
        schema,
        "kundenname",
        connector_id="erp",
        entity="Kunde",
        column="name",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        cardinality=Cardinality.KEY_UNIQUE,
        unique_column="kd_id",
    )
    schema = connect_data(schema, reader, "kundenname", AccessMode.READ, mandatory=False)
    return schema


def test_read_scalar_end_to_end(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    schema = _bound_schema()
    dal = DataAccessLayer()
    dal.register("erp", SqlAlchemyConnector(create_engine(url)))
    assert dal.read_scalar(schema, {"kunden_nr": 1}, "kundenname") == "Acme"


def test_read_scalar_missing_key_raises(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    schema = _bound_schema()
    dal = DataAccessLayer()
    dal.register("erp", SqlAlchemyConnector(create_engine(url)))
    with pytest.raises(DataAccessError):
        dal.read_scalar(schema, {}, "kundenname")


# --- bind_sql_select operation -------------------------------------------


def test_bind_sql_select_produces_valid_schema() -> None:
    schema = _bound_schema("bindok")
    element = schema.data_elements["kundenname"]
    assert element.source is DataSourceKind.EXTERNAL
    assert element.select is not None
    assert element.external is None
    assert element.select.column == "name"


def test_bind_sql_select_rejects_type_mismatch() -> None:
    schema = create_empty_schema("SQL", schema_id="bindbad")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "kundenname", DataType.STRING, element_id="kundenname")
    with pytest.raises(CorrectnessError) as exc:
        bind_sql_select(
            schema,
            "kundenname",
            connector_id="erp",
            entity="Kunde",
            column="kd_id",
            column_type=DataType.INTEGER,  # STRING element -> C4 mismatch
            cardinality=Cardinality.AGGREGATE,
            aggregate=AggregateKind.MAX,
        )
    assert any(f.rule == "C4" for f in exc.value.findings)


# --- Pre-Fetch of a scalar value into an external task --------------------


def _external_scalar_schema() -> tuple[ProcessSchema, str]:
    schema = create_empty_schema("Conn", schema_id="cscalar")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = _activity(schema, "Erfassen")
    schema = serial_insert(schema, "Bearbeiten", after_node_id=writer)
    worker_node = _activity(schema, "Bearbeiten")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "Kundennr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "Kundenname", DataType.STRING, element_id="kundenname")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    schema = bind_sql_select(
        schema,
        "kundenname",
        connector_id="erp",
        entity="Kunde",
        column="name",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        cardinality=Cardinality.KEY_UNIQUE,
        unique_column="kd_id",
    )
    schema = assign_service(schema, worker_node, "Bearbeiten", automatic=True)
    schema = set_automation(schema, worker_node, AutomationKind.EXTERNAL_TASK, topic="erp")
    schema = connect_data(schema, worker_node, "kundenname", AccessMode.READ, mandatory=False)
    schema = release(schema)
    return schema, writer


def test_prefetch_scalar_select_into_input(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    schema, writer = _external_scalar_schema()
    schemas.put(schema)
    context = ExecutionContext(make_resolver(schemas), instances)
    instance = instantiate(schema, context=context)
    instances.put(instance)
    instance = complete_activity(instance, schema, writer, {"kunden_nr": 1}, context=context)
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
    task = runtime.fetch_and_lock("w1", ["erp"], lock_ms=10_000)[0]
    assert task.input_variables["kundenname"] == "Acme"


# --- BPMN round-trip -----------------------------------------------------


def test_bpmn_roundtrip_preserves_sql_select() -> None:
    schema = _bound_schema("rt")
    restored = import_bpmn(export_bpmn(schema), schema_id="rt2")
    element = restored.data_elements["kundenname"]
    assert element.source is DataSourceKind.EXTERNAL
    assert element.select is not None
    assert element.select.column == "name"
    assert element.select.unique_column == "kd_id"
    assert element.select.filters[0].key_element_id == "kunden_nr"
    assert restored.connectors["erp"].kind is ConnectorKind.MS_SQL


# --- API endpoints -------------------------------------------------------


def test_api_bind_sql_select_endpoint() -> None:
    schema = create_empty_schema("SQLAPI", schema_id="sqlq_api")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "kundenname", DataType.STRING, element_id="kundenname")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = _activity(schema, "Erfassen")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    _store.put(schema)

    client = TestClient(app)
    response = client.post(
        "/schemas/sqlq_api/data-elements/kundenname/sql-select",
        json={
            "connector_id": "erp",
            "entity": "Kunde",
            "column": "name",
            "column_type": "STRING",
            "filters": [
                {
                    "column": "kd_id",
                    "column_type": "INTEGER",
                    "operator": "EQ",
                    "key_element_id": "kunden_nr",
                }
            ],
            "cardinality": "KEY_UNIQUE",
            "unique_column": "kd_id",
        },
    )
    assert response.status_code == 200
    element = response.json()["data_elements"]["kundenname"]
    assert element["source"] == "EXTERNAL"
    assert element["select"]["column"] == "name"


def test_api_bind_sql_select_rejects_mismatch() -> None:
    schema = create_empty_schema("SQLAPI", schema_id="sqlq_api_bad")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kundenname", DataType.STRING, element_id="kundenname")
    _store.put(schema)

    client = TestClient(app)
    response = client.post(
        "/schemas/sqlq_api_bad/data-elements/kundenname/sql-select",
        json={
            "connector_id": "erp",
            "entity": "Kunde",
            "column": "kd_id",
            "column_type": "INTEGER",
            "cardinality": "AGGREGATE",
            "aggregate": "MAX",
        },
    )
    assert response.status_code == 422


def test_api_connector_columns_endpoint(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    _connections.register(
        ConnectionConfig(connector_id="erp_cols", kind=ConnectorKind.MS_SQL, url=url)
    )
    try:
        client = TestClient(app)
        response = client.get(
            "/v1/connectors/erp_cols/columns", params={"entity": "Kunde"}
        )
        assert response.status_code == 200
        mapped = {c["column"]: c["data_type"] for c in response.json()}
        assert mapped["name"] == "STRING"
        assert mapped["kd_id"] == "INTEGER"
        assert mapped["umsatz"] == "FLOAT"
    finally:
        _connections._configs.pop("erp_cols", None)
        _connections._cache.pop("erp_cols", None)


def test_api_connector_columns_unknown_connector() -> None:
    client = TestClient(app)
    response = client.get("/v1/connectors/ghost/columns", params={"entity": "Kunde"})
    assert response.status_code == 404


# --- scalar write-back (Q4) -----------------------------------------------


def _status_of(url: str, kd_id: int) -> object:
    engine = create_engine(url)
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT status FROM Kunde WHERE kd_id = :i"), {"i": kd_id}
        ).scalar()
    engine.dispose()
    return value


def _write_binding() -> SqlWriteBinding:
    return SqlWriteBinding(
        connector_id="erp",
        entity="Kunde",
        column="status",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        unique_column="kd_id",
    )


def test_update_scalar_writes_exactly_one_row(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    connector = SqlAlchemyConnector(create_engine(url))
    affected = connector.update_scalar(_write_binding(), "geprueft", {"kunden_nr": 1})
    assert affected == 1
    assert _status_of(url, 1) == "geprueft"
    assert _status_of(url, 2) is None


def _write_bound_schema(schema_id: str = "sqlw") -> ProcessSchema:
    schema = create_empty_schema("SQLW", schema_id=schema_id)
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "status_extern", DataType.STRING, element_id="status_extern")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = _activity(schema, "Erfassen")
    schema = serial_insert(schema, "Melden", after_node_id=writer)
    reporter = _activity(schema, "Melden")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    schema = bind_sql_write(
        schema,
        "status_extern",
        connector_id="erp",
        entity="Kunde",
        column="status",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        unique_column="kd_id",
    )
    schema = connect_data(schema, reporter, "status_extern", AccessMode.WRITE, mandatory=False)
    return schema


def test_write_scalar_end_to_end(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    schema = _write_bound_schema()
    dal = DataAccessLayer()
    dal.register("erp", SqlAlchemyConnector(create_engine(url)))
    affected = dal.write_scalar(schema, {"kunden_nr": 2}, "status_extern", "offen")
    assert affected == 1
    assert _status_of(url, 2) == "offen"


def _external_write_schema() -> tuple[ProcessSchema, str]:
    schema = create_empty_schema("ConnW", schema_id="cwrite")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = _activity(schema, "Erfassen")
    schema = serial_insert(schema, "Bearbeiten", after_node_id=writer)
    worker_node = _activity(schema, "Bearbeiten")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "Kundennr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "Status", DataType.STRING, element_id="status_extern")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    schema = bind_sql_write(
        schema,
        "status_extern",
        connector_id="erp",
        entity="Kunde",
        column="status",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        unique_column="kd_id",
    )
    schema = assign_service(schema, worker_node, "Bearbeiten", automatic=True)
    schema = set_automation(schema, worker_node, AutomationKind.EXTERNAL_TASK, topic="erpw")
    schema = connect_data(schema, worker_node, "status_extern", AccessMode.WRITE, mandatory=False)
    schema = release(schema)
    return schema, writer


def test_postflush_scalar_write_on_complete(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    _seed(url)
    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    schema, writer = _external_write_schema()
    schemas.put(schema)
    context = ExecutionContext(make_resolver(schemas), instances)
    instance = instantiate(schema, context=context)
    instances.put(instance)
    instance = complete_activity(instance, schema, writer, {"kunden_nr": 3}, context=context)
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
    task = runtime.fetch_and_lock("w1", ["erpw"], lock_ms=10_000)[0]
    runtime.complete(task.id, "w1", {"status_extern": "fertig"})
    assert _status_of(url, 3) == "fertig"


def test_api_bind_sql_write_endpoint() -> None:
    schema = create_empty_schema("SQLAPIW", schema_id="sqlw_api")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "status_extern", DataType.STRING, element_id="status_extern")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = _activity(schema, "Erfassen")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    _store.put(schema)

    client = TestClient(app)
    response = client.post(
        "/schemas/sqlw_api/data-elements/status_extern/sql-write",
        json={
            "connector_id": "erp",
            "entity": "Kunde",
            "column": "status",
            "column_type": "STRING",
            "filters": [
                {
                    "column": "kd_id",
                    "column_type": "INTEGER",
                    "operator": "EQ",
                    "key_element_id": "kunden_nr",
                }
            ],
            "unique_column": "kd_id",
        },
    )
    assert response.status_code == 200
    element = response.json()["data_elements"]["status_extern"]
    assert element["source"] == "EXTERNAL"
    assert element["write"]["column"] == "status"


def test_api_bind_sql_write_rejects_missing_unique_column() -> None:
    schema = create_empty_schema("SQLAPIW", schema_id="sqlw_api_bad")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "status_extern", DataType.STRING, element_id="status_extern")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = _activity(schema, "Erfassen")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    _store.put(schema)

    client = TestClient(app)
    response = client.post(
        "/schemas/sqlw_api_bad/data-elements/status_extern/sql-write",
        json={
            "connector_id": "erp",
            "entity": "Kunde",
            "column": "status",
            "column_type": "STRING",
            "filters": [],
            "unique_column": "",
        },
    )
    assert response.status_code == 422


def test_bpmn_roundtrip_preserves_sql_write() -> None:
    schema = _write_bound_schema("rtw")
    restored = import_bpmn(export_bpmn(schema), schema_id="rtw2")
    element = restored.data_elements["status_extern"]
    assert element.source is DataSourceKind.EXTERNAL
    assert element.write is not None
    assert element.write.column == "status"
    assert element.write.unique_column == "kd_id"
    assert element.write.filters[0].key_element_id == "kunden_nr"
    assert restored.connectors["erp"].kind is ConnectorKind.MS_SQL
