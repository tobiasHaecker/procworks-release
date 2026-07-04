# SPDX-License-Identifier: BUSL-1.1
"""OData connector tests (roadmap Q5) -- fully offline via a fake transport.

The OData connector fulfils the same structured SPI as the SQL connector by
translating :class:`SqlSelectBinding` / :class:`SqlWriteBinding` into OData query
options (``$select``/``$filter``/``$orderby``/``$top``/``$count``/``$apply``) and
a keyed ``PATCH``. A fake HTTP transport records the built requests and returns
canned JSON, so no network (and no live Dynamics 365 / SAP) is needed.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from procworks import (
    AggregateKind,
    Cardinality,
    ConnectionConfig,
    ConnectionRegistry,
    ConnectorKind,
    DataAccessError,
    DataAccessLayer,
    DataType,
    FilterOperator,
    ODataConnector,
    OrderBy,
    QueryFilter,
    SqlSelectBinding,
    SqlWriteBinding,
    add_data_element,
    bind_sql_select,
    connect_data,
    create_empty_schema,
    register_connector,
    serial_insert,
)
from procworks.model import AccessMode

_BASE = "https://svc/odata"


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses: list[tuple[int, str]] = []

    def push(self, text: str, status: int = 200) -> None:
        self._responses.append((status, text))

    def request(self, method, url, *, headers, body, timeout):  # type: ignore[no-untyped-def]
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if self._responses:
            return self._responses.pop(0)
        return (200, json.dumps({"value": []}))


def _select(**overrides: object) -> SqlSelectBinding:
    spec: dict[str, object] = {
        "connector_id": "dv",
        "entity": "accounts",
        "column": "name",
        "column_type": DataType.STRING,
        "filters": [
            QueryFilter(
                column="accountid",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        "cardinality": Cardinality.KEY_UNIQUE,
        "unique_column": "accountid",
    }
    spec.update(overrides)
    return SqlSelectBinding(**spec)  # type: ignore[arg-type]


def _connector(transport: _FakeTransport, *, token: str | None = None) -> ODataConnector:
    return ODataConnector(_BASE, transport=transport, token=token)


# --- select_scalar translation -------------------------------------------


def test_select_scalar_builds_query_and_returns_value() -> None:
    transport = _FakeTransport()
    transport.push(json.dumps({"value": [{"name": "Acme"}]}))
    result = _connector(transport).select_scalar(_select(), {"kunden_nr": 42})
    assert result == "Acme"
    url = transport.calls[0]["url"]
    assert isinstance(url, str)
    assert "$select=name" in url
    assert "$top=1" in url
    assert "$filter=" + quote("accountid eq 42") in url


def test_select_scalar_count_uses_dollar_count() -> None:
    transport = _FakeTransport()
    transport.push("7")
    binding = _select(
        aggregate=AggregateKind.COUNT,
        cardinality=Cardinality.AGGREGATE,
        unique_column="",
        filters=[
            QueryFilter(
                column="statecode",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="state",
            )
        ],
    )
    assert _connector(transport).select_scalar(binding, {"state": 0}) == 7
    assert "/accounts/$count" in str(transport.calls[0]["url"])


def test_select_scalar_avg_uses_apply() -> None:
    transport = _FakeTransport()
    transport.push(json.dumps({"value": [{"val": 123.5}]}))
    binding = _select(
        column="revenue",
        column_type=DataType.FLOAT,
        aggregate=AggregateKind.AVG,
        cardinality=Cardinality.AGGREGATE,
        unique_column="",
        filters=[],
    )
    assert _connector(transport).select_scalar(binding, {}) == 123.5
    assert "$apply=" + quote("aggregate(revenue with average as val)") in str(
        transport.calls[0]["url"]
    )


def test_select_scalar_first_ordered_adds_orderby() -> None:
    transport = _FakeTransport()
    transport.push(json.dumps({"value": [{"name": "Zeta"}]}))
    binding = _select(
        cardinality=Cardinality.FIRST_ORDERED,
        unique_column="",
        order_by=[OrderBy(column="createdon", descending=True)],
        filters=[
            QueryFilter(
                column="city",
                column_type=DataType.STRING,
                operator=FilterOperator.EQ,
                key_element_id="c",
            )
        ],
    )
    _connector(transport).select_scalar(binding, {"c": "Bonn"})
    url = str(transport.calls[0]["url"])
    assert "$orderby=" + quote("createdon desc") in url
    assert "$top=1" in url


def test_filter_in_operator_translates_to_odata_in() -> None:
    transport = _FakeTransport()
    transport.push(json.dumps({"value": [{"name": "Acme"}]}))
    binding = _select(
        cardinality=Cardinality.FIRST_ORDERED,
        unique_column="",
        order_by=[OrderBy(column="accountid")],
        filters=[
            QueryFilter(
                column="accountid",
                column_type=DataType.INTEGER,
                operator=FilterOperator.IN,
                key_element_id="ids",
            )
        ],
    )
    _connector(transport).select_scalar(binding, {"ids": [1, 2]})
    assert quote("accountid in (1,2)") in str(transport.calls[0]["url"])


def test_filter_like_translates_to_startswith() -> None:
    transport = _FakeTransport()
    transport.push(json.dumps({"value": [{"name": "Acme"}]}))
    binding = _select(
        cardinality=Cardinality.FIRST_ORDERED,
        unique_column="",
        order_by=[OrderBy(column="name")],
        filters=[
            QueryFilter(
                column="name",
                column_type=DataType.STRING,
                operator=FilterOperator.LIKE,
                key_element_id="pat",
            )
        ],
    )
    _connector(transport).select_scalar(binding, {"pat": "Ac%"})
    assert quote("startswith(name,'Ac')") in str(transport.calls[0]["url"])


def test_string_literal_escapes_single_quote() -> None:
    transport = _FakeTransport()
    transport.push(json.dumps({"value": []}))
    binding = _select(
        filters=[
            QueryFilter(
                column="name",
                column_type=DataType.STRING,
                operator=FilterOperator.EQ,
                key_element_id="n",
            )
        ]
    )
    _connector(transport).select_scalar(binding, {"n": "O'Brien"})
    assert quote("name eq 'O''Brien'") in str(transport.calls[0]["url"])


# --- update_scalar / record SPI ------------------------------------------


def test_update_scalar_patches_entity_by_key() -> None:
    transport = _FakeTransport()
    transport.push("", status=204)
    binding = SqlWriteBinding(
        connector_id="dv",
        entity="accounts",
        column="statuscode",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="accountid",
                column_type=DataType.STRING,
                operator=FilterOperator.EQ,
                key_element_id="acc",
            )
        ],
        unique_column="accountid",
    )
    affected = _connector(transport).update_scalar(binding, "closed", {"acc": "A-1"})
    assert affected == 1
    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == f"{_BASE}/accounts('A-1')"
    assert json.loads(str(call["body"], "utf-8")) == {"statuscode": "closed"}


def test_read_strips_odata_metadata() -> None:
    transport = _FakeTransport()
    transport.push(json.dumps({"@odata.context": "x", "accountid": "A-1", "name": "Acme"}))
    record = _connector(transport).read("accounts", "A-1")
    assert record == {"accountid": "A-1", "name": "Acme"}
    assert transport.calls[0]["url"] == f"{_BASE}/accounts('A-1')"


def test_query_builds_equality_filter() -> None:
    transport = _FakeTransport()
    transport.push(json.dumps({"value": [{"name": "Acme"}]}))
    rows = _connector(transport).query("accounts", {"city": "Bonn"})
    assert rows == [{"name": "Acme"}]
    assert quote("city eq 'Bonn'") in str(transport.calls[0]["url"])


def test_columns_infer_types_from_sample_row() -> None:
    transport = _FakeTransport()
    transport.push(
        json.dumps(
            {"value": [{"accountid": 1, "name": "Acme", "revenue": 1.5, "active": True}]}
        )
    )
    mapped = {c["column"]: c["data_type"] for c in _connector(transport).columns("accounts")}
    assert mapped["accountid"] is DataType.INTEGER
    assert mapped["name"] is DataType.STRING
    assert mapped["revenue"] is DataType.FLOAT
    assert mapped["active"] is DataType.BOOLEAN


def test_bearer_token_is_sent() -> None:
    transport = _FakeTransport()
    transport.push(json.dumps({"value": []}))
    _connector(transport, token="tok").query("accounts", {})
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer tok"  # type: ignore[index]


def test_http_error_raises_data_access_error() -> None:
    transport = _FakeTransport()
    transport.push("boom", status=500)
    with pytest.raises(DataAccessError):
        _connector(transport).read("accounts", "x")


def test_unsafe_entity_is_rejected() -> None:
    transport = _FakeTransport()
    with pytest.raises(DataAccessError):
        _connector(transport).read("accounts; DROP", "x")


# --- registry dispatch + end-to-end via the DAL --------------------------


def test_registry_builds_odata_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DV_TOKEN", "secret")
    registry = ConnectionRegistry()
    registry.register(
        ConnectionConfig(
            connector_id="dv",
            kind=ConnectorKind.DYNAMICS_365,
            url=f"{_BASE}",
            token_env="DV_TOKEN",
        )
    )
    assert isinstance(registry.connector("dv"), ODataConnector)


def test_registry_odata_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DV_TOKEN", raising=False)
    registry = ConnectionRegistry()
    registry.register(
        ConnectionConfig(
            connector_id="dv",
            kind=ConnectorKind.SAP,
            url=f"{_BASE}",
            token_env="DV_TOKEN",
        )
    )
    with pytest.raises(DataAccessError):
        registry.connector("dv")


def _odata_bound_schema():  # type: ignore[no-untyped-def]
    schema = create_empty_schema("OData", schema_id="odsc")
    schema = register_connector(schema, "Dataverse", ConnectorKind.DYNAMICS_365, connector_id="dv")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "kundenname", DataType.STRING, element_id="kundenname")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = serial_insert(schema, "Pruefen", after_node_id=writer)
    reader = next(n.id for n in schema.nodes.values() if n.label == "Pruefen")
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    schema = bind_sql_select(
        schema,
        "kundenname",
        connector_id="dv",
        entity="accounts",
        column="name",
        column_type=DataType.STRING,
        filters=[
            QueryFilter(
                column="accountid",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        cardinality=Cardinality.KEY_UNIQUE,
        unique_column="accountid",
    )
    schema = connect_data(schema, reader, "kundenname", AccessMode.READ, mandatory=False)
    return schema


def test_read_scalar_via_dal_routes_to_odata() -> None:
    schema = _odata_bound_schema()
    transport = _FakeTransport()
    transport.push(json.dumps({"value": [{"name": "Acme"}]}))
    dal = DataAccessLayer()
    dal.register("dv", ODataConnector(_BASE, transport=transport))
    assert dal.read_scalar(schema, {"kunden_nr": 42}, "kundenname") == "Acme"
