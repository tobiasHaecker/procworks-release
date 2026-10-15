# SPDX-License-Identifier: BUSL-1.1
"""A scalar SQL write must hit exactly one row -- or fail without effect.

A write-back whose key matches no row used to end as a silent success: the
worker's value was stored nowhere, yet task and case counted as done. The
modelled write targets one row (its filter names a unique column), so:

* no matching row -> the write fails (``DAL.no-row``), the task stays locked
  and the worker may report a failure (retries, then an incident);
* several matching rows (the column is not unique after all) -> it fails
  (``DAL.many-rows``) and changes nothing;
* the decision never rests on a driver's row count alone: the connector counts
  the matching rows in the same transaction, so an unknown count (``-1``, which
  some drivers report) is neither taken as "no row" nor as success. A foreign
  connector that only reports ``-1`` fails closed (``DAL.row-count-unknown``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import CursorResult
from staffing import staffed

import procworks.api as api_module
from procworks import (
    ConnectorKind,
    DataAccessLayer,
    DataType,
    FilterOperator,
    QueryFilter,
    SqlAlchemyConnector,
    SqlSelectBinding,
    SqlWriteBinding,
    add_data_element,
    assign_service,
    bind_sql_write,
    complete_activity,
    connect_data,
    create_empty_schema,
    instantiate,
    register_connector,
    release,
    serial_insert,
    set_automation,
)
from procworks.dal import DataAccessError, WriteRowCountError
from procworks.execution import ExecutionContext
from procworks.integration_runtime import ExternalTaskError, ExternalTaskRuntime
from procworks.model import (
    AccessMode,
    AutomationKind,
    ExternalTaskState,
    InstanceState,
    ProcessInstance,
    ProcessSchema,
)
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)


def _url(tmp_path: Path, *, unique: bool = True) -> str:
    """SQLite table ``Kunde`` with rows 1..3; ``unique=False`` adds a second row 3."""

    url = f"sqlite:///{tmp_path / 'erp.db'}"
    engine = create_engine(url)
    key = "kd_id INTEGER PRIMARY KEY" if unique else "kd_id INTEGER"
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE Kunde ({key}, status TEXT)"))
        rows = [1, 2, 3] if unique else [1, 2, 3, 3]
        for kd_id in rows:
            conn.execute(text("INSERT INTO Kunde (kd_id) VALUES (:i)"), {"i": kd_id})
    engine.dispose()
    return url


def _statuses(url: str) -> list[object]:
    engine = create_engine(url)
    with engine.connect() as conn:
        values = [r[0] for r in conn.execute(text("SELECT status FROM Kunde ORDER BY kd_id"))]
    engine.dispose()
    return values


def _binding() -> SqlWriteBinding:
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


# --- connector: decided by counting, not by the driver's row count ---------


def test_exactly_one_row_is_written(tmp_path: Path) -> None:
    url = _url(tmp_path)
    connector = SqlAlchemyConnector(create_engine(url))

    assert connector.update_scalar(_binding(), "fertig", {"kunden_nr": 2}) == 1
    assert _statuses(url) == [None, "fertig", None]


def test_a_key_without_row_fails_and_changes_nothing(tmp_path: Path) -> None:
    url = _url(tmp_path)
    connector = SqlAlchemyConnector(create_engine(url))

    with pytest.raises(WriteRowCountError) as exc:
        connector.update_scalar(_binding(), "fertig", {"kunden_nr": 98})

    assert exc.value.code == "DAL.no-row"
    assert _statuses(url) == [None, None, None]


def test_several_matching_rows_fail_and_change_nothing(tmp_path: Path) -> None:
    url = _url(tmp_path, unique=False)
    connector = SqlAlchemyConnector(create_engine(url))

    with pytest.raises(WriteRowCountError) as exc:
        connector.update_scalar(_binding(), "fertig", {"kunden_nr": 3})

    assert exc.value.code == "DAL.many-rows"
    assert _statuses(url) == [None, None, None, None]


def test_an_unknown_driver_row_count_is_neither_success_nor_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some drivers report ``rowcount == -1``; the count decides instead."""

    monkeypatch.setattr(CursorResult, "rowcount", property(lambda self: -1))
    url = _url(tmp_path)
    connector = SqlAlchemyConnector(create_engine(url))

    assert connector.update_scalar(_binding(), "fertig", {"kunden_nr": 1}) == 1
    with pytest.raises(WriteRowCountError) as exc:
        connector.update_scalar(_binding(), "fertig", {"kunden_nr": 98})

    assert exc.value.code == "DAL.no-row"
    assert _statuses(url) == ["fertig", None, None]


def test_a_known_row_count_that_disagrees_rolls_the_write_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that appears between count and update must not slip through."""

    monkeypatch.setattr(CursorResult, "rowcount", property(lambda self: 2))
    url = _url(tmp_path)
    connector = SqlAlchemyConnector(create_engine(url))

    with pytest.raises(WriteRowCountError) as exc:
        connector.update_scalar(_binding(), "fertig", {"kunden_nr": 1})

    assert exc.value.code == "DAL.many-rows"
    assert _statuses(url) == [None, None, None]


# --- data access layer: the one-row contract for every connector ----------


class _ReportingConnector:
    """A foreign connector that reports a fixed row count for every write."""

    def __init__(self, rows: int) -> None:
        self.rows = rows

    def update_scalar(
        self, binding: SqlWriteBinding, value: object, key_values: Mapping[str, object]
    ) -> int:
        return self.rows


def _write_schema() -> ProcessSchema:
    schema = create_empty_schema("Zeilen", schema_id="rows")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "Kundennr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "Status", DataType.STRING, element_id="status_extern")
    binding = _binding()
    return bind_sql_write(
        schema,
        "status_extern",
        connector_id="erp",
        entity=binding.entity,
        column=binding.column,
        column_type=binding.column_type,
        filters=binding.filters,
        unique_column=binding.unique_column,
    )


@pytest.mark.parametrize(
    ("rows", "code"),
    [(0, "DAL.no-row"), (2, "DAL.many-rows"), (-1, "DAL.row-count-unknown")],
)
def test_the_layer_refuses_anything_but_one_row(rows: int, code: str) -> None:
    dal = DataAccessLayer()
    dal.register("erp", _ReportingConnector(rows))  # type: ignore[arg-type]

    with pytest.raises(WriteRowCountError) as exc:
        dal.write_scalar(_write_schema(), {"kunden_nr": 1}, "status_extern", "x")

    assert exc.value.code == code


def test_the_layer_accepts_exactly_one_row() -> None:
    dal = DataAccessLayer()
    dal.register("erp", _ReportingConnector(1))  # type: ignore[arg-type]

    assert dal.write_scalar(_write_schema(), {"kunden_nr": 1}, "status_extern", "x") == 1


# --- external task: the completion fails, nothing moves --------------------


def _runtime(url: str) -> tuple[ExternalTaskRuntime, InMemoryInstanceStore, str]:
    """External task "Bearbeiten" writes ``status_extern`` back to ``Kunde``."""

    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    schema = _write_schema()
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = serial_insert(schema, "Bearbeiten", after_node_id=writer)
    worker = next(n.id for n in schema.nodes.values() if n.label == "Bearbeiten")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    schema = assign_service(schema, worker, "Bearbeiten", automatic=True)
    schema = set_automation(schema, worker, AutomationKind.EXTERNAL_TASK, topic="zeilen")
    schema = connect_data(schema, worker, "status_extern", AccessMode.WRITE, mandatory=False)
    schema = release(staffed(schema))
    schemas.put(schema)
    context = ExecutionContext(make_resolver(schemas), instances)
    instance = instantiate(schema, context=context)
    instance = complete_activity(instance, schema, writer, {"kunden_nr": 98}, context=context)
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
    return runtime, instances, instance.id


def test_a_completion_whose_write_hits_no_row_fails_and_keeps_the_task(tmp_path: Path) -> None:
    url = _url(tmp_path)
    runtime, instances, iid = _runtime(url)
    task = runtime.fetch_and_lock("w1", ["zeilen"], lock_ms=60_000)[0]
    before = instances.get(iid)
    assert before is not None

    with pytest.raises(ExternalTaskError) as exc:
        runtime.complete(task.id, "w1", {"status_extern": "7000"})

    assert exc.value.status == 502
    assert exc.value.code == "DAL.no-row"
    stored = runtime.get(task.id)
    assert stored is not None and stored.state is ExternalTaskState.LOCKED
    after = instances.get(iid)
    assert after is not None
    assert after.state is InstanceState.RUNNING
    assert after.data_values == before.data_values
    assert _statuses(url) == [None, None, None]


def test_the_api_hands_the_code_to_the_worker() -> None:
    def refuse() -> object:
        raise ExternalTaskError("post-flush of 'x' failed: no row", 502, code="DAL.no-row")

    with pytest.raises(HTTPException) as exc:
        api_module._run_external(refuse)

    assert exc.value.status_code == 502
    assert exc.value.detail == {"message": "post-flush of 'x' failed: no row",
                                "code": "DAL.no-row"}


# --- unreachable system: 502 with a message, never a server error ------------


def _unreachable_url(tmp_path: Path) -> str:
    """SQLite file in a folder that does not exist: every connect fails."""

    return f"sqlite:///{tmp_path / 'fehlt' / 'erp.db'}"


def _select_binding() -> SqlSelectBinding:
    return SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="status",
        column_type=DataType.STRING,
        filters=_binding().filters,
        unique_column="kd_id",
    )


@pytest.mark.parametrize(
    "operation",
    [
        lambda c: c.ping(),
        lambda c: c.read("Kunde", 1),
        lambda c: c.query("Kunde", {}),
        lambda c: c.write("Kunde", 1, {"status": "x"}),
        lambda c: c.select_scalar(_select_binding(), {"kunden_nr": 1}),
        lambda c: c.update_scalar(_binding(), "x", {"kunden_nr": 1}),
    ],
    ids=["ping", "read", "query", "write", "select_scalar", "update_scalar"],
)
def test_every_sql_operation_reports_an_outage_as_data_access_error(
    tmp_path: Path, operation
) -> None:
    """Ein nicht erreichbares System kam als roher Treiberfehler durch (500).
    Jede Anweisung meldet es jetzt als ``DataAccessError`` -- ohne Verbindungs-
    angaben in der Meldung."""

    url = _unreachable_url(tmp_path)
    connector = SqlAlchemyConnector(create_engine(url))

    with pytest.raises(DataAccessError) as exc:
        operation(connector)

    assert not isinstance(exc.value, WriteRowCountError)
    assert "external system failed" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_a_completion_against_an_unreachable_system_is_502_and_keeps_the_task(
    tmp_path: Path,
) -> None:
    runtime, instances, iid = _runtime(_unreachable_url(tmp_path))
    task = runtime.fetch_and_lock("w1", ["zeilen"], lock_ms=60_000)[0]
    before = instances.get(iid)
    assert before is not None

    with pytest.raises(ExternalTaskError) as exc:
        runtime.complete(task.id, "w1", {"status_extern": "7000"})

    assert exc.value.status == 502
    assert "external system failed" in exc.value.message
    assert str(tmp_path) not in exc.value.message
    stored = runtime.get(task.id)
    assert stored is not None and stored.state is ExternalTaskState.LOCKED
    after = instances.get(iid)
    assert after is not None and after.state is InstanceState.RUNNING
    assert after.data_values == before.data_values


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("unreachable", id="nicht-erreichbar"),
        # A URL the driver cannot even parse: its text (with the password)
        # must not reach the answer.
        pytest.param("keintreiber://nutzer:geheim-123@db.example/erp", id="unbekannter-treiber"),
    ],
)
def test_the_connection_test_answers_502_without_connection_details(
    tmp_path: Path, url: str
) -> None:
    from fastapi.testclient import TestClient

    from procworks.connections import ConnectionConfig

    if url == "unreachable":
        url = _unreachable_url(tmp_path)
    api_module._connections.register(
        ConnectionConfig(connector_id="erp_down", kind=ConnectorKind.MS_SQL, url=url)
    )
    try:
        response = TestClient(api_module.app).post("/v1/connectors/erp_down/test")
    finally:
        api_module._connections._configs.pop("erp_down", None)
        api_module._connections._cache.pop("erp_down", None)

    assert response.status_code == 502, response.text
    assert "external system failed" in response.json()["detail"]["message"]
    assert str(tmp_path) not in response.text
    assert "geheim-123" not in response.text and "nutzer" not in response.text


def test_an_odata_service_without_answer_is_a_data_access_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auch ein OData-System ohne Antwort (Verbindung abgelehnt) kam als roher
    ``URLError`` durch. Jetzt ``DataAccessError`` ohne URL und ohne Token."""

    from urllib import error as urllib_error

    from procworks.odata import ODataConnector

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise urllib_error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr("procworks.odata.urllib_request.urlopen", refuse)
    connector = ODataConnector("https://erp.example/odata", token="geheimes-token")

    with pytest.raises(DataAccessError) as exc:
        connector.ping()

    assert "external system failed (URLError)" in str(exc.value)
    assert "erp.example" not in str(exc.value)
    assert "geheimes-token" not in str(exc.value)


class _DriverDown(Exception):
    """Stands in for a driver error whose text names host and port."""


_DRIVER_TEXT = 'connection to server at "10.20.30.40", port 5439 failed: refused'


def _failing_inspect(_engine: object) -> object:
    """``sqlalchemy.inspect`` replacement: the catalogue read fails in the driver."""

    from sqlalchemy.exc import OperationalError

    raise OperationalError("SELECT ...", {}, _DriverDown(_DRIVER_TEXT))


@pytest.mark.parametrize(
    "operation",
    [lambda c: c.entities(), lambda c: c.columns("Kunde")],
    ids=["entities", "columns"],
)
def test_catalogue_reads_report_an_outage_without_the_driver_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation
) -> None:
    """Listing tables and reflecting columns name only action and error class.

    The driver's text carries host and port of the external system (and may
    carry more); it must not reach any answer, exactly like every other
    statement of the connector.
    """

    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "inspect", _failing_inspect)
    connector = SqlAlchemyConnector(create_engine(f"sqlite:///{tmp_path / 'erp.db'}"))

    with pytest.raises(DataAccessError) as exc:
        operation(connector)

    message = str(exc.value)
    assert "external system failed (_DriverDown)" in message
    assert "10.20.30.40" not in message
    assert "5439" not in message


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/v1/connectors/erp_cat/entities", None),
        ("get", "/v1/connectors/erp_cat/columns?entity=Kunde", None),
        ("post", "/v1/connectors/erp_cat/sample-read", {"entity": "Kunde"}),
    ],
    ids=["entities", "columns", "sample-read"],
)
def test_catalogue_endpoints_answer_502_without_host_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: object
) -> None:
    import sqlalchemy
    from fastapi.testclient import TestClient

    from procworks.connections import ConnectionConfig

    monkeypatch.setattr(sqlalchemy, "inspect", _failing_inspect)
    api_module._connections.register(
        ConnectionConfig(
            connector_id="erp_cat",
            kind=ConnectorKind.MS_SQL,
            url=f"sqlite:///{tmp_path / 'erp.db'}",
        )
    )
    try:
        client = TestClient(api_module.app)
        if method == "get":
            response = client.get(path)
        else:
            response = client.post(path, json=body)
    finally:
        api_module._connections._configs.pop("erp_cat", None)
        api_module._connections._cache.pop("erp_cat", None)

    assert response.status_code == 502, response.text
    assert "external system failed" in response.json()["detail"]["message"]
    assert "10.20.30.40" not in response.text
    assert "5439" not in response.text
