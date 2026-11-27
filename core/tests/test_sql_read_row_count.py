# SPDX-License-Identifier: BUSL-1.1
"""A read by a unique key that finds several rows fails, like the write does.

A scalar select with cardinality ``KEY_UNIQUE`` promises one row (its filter
names a unique column). When the column is not unique after all, the read
used to return the *first* match silently -- the worker got "Dup-a" as input
while the write-back on the same key already failed with ``DAL.many-rows``.
Now the read fails with the same code (``ReadRowCountError``), on SQL and
OData alike; a keyed record read does the same. No row stays ``None`` (an
unset value), and ``AGGREGATE``/``FIRST_ORDERED`` selects are untouched --
they guarantee one row by construction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from staffing import staffed

from procworks import (
    AccessMode,
    AggregateKind,
    AutomationKind,
    ConnectorKind,
    DataAccessLayer,
    DataType,
    FilterOperator,
    QueryFilter,
    SqlAlchemyConnector,
    SqlSelectBinding,
    add_data_element,
    assign_service,
    bind_sql_select,
    complete_activity,
    connect_data,
    create_empty_schema,
    instantiate,
    register_connector,
    release,
    serial_insert,
    set_automation,
)
from procworks.dal import DataAccessError, ReadRowCountError, RowCountError, WriteRowCountError
from procworks.execution import ExecutionContext
from procworks.integration_runtime import ExternalTaskError, ExternalTaskRuntime
from procworks.model import Cardinality, OrderBy, ProcessInstance, ProcessSchema
from procworks.odata import ODataConnector
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)


def _url(tmp_path: Path) -> str:
    """SQLite ``Kunde``: kd_id 1 ("Eins") once, kd_id 3 twice ("Dup-a", "Dup-b")."""

    url = f"sqlite:///{tmp_path / 'erp.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Kunde (kd_id INTEGER, name TEXT)"))
        for kd_id, name in ((1, "Eins"), (3, "Dup-a"), (3, "Dup-b")):
            conn.execute(
                text("INSERT INTO Kunde (kd_id, name) VALUES (:i, :n)"), {"i": kd_id, "n": name}
            )
    engine.dispose()
    return url


def _key_filter() -> list[QueryFilter]:
    return [
        QueryFilter(
            column="kd_id",
            column_type=DataType.INTEGER,
            operator=FilterOperator.EQ,
            key_element_id="kunden_nr",
        )
    ]


def _by_key() -> SqlSelectBinding:
    return SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="name",
        column_type=DataType.STRING,
        filters=_key_filter(),
        cardinality=Cardinality.KEY_UNIQUE,
        unique_column="kd_id",
    )


def test_the_error_shares_the_code_family_with_the_write() -> None:
    assert issubclass(ReadRowCountError, RowCountError)
    assert issubclass(WriteRowCountError, RowCountError)
    assert issubclass(RowCountError, DataAccessError)


def test_a_unique_key_reads_its_one_value(tmp_path: Path) -> None:
    connector = SqlAlchemyConnector(create_engine(_url(tmp_path)))
    assert connector.select_scalar(_by_key(), {"kunden_nr": 1}) == "Eins"


def test_a_key_without_row_reads_nothing(tmp_path: Path) -> None:
    connector = SqlAlchemyConnector(create_engine(_url(tmp_path)))
    assert connector.select_scalar(_by_key(), {"kunden_nr": 2}) is None


def test_a_key_with_several_rows_fails_instead_of_taking_the_first(tmp_path: Path) -> None:
    connector = SqlAlchemyConnector(create_engine(_url(tmp_path)))
    with pytest.raises(ReadRowCountError) as exc:
        connector.select_scalar(_by_key(), {"kunden_nr": 3})
    assert exc.value.code == "DAL.many-rows"
    assert "Dup-a" not in str(exc.value)


def test_aggregate_and_first_ordered_selects_are_untouched(tmp_path: Path) -> None:
    connector = SqlAlchemyConnector(create_engine(_url(tmp_path)))
    counted = SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="kd_id",
        column_type=DataType.INTEGER,
        aggregate=AggregateKind.COUNT,
        filters=_key_filter(),
        cardinality=Cardinality.AGGREGATE,
    )
    first = SqlSelectBinding(
        connector_id="erp",
        entity="Kunde",
        column="name",
        column_type=DataType.STRING,
        filters=_key_filter(),
        cardinality=Cardinality.FIRST_ORDERED,
        order_by=[OrderBy(column="name", descending=True)],
    )
    assert connector.select_scalar(counted, {"kunden_nr": 3}) == 2
    assert connector.select_scalar(first, {"kunden_nr": 3}) == "Dup-b"


def test_a_keyed_record_read_with_several_rows_fails(tmp_path: Path) -> None:
    connector = SqlAlchemyConnector(create_engine(_url(tmp_path)), key_column="kd_id")
    assert connector.read("Kunde", 1) == {"kd_id": 1, "name": "Eins"}
    with pytest.raises(ReadRowCountError) as exc:
        connector.read("Kunde", 3)
    assert exc.value.code == "DAL.many-rows"


class _Transport:
    """Answers every GET with the given OData rows and records the URL."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.urls: list[str] = []

    def request(self, method: str, url: str, **_kwargs: object) -> tuple[int, str]:
        self.urls.append(url)
        return 200, json.dumps({"value": self.rows})


def test_odata_reads_two_rows_to_tell_one_from_several() -> None:
    several = _Transport([{"name": "Dup-a"}, {"name": "Dup-b"}])
    with pytest.raises(ReadRowCountError) as exc:
        ODataConnector("https://erp.example/odata", transport=several).select_scalar(
            _by_key(), {"kunden_nr": 3}
        )
    assert exc.value.code == "DAL.many-rows"
    assert "%24top=2" in several.urls[0] or "$top=2" in several.urls[0]

    one = _Transport([{"name": "Eins"}])
    connector = ODataConnector("https://erp.example/odata", transport=one)
    assert connector.select_scalar(_by_key(), {"kunden_nr": 1}) == "Eins"


# --- external task: the pre-fetch names the reason ---------------------------


def test_a_prefetch_on_a_non_unique_key_fails_with_the_code(tmp_path: Path) -> None:
    schema = create_empty_schema("Lesen", schema_id="read-rows")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "Kundennr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "Name", DataType.STRING, element_id="name_extern")
    schema = bind_sql_select(
        schema,
        "name_extern",
        connector_id="erp",
        entity="Kunde",
        column="name",
        column_type=DataType.STRING,
        filters=_key_filter(),
        unique_column="kd_id",
    )
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = serial_insert(schema, "Bearbeiten", after_node_id=writer)
    worker = next(n.id for n in schema.nodes.values() if n.label == "Bearbeiten")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    schema = assign_service(schema, worker, "Bearbeiten", automatic=True)
    schema = set_automation(schema, worker, AutomationKind.EXTERNAL_TASK, topic="lesen")
    schema = connect_data(schema, worker, "name_extern", AccessMode.READ, mandatory=False)
    schema = release(staffed(schema))
    schemas = InMemorySchemaStore()
    schemas.put(schema)
    instances = InMemoryInstanceStore()
    context = ExecutionContext(make_resolver(schemas), instances)
    instance = instantiate(schema, context=context)
    instance = complete_activity(instance, schema, writer, {"kunden_nr": 3}, context=context)
    instances.put(instance)
    dal = DataAccessLayer()
    dal.register("erp", SqlAlchemyConnector(create_engine(_url(tmp_path))))

    def schema_for(inst: ProcessInstance) -> ProcessSchema:
        return schema

    runtime = ExternalTaskRuntime(
        InMemoryExternalTaskStore(), instances, schema_for, context, dal=dal
    )
    with pytest.raises(ExternalTaskError) as exc:
        runtime.fetch_and_lock("w1", ["lesen"], lock_ms=60_000)
    assert (exc.value.status, exc.value.code) == (502, "DAL.many-rows")
    assert "Dup-a" not in exc.value.message
