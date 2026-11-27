# SPDX-License-Identifier: BUSL-1.1
"""Table and column names of external SQL access: one check, everywhere.

A modelled binding, the sample read and the column reflection all name a
table (and columns) of an external database. Two promises hold for every one
of these paths, at modelling time as at run time:

* **No system catalogue** -- ``sqlite_master``, ``pg_catalog.*``,
  ``information_schema.*``, ``sys.*`` … are never read or written, whether the
  name is qualified with a schema (``main.sqlite_master``) or not, and
  whatever its spelling (``PG_CATALOG.pg_authid``). Such a table reveals the
  structure (and in PostgreSQL the password hashes) of the whole database.
* **Only plain identifiers** -- a name with ``;``, quotes or a comment marker
  is the caller's input error (422 with a code), not a failing external system
  (502), and it is refused when it is modelled, not first when a case runs.

The check lives in one function (:func:`procworks.dal.check_entity_name` /
:func:`procworks.dal.check_column_name`); every path calls it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from planted import plant
from sqlalchemy import create_engine, text
from staffing import staffed

import procworks.api as api_module
from procworks import (
    AccessMode,
    AggregateKind,
    Cardinality,
    ConnectionConfig,
    ConnectorKind,
    CorrectnessError,
    DataType,
    ExternalBinding,
    FilterOperator,
    OrderBy,
    ProcessSchema,
    QueryFilter,
    SqlAlchemyConnector,
    SqlSelectBinding,
    SqlWriteBinding,
    add_data_element,
    bind_external_data,
    bind_sql_select,
    compile_select,
    connect_data,
    create_empty_schema,
    export_bpmn,
    register_connector,
    serial_insert,
    validate,
)
from procworks.api import _connections, _store, app
from procworks.auth_token import TokenAuthBackend
from procworks.dal import UnsafeIdentifierError, check_column_name, check_entity_name

client = TestClient(app)

#: System catalogues of the supported databases, in the spellings that slipped
#: past the old prefix check (schema-qualified, mixed case).
SYSTEM_ENTITIES = [
    "sqlite_master",
    "main.sqlite_master",
    "temp.sqlite_temp_master",
    "Main.SQLITE_MASTER",
    "sqlite_sequence",
    "pragma_table_list",
    "pg_catalog.pg_authid",
    "PG_CATALOG.pg_authid",
    "pg_authid",
    "public.pg_shadow",
    "information_schema.tables",
    "INFORMATION_SCHEMA.COLUMNS",
    "sys.objects",
    "mysql.user",
    "performance_schema.threads",
    # Virtual tables and compatibility views that carry no system prefix:
    # SQLite's ``dbstat`` lists every table and index of the file, MS SQL's
    # ``sys*`` views are readable without a schema.
    "dbstat",
    "main.dbstat",
    "MAIN.DBSTAT",
    "sysobjects",
    "dbo.syscolumns",
]

#: Names that are no plain (optionally schema-qualified) identifier.
UNSAFE_ENTITIES = [
    "kunden; DROP TABLE kunden",
    'kunden"--',
    "kunden--",
    "master.sys.objects",
    "a.b.c",
    "kunden/*x*/",
    "",
]


# --- the one check -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Kunde", "kunden", "public.kunden", "dbo.Kunde", "system_log", "dbstat_archiv", "systeme"],
)
def test_business_tables_pass_the_check(name: str) -> None:
    assert check_entity_name(name) == name


@pytest.mark.parametrize("name", SYSTEM_ENTITIES)
def test_system_catalogues_are_refused_with_their_own_code(name: str) -> None:
    with pytest.raises(UnsafeIdentifierError) as exc:
        check_entity_name(name)
    assert exc.value.code == "DAL.system-entity"


@pytest.mark.parametrize("name", UNSAFE_ENTITIES)
def test_unsafe_table_names_are_refused_as_unsafe(name: str) -> None:
    with pytest.raises(UnsafeIdentifierError) as exc:
        check_entity_name(name)
    assert exc.value.code == "DAL.unsafe-name"


@pytest.mark.parametrize("name", ['name"--', "name;", "a.b", "", "x y"])
def test_unsafe_column_names_are_refused(name: str) -> None:
    with pytest.raises(UnsafeIdentifierError) as exc:
        check_column_name(name)
    assert exc.value.code == "DAL.unsafe-name"


def test_the_compiler_refuses_a_system_catalogue_at_run_time() -> None:
    """A stored binding is compiled before every read -- it never reaches SQL."""

    binding = SqlSelectBinding(
        connector_id="erp",
        entity="main.sqlite_master",
        column="sql",
        column_type=DataType.STRING,
        cardinality=Cardinality.AGGREGATE,
        aggregate=AggregateKind.MAX,
    )
    with pytest.raises(UnsafeIdentifierError) as exc:
        compile_select(binding)
    assert exc.value.code == "DAL.system-entity"


def test_the_sql_connector_refuses_a_qualified_system_catalogue(tmp_path: Path) -> None:
    url = _seed(tmp_path)
    connector = SqlAlchemyConnector(create_engine(url))
    with pytest.raises(UnsafeIdentifierError) as exc:
        connector.query("main.sqlite_master", {})
    assert exc.value.code == "DAL.system-entity"
    assert connector.query("main.Kunde", {})  # the business table stays readable


# --- /v1/connectors: sample read, columns, entities ----------------------


def _seed(tmp_path: Path) -> str:
    """A SQLite database with one business table; AUTOINCREMENT creates the
    system table ``sqlite_sequence`` next to it."""

    url = f"sqlite:///{tmp_path / 'erp.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE Kunde (nr INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, name TEXT)"
        ))
        conn.execute(text("INSERT INTO Kunde (id, name) VALUES ('K1', 'Acme')"))
    engine.dispose()
    return url


@pytest.fixture
def connector(tmp_path: Path):  # type: ignore[no-untyped-def]
    _connections.register(ConnectionConfig(connector_id="cat-erp", url=_seed(tmp_path)))
    yield "cat-erp"
    _connections._configs.pop("cat-erp", None)
    _connections._cache.pop("cat-erp", None)


@pytest.mark.parametrize("entity", SYSTEM_ENTITIES)
def test_sample_read_never_returns_a_system_catalogue(connector: str, entity: str) -> None:
    res = client.post(
        f"/v1/connectors/{connector}/sample-read", json={"entity": entity, "limit": 5}
    )
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "DAL.system-entity"
    assert res.json()["detail"]["params"] == {"name": entity}


def test_sample_read_refuses_a_system_catalogue_for_a_service_token(connector: str) -> None:
    original = api_module._auth_backend
    api_module._auth_backend = TokenAuthBackend({
        "svc": {"subject": "bridge", "roles": ["integration"], "scopes": ["data:read"]},
    })
    try:
        res = client.post(
            f"/v1/connectors/{connector}/sample-read",
            json={"entity": "main.sqlite_master", "limit": 5},
            headers={"Authorization": "Bearer svc"},
        )
        ok = client.post(
            f"/v1/connectors/{connector}/sample-read",
            json={"entity": "Kunde", "limit": 5},
            headers={"Authorization": "Bearer svc"},
        )
    finally:
        api_module._auth_backend = original
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "DAL.system-entity"
    assert ok.status_code == 200


def test_sample_read_reports_an_unsafe_name_with_its_code(connector: str) -> None:
    res = client.post(
        f"/v1/connectors/{connector}/sample-read",
        json={"entity": "main.Kunde;DROP", "limit": 1},
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "DAL.unsafe-name"


def test_sample_read_still_reads_a_business_table(connector: str) -> None:
    for entity in ("Kunde", "main.Kunde"):
        res = client.post(
            f"/v1/connectors/{connector}/sample-read", json={"entity": entity, "limit": 1}
        )
        assert res.status_code == 200, res.text
        assert res.json()[0]["id"] == "K1"


@pytest.mark.parametrize("entity", ["sqlite_master", "main.sqlite_master", "Main.SQLITE_MASTER"])
def test_columns_never_reflect_a_system_catalogue(connector: str, entity: str) -> None:
    res = client.get(f"/v1/connectors/{connector}/columns", params={"entity": entity})
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "DAL.system-entity"


def test_columns_report_an_injected_name_as_input_error_not_bad_gateway(
    connector: str,
) -> None:
    res = client.get(
        f"/v1/connectors/{connector}/columns", params={"entity": "kunden; DROP TABLE kunden"}
    )
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "DAL.unsafe-name"


def test_columns_of_a_business_table_still_work(connector: str) -> None:
    res = client.get(f"/v1/connectors/{connector}/columns", params={"entity": "Kunde"})
    assert res.status_code == 200
    assert {c["column"] for c in res.json()} == {"nr", "id", "name"}


def test_entities_offer_no_system_catalogue(connector: str) -> None:
    raw = create_engine(_connections._configs[connector].url)
    with raw.connect() as conn:
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master"))}
    raw.dispose()
    assert "sqlite_sequence" in tables  # the precondition of this test

    res = client.get(f"/v1/connectors/{connector}/entities")
    assert res.status_code == 200
    assert res.json() == ["Kunde"]


# --- modelling: the bindings refuse the same names -----------------------


def _schema(schema_id: str) -> ProcessSchema:
    schema = create_empty_schema("Katalog", schema_id=schema_id)
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "kundenname", DataType.STRING, element_id="kundenname")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = next(n for n in schema.nodes.values() if n.label == "Erfassen").id
    return connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)


def _select_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "connector_id": "erp",
        "entity": "kunden",
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
    }
    body.update(overrides)
    return body


def _post_binding(kind: str, schema_id: str, body: dict[str, object]):  # type: ignore[no-untyped-def]
    plant(_store, _schema(schema_id))
    before = _store.get(schema_id).model_dump()
    res = client.post(f"/schemas/{schema_id}/data-elements/kundenname/{kind}", json=body)
    return res, before, _store.get(schema_id).model_dump()


def _codes(res) -> list[str]:  # type: ignore[no-untyped-def]
    return [f["code"] for f in res.json()["detail"]["findings"]]


@pytest.mark.parametrize("kind", ["sql-select", "sql-write"])
@pytest.mark.parametrize(
    "entity", ["pg_catalog.pg_authid", "PG_CATALOG.pg_authid", "main.sqlite_master",
               "information_schema.tables", "sys.objects"]
)
def test_a_binding_to_a_system_catalogue_is_refused_when_modelled(
    kind: str, entity: str
) -> None:
    res, before, after = _post_binding(kind, "cat_sys", _select_body(entity=entity))
    assert res.status_code == 422, res.text
    assert _codes(res) == ["C10.system-entity"]
    assert res.json()["detail"]["findings"][0]["rule"] == "C10"
    assert after == before


@pytest.mark.parametrize("kind", ["sql-select", "sql-write"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity", "kunden; DROP TABLE kunden"),
        ("column", 'name"--'),
        ("unique_column", "kd_id;--"),
    ],
)
def test_a_binding_with_an_unsafe_name_is_refused_when_modelled(
    kind: str, field: str, value: str
) -> None:
    res, before, after = _post_binding(kind, "cat_unsafe", _select_body(**{field: value}))
    assert res.status_code == 422, res.text
    assert "C10.unsafe-name" in _codes(res)
    refused = [f for f in res.json()["detail"]["findings"] if f["code"] == "C10.unsafe-name"]
    assert [f["params"]["name"] for f in refused] == [value]
    assert after == before


@pytest.mark.parametrize("kind", ["sql-select", "sql-write"])
def test_a_binding_with_an_unsafe_filter_column_is_refused(kind: str) -> None:
    body = _select_body()
    body["filters"] = [{
        "column": "kd_id or 1=1",
        "column_type": "INTEGER",
        "operator": "EQ",
        "key_element_id": "kunden_nr",
    }]
    res, before, after = _post_binding(kind, "cat_filter", body)
    assert res.status_code == 422
    assert "C10.unsafe-name" in _codes(res)
    assert after == before


def test_a_select_with_an_unsafe_order_column_is_refused() -> None:
    schema = _schema("cat_order")
    with pytest.raises(CorrectnessError) as exc:
        bind_sql_select(
            schema,
            "kundenname",
            connector_id="erp",
            entity="kunden",
            column="name",
            column_type=DataType.STRING,
            cardinality=Cardinality.FIRST_ORDERED,
            order_by=[OrderBy(column="datum desc; --", descending=True)],
        )
    assert [f.code for f in exc.value.findings] == ["C10.unsafe-name"]


@pytest.mark.parametrize("kind", ["sql-select", "sql-write"])
@pytest.mark.parametrize("entity", ["kunden", "public.kunden", "dbo.Kunde"])
def test_a_binding_to_a_business_table_is_accepted(kind: str, entity: str) -> None:
    res, _before, after = _post_binding(kind, "cat_ok", _select_body(entity=entity))
    assert res.status_code == 200, res.text
    binding = after["data_elements"]["kundenname"]["select" if kind == "sql-select" else "write"]
    assert binding["entity"] == entity


def test_a_record_binding_to_a_system_catalogue_is_refused() -> None:
    schema = _schema("cat_record")
    with pytest.raises(CorrectnessError) as exc:
        bind_external_data(
            schema,
            "kundenname",
            connector_id="erp",
            entity="main.sqlite_master",
            key_element_id="kunden_nr",
        )
    assert [f.code for f in exc.value.findings] == ["C10.system-entity"]


def test_an_empty_name_keeps_its_structural_finding() -> None:
    """Empty names stay the business of the rules that already report them."""

    schema = _schema("cat_empty")
    with pytest.raises(CorrectnessError) as exc:
        bind_sql_select(
            schema,
            "kundenname",
            connector_id="erp",
            entity="   ",
            column="name",
            column_type=DataType.STRING,
            filters=[QueryFilter(column="kd_id", column_type=DataType.INTEGER,
                                 operator=FilterOperator.EQ, key_element_id="kunden_nr")],
            unique_column="kd_id",
        )
    codes = [f.code for f in exc.value.findings]
    assert "C5.empty-entity" in codes
    assert "C10.unsafe-name" not in codes


def test_entities_drop_system_catalogues_a_driver_reports(
    connector: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever a driver lists, the offer holds only usable business tables."""

    monkeypatch.setattr(
        SqlAlchemyConnector,
        "entities",
        lambda self: ["Kunde", "sqlite_sequence", "pg_catalog.pg_class", "sys.objects"],
    )
    res = client.get(f"/v1/connectors/{connector}/entities")
    assert res.status_code == 200
    assert res.json() == ["Kunde"]


# --- validate(): the same promise for every way into a schema --------------
#
# The bind operations are not the only entrance: the BPMN import maps the
# bindings of an export as they are, and an ad-hoc change, a migration or a
# stored draft never pass a bind operation at all. The rule therefore lives in
# validate() (C10) -- an imported model with such a binding is refused instead
# of being released and failing at the first case.


def _bound_schema(schema_id: str) -> ProcessSchema:
    """A correct schema whose element ``kundenname`` reads a business table."""

    schema = _schema(schema_id)
    return bind_sql_select(
        schema,
        "kundenname",
        connector_id="erp",
        entity="kunden",
        column="name",
        column_type=DataType.STRING,
        filters=[QueryFilter(column="kd_id", column_type=DataType.INTEGER,
                             operator=FilterOperator.EQ, key_element_id="kunden_nr")],
        unique_column="kd_id",
    )


def _with_select(schema: ProcessSchema, **changes: object) -> ProcessSchema:
    """``schema`` with its select binding changed *past* the operations."""

    tampered = schema.model_copy(deep=True)
    select = tampered.data_elements["kundenname"].select
    assert select is not None
    tampered.data_elements["kundenname"].select = select.model_copy(update=changes)
    return tampered


@pytest.mark.parametrize(
    ("changes", "code", "name"),
    [
        ({"entity": "main.sqlite_master"}, "C10.system-entity", "main.sqlite_master"),
        ({"entity": "pg_catalog.pg_authid"}, "C10.system-entity", "pg_catalog.pg_authid"),
        ({"entity": "main.dbstat"}, "C10.system-entity", "main.dbstat"),
        ({"entity": "kunden; DROP TABLE kunden"}, "C10.unsafe-name", "kunden; DROP TABLE kunden"),
        ({"column": 'name"--'}, "C10.unsafe-name", 'name"--'),
    ],
)
def test_validate_reports_a_stored_binding_to_an_unusable_name(
    changes: dict[str, object], code: str, name: str
) -> None:
    findings = validate(_with_select(_bound_schema("val_names"), **changes))
    assert [(f.rule, f.code) for f in findings] == [("C10", code)]
    assert findings[0].params == {"element": "kundenname", "name": name}


def test_validate_accepts_a_binding_to_a_business_table() -> None:
    assert validate(_with_select(_bound_schema("val_ok"), entity="public.kunden")) == []


def test_validate_reports_a_record_binding_and_a_write_binding_alike() -> None:
    schema = _bound_schema("val_kinds")
    tampered = schema.model_copy(deep=True)
    tampered.data_elements["kundenname"].select = None
    tampered.data_elements["kundenname"].external = ExternalBinding(
        connector_id="erp", entity="sqlite_master", key_element_id="kunden_nr"
    )
    findings = validate(tampered)
    assert [f.code for f in findings] == ["C10.system-entity"]

    written = schema.model_copy(deep=True)
    select = written.data_elements["kundenname"].select
    assert select is not None
    written.data_elements["kundenname"].select = None
    written.data_elements["kundenname"].write = SqlWriteBinding(
        connector_id="erp", entity="kunden", column="name", column_type=DataType.STRING,
        filters=select.filters, unique_column="kd_id;--",
    )
    assert "C10.unsafe-name" in [f.code for f in validate(written)]


def _export_with(entity: str) -> str:
    """The BPMN export of a bound schema, with the entity replaced in the file."""

    xml = export_bpmn(_bound_schema("exp_src"))
    assert xml.count('"entity": "kunden"') == 1  # precondition of the replacement
    return xml.replace('"entity": "kunden"', json.dumps({"entity": entity})[1:-1])


@pytest.mark.parametrize(
    ("entity", "code"),
    [
        ("main.sqlite_master", "C10.system-entity"),
        ("pg_catalog.pg_authid", "C10.system-entity"),
        ("main.dbstat", "C10.system-entity"),
        ("kunden; DROP TABLE kunden", "C10.unsafe-name"),
    ],
)
def test_the_bpmn_import_refuses_a_binding_to_an_unusable_name(entity: str, code: str) -> None:
    before = set(_store.list_ids())
    res = client.post("/bpmn-import", json={"xml": _export_with(entity)})
    assert res.status_code == 422, res.text
    assert _codes(res) == [code]
    assert res.json()["detail"]["findings"][0]["params"]["name"] == entity
    assert set(_store.list_ids()) == before  # nothing stored


def test_the_bpmn_import_keeps_a_binding_to_a_business_table() -> None:
    res = client.post("/bpmn-import", json={"xml": _export_with("public.kunden")})
    assert res.status_code == 201, res.text
    assert res.json()["data_elements"]["kundenname"]["select"]["entity"] == "public.kunden"


def test_a_stored_draft_with_such_a_binding_cannot_be_released() -> None:
    """A draft stored before the rule existed: release names the binding."""

    legacy = _with_select(staffed(_bound_schema("legacy_names")), entity="main.sqlite_master")
    plant(_store, legacy)
    res = client.post("/schemas/legacy_names/release")
    assert res.status_code == 422, res.text
    assert "C10.system-entity" in _codes(res)
    assert _store.get("legacy_names").lifecycle_state.value == "ENTWURF"


def test_the_sample_read_refuses_dbstat_although_it_has_no_system_prefix(
    connector: str,
) -> None:
    res = client.post(
        f"/v1/connectors/{connector}/sample-read", json={"entity": "main.dbstat", "limit": 1}
    )
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "DAL.system-entity"


def test_a_stored_draft_with_such_a_binding_is_repaired_by_binding_again() -> None:
    """No dead end: the draft stays editable (an edit is refused only for
    findings it adds), the old name keeps the release locked, and binding the
    element anew replaces it."""

    legacy = _with_select(_bound_schema("legacy_fix"), entity="main.sqlite_master")
    plant(_store, legacy)
    step = next(n.id for n in legacy.nodes.values() if n.label == "Erfassen")
    renamed = client.patch(f"/schemas/legacy_fix/nodes/{step}", json={"label": "Neu"})
    assert renamed.status_code == 200, renamed.text
    assert [f.code for f in validate(_store.get("legacy_fix"))] == ["C10.system-entity"]
    locked = client.post("/schemas/legacy_fix/release")
    assert locked.status_code == 422, locked.text
    assert "C10.system-entity" in _codes(locked)
    res = client.post(
        "/schemas/legacy_fix/data-elements/kundenname/sql-select", json=_select_body()
    )
    assert res.status_code == 200, res.text
    assert validate(_store.get("legacy_fix")) == []
