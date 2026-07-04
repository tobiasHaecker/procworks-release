# SPDX-License-Identifier: BUSL-1.1
"""Structured scalar SQL-select tests: compiler + rules C4-C6 (concept Q0).

All tests here are **DB-free**: the compiler turns a :class:`SqlSelectBinding`
into deterministic, parameterized SQL text without a connection, and the
validator checks that the select's result fits the data element it fills (C4),
its filters are well-formed and supplied in time (C5) and it yields at most one
row (C6). Runtime resolution against a real database is a later roadmap step.
"""

from __future__ import annotations

import pytest

from procworks import (
    AggregateKind,
    Cardinality,
    ConnectorKind,
    DataAccessError,
    DataSourceKind,
    DataType,
    ExternalBinding,
    FilterOperator,
    OrderBy,
    QueryFilter,
    SqlSelectBinding,
    SqlWriteBinding,
    add_data_element,
    aggregate_result_type,
    bind_sql_write,
    compile_select,
    compile_update,
    connect_data,
    create_empty_schema,
    register_connector,
    serial_insert,
    validate,
)
from procworks.model import AccessMode


def _activity_ids(schema, label):  # type: ignore[no-untyped-def]
    return [n.id for n in schema.nodes.values() if n.label == label]


def _valid_binding(**overrides: object) -> SqlSelectBinding:
    """A well-formed KEY_UNIQUE select over "Kunde" projecting a STRING name."""

    spec: dict[str, object] = {
        "connector_id": "erp",
        "entity": "Kunde",
        "column": "name",
        "column_type": DataType.STRING,
        "aggregate": AggregateKind.NONE,
        "filters": [
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        "cardinality": Cardinality.KEY_UNIQUE,
        "order_by": [],
        "unique_column": "kd_id",
    }
    spec.update(overrides)
    return SqlSelectBinding(**spec)  # type: ignore[arg-type]


def _flip_to_select(schema, element_id, binding):  # type: ignore[no-untyped-def]
    """Turn an INSTANCE element into a scalar-select-bound EXTERNAL element."""

    candidate = schema.model_copy(deep=True)
    element = candidate.data_elements[element_id]
    element.source = DataSourceKind.EXTERNAL
    element.external = None
    element.select = binding
    return candidate


def _base_schema():  # type: ignore[no-untyped-def]
    """start -> Erfassen(writes kunden_nr) -> Pruefen(reads kundenname) -> end.

    ``kundenname`` is bound to a valid scalar select; the returned schema is
    fully correct, so negative tests can rebind it and inspect the findings.
    """

    schema = create_empty_schema("SQL", schema_id="sql")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "kundenname", DataType.STRING, element_id="kundenname")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = _activity_ids(schema, "Erfassen")[0]
    schema = serial_insert(schema, "Pruefen", after_node_id=writer)
    reader = _activity_ids(schema, "Pruefen")[0]
    schema = connect_data(schema, writer, "kunden_nr", AccessMode.WRITE)
    schema = _flip_to_select(schema, "kundenname", _valid_binding())
    schema = connect_data(schema, reader, "kundenname", AccessMode.READ, mandatory=False)
    return schema, reader


def _rebind(schema, binding, element_id="kundenname"):  # type: ignore[no-untyped-def]
    candidate = schema.model_copy(deep=True)
    candidate.data_elements[element_id].select = binding
    return candidate


# --- compiler: deterministic, DB-free SQL --------------------------------


def test_compile_key_unique_projects_single_column() -> None:
    compiled = compile_select(_valid_binding())
    assert compiled.sql == 'SELECT "name" FROM "Kunde" WHERE "kd_id" = :f0'
    assert compiled.binds == (("f0", "kunden_nr"),)


def test_compile_quotes_schema_qualified_entity() -> None:
    compiled = compile_select(_valid_binding(entity="dbo.Kunde"))
    assert compiled.sql == 'SELECT "name" FROM "dbo"."Kunde" WHERE "kd_id" = :f0'


def test_compile_aggregate_has_no_binds() -> None:
    compiled = compile_select(
        _valid_binding(
            column="betrag",
            column_type=DataType.FLOAT,
            aggregate=AggregateKind.COUNT,
            cardinality=Cardinality.AGGREGATE,
            unique_column="",
            filters=[],
        )
    )
    assert compiled.sql == 'SELECT COUNT("betrag") FROM "Kunde"'
    assert compiled.binds == ()


def test_compile_first_ordered_adds_order_and_limit() -> None:
    compiled = compile_select(
        _valid_binding(
            cardinality=Cardinality.FIRST_ORDERED,
            unique_column="",
            filters=[
                QueryFilter(
                    column="stadt",
                    column_type=DataType.STRING,
                    operator=FilterOperator.EQ,
                    key_element_id="kunden_nr",
                )
            ],
            order_by=[OrderBy(column="created", descending=True)],
        )
    )
    assert compiled.sql == (
        'SELECT "name" FROM "Kunde" WHERE "stadt" = :f0 '
        'ORDER BY "created" DESC LIMIT 1'
    )


def test_compile_multiple_filters_and_operators() -> None:
    compiled = compile_select(
        _valid_binding(
            filters=[
                QueryFilter(
                    column="kd_id",
                    column_type=DataType.INTEGER,
                    operator=FilterOperator.EQ,
                    key_element_id="a",
                ),
                QueryFilter(
                    column="name",
                    column_type=DataType.STRING,
                    operator=FilterOperator.LIKE,
                    key_element_id="b",
                ),
            ]
        )
    )
    assert compiled.sql == (
        'SELECT "name" FROM "Kunde" WHERE "kd_id" = :f0 AND "name" LIKE :f1'
    )
    assert compiled.binds == (("f0", "a"), ("f1", "b"))


def test_compile_in_operator() -> None:
    compiled = compile_select(
        _valid_binding(
            filters=[
                QueryFilter(
                    column="kd_id",
                    column_type=DataType.INTEGER,
                    operator=FilterOperator.IN,
                    key_element_id="ids",
                )
            ]
        )
    )
    assert compiled.sql == 'SELECT "name" FROM "Kunde" WHERE "kd_id" IN :f0'


def test_compile_uses_custom_dialect_quoters() -> None:
    compiled = compile_select(
        _valid_binding(),
        quote_identifier=lambda name: f"`{name}`",
        quote_entity=lambda entity: f"`{entity}`",
    )
    assert compiled.sql == "SELECT `name` FROM `Kunde` WHERE `kd_id` = :f0"


def test_compile_rejects_unsafe_column() -> None:
    with pytest.raises(DataAccessError):
        compile_select(_valid_binding(column="name; DROP TABLE kunde"))


def test_compile_rejects_unsafe_entity() -> None:
    with pytest.raises(DataAccessError):
        compile_select(_valid_binding(entity="Kunde; DROP"))


# --- aggregate result typing --------------------------------------------


@pytest.mark.parametrize(
    ("aggregate", "column_type", "expected"),
    [
        (AggregateKind.NONE, DataType.STRING, DataType.STRING),
        (AggregateKind.COUNT, DataType.STRING, DataType.INTEGER),
        (AggregateKind.AVG, DataType.INTEGER, DataType.FLOAT),
        (AggregateKind.SUM, DataType.FLOAT, DataType.FLOAT),
        (AggregateKind.MIN, DataType.DATE, DataType.DATE),
        (AggregateKind.MAX, DataType.INTEGER, DataType.INTEGER),
    ],
)
def test_aggregate_result_type(aggregate, column_type, expected) -> None:  # type: ignore[no-untyped-def]
    assert aggregate_result_type(aggregate, column_type) is expected


# --- validator: C4-C6 ----------------------------------------------------


def test_valid_scalar_select_passes() -> None:
    schema, _ = _base_schema()
    assert validate(schema) == []


def test_rules_silent_without_any_select_binding() -> None:
    schema = create_empty_schema("Plain", schema_id="plain")
    assert validate(schema) == []


def test_c4_rejects_projection_type_mismatch() -> None:
    schema, _ = _base_schema()
    schema = _rebind(schema, _valid_binding(column_type=DataType.INTEGER))
    assert any(f.rule == "C4" for f in validate(schema))


def test_c4_count_yields_integer_for_integer_element() -> None:
    schema, _ = _base_schema()
    candidate = schema.model_copy(deep=True)
    candidate.data_elements["kundenname"].data_type = DataType.INTEGER
    candidate = _rebind(
        candidate,
        _valid_binding(
            aggregate=AggregateKind.COUNT,
            cardinality=Cardinality.AGGREGATE,
            unique_column="",
            filters=[],
        ),
    )
    assert validate(candidate) == []


def test_c5_rejects_unknown_connector() -> None:
    schema, _ = _base_schema()
    schema = _rebind(schema, _valid_binding(connector_id="ghost"))
    assert any(f.rule == "C5" for f in validate(schema))


def test_c5_rejects_empty_entity() -> None:
    schema, _ = _base_schema()
    schema = _rebind(schema, _valid_binding(entity="   "))
    assert any(f.rule == "C5" for f in validate(schema))


def test_c5_rejects_empty_column() -> None:
    schema, _ = _base_schema()
    schema = _rebind(schema, _valid_binding(column=""))
    assert any(f.rule == "C5" for f in validate(schema))


def test_c5_rejects_unknown_filter_source() -> None:
    schema, _ = _base_schema()
    binding = _valid_binding(
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="ghost",
            )
        ]
    )
    schema = _rebind(schema, binding)
    assert any(
        f.rule == "C5" and "unknown filter source" in f.message for f in validate(schema)
    )


def test_c5_rejects_non_instance_filter_source() -> None:
    schema, _ = _base_schema()
    binding = _valid_binding(
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.STRING,
                operator=FilterOperator.EQ,
                key_element_id="kundenname",  # EXTERNAL, not INSTANCE
            )
        ]
    )
    schema = _rebind(schema, binding)
    assert any(
        f.rule == "C5" and "must be an INSTANCE" in f.message for f in validate(schema)
    )


def test_c5_rejects_filter_type_mismatch() -> None:
    schema, _ = _base_schema()
    binding = _valid_binding(
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.STRING,  # kunden_nr is INTEGER
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ]
    )
    schema = _rebind(schema, binding)
    assert any(
        f.rule == "C5" and "does not match source" in f.message for f in validate(schema)
    )


def test_c5_rejects_operator_type_mismatch() -> None:
    schema, _ = _base_schema()
    binding = _valid_binding(
        cardinality=Cardinality.FIRST_ORDERED,
        unique_column="",
        order_by=[OrderBy(column="name")],
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.LIKE,  # LIKE only valid on STRING
                key_element_id="kunden_nr",
            )
        ],
    )
    schema = _rebind(schema, binding)
    assert any(
        f.rule == "C5" and "operator LIKE" in f.message for f in validate(schema)
    )


def test_c5_d1_coupling_rejects_filter_source_read_before_written() -> None:
    schema, reader = _base_schema()
    candidate = schema.model_copy(deep=True)
    candidate.data_accesses = [
        a
        for a in candidate.data_accesses
        if not (a.element_id == "kunden_nr" and a.mode is AccessMode.WRITE)
    ]
    findings = validate(candidate)
    assert any(
        f.rule == "C5" and f.node_id == reader and "before it is written" in f.message
        for f in findings
    )


def test_c6_key_unique_requires_unique_column() -> None:
    schema, _ = _base_schema()
    schema = _rebind(schema, _valid_binding(unique_column=""))
    assert any(
        f.rule == "C6" and "no unique column" in f.message for f in validate(schema)
    )


def test_c6_key_unique_requires_equality_filter_on_unique_column() -> None:
    schema, _ = _base_schema()
    binding = _valid_binding(
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.GT,  # not an equality filter
                key_element_id="kunden_nr",
            )
        ]
    )
    schema = _rebind(schema, binding)
    assert any(
        f.rule == "C6" and "no equality filter" in f.message for f in validate(schema)
    )


def test_c6_aggregate_requires_an_aggregate() -> None:
    schema, _ = _base_schema()
    schema = _rebind(
        schema,
        _valid_binding(
            cardinality=Cardinality.AGGREGATE,
            aggregate=AggregateKind.NONE,
            unique_column="",
            filters=[],
        ),
    )
    assert any(
        f.rule == "C6" and "projects a plain column" in f.message for f in validate(schema)
    )


def test_c6_first_ordered_requires_order_by() -> None:
    schema, _ = _base_schema()
    schema = _rebind(
        schema,
        _valid_binding(
            cardinality=Cardinality.FIRST_ORDERED,
            order_by=[],
            unique_column="",
            filters=[],
        ),
    )
    assert any(f.rule == "C6" for f in validate(schema))


def test_c1_rejects_record_and_select_binding_together() -> None:
    schema, _ = _base_schema()
    candidate = schema.model_copy(deep=True)
    candidate.data_elements["kundenname"].external = ExternalBinding(
        connector_id="erp", entity="Kunde", key_element_id="kunden_nr"
    )
    assert any(
        f.rule == "C1" and "exactly one external binding" in f.message
        for f in validate(candidate)
    )


def test_c1_rejects_instance_element_carrying_select() -> None:
    schema = create_empty_schema("SQL", schema_id="sql2")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "wert", DataType.STRING, element_id="wert")
    candidate = schema.model_copy(deep=True)
    candidate.data_elements["wert"].select = _valid_binding()  # source stays INSTANCE
    assert any(f.rule == "C1" for f in validate(candidate))


# --- scalar write-back: compiler + rules C7-C9 (Q4) ----------------------


def _write_binding(**overrides: object) -> SqlWriteBinding:
    spec: dict[str, object] = {
        "connector_id": "erp",
        "entity": "Kunde",
        "column": "status",
        "column_type": DataType.STRING,
        "filters": [
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="kunden_nr",
            )
        ],
        "unique_column": "kd_id",
    }
    spec.update(overrides)
    return SqlWriteBinding(**spec)  # type: ignore[arg-type]


def _write_base_schema(schema_id: str = "sqlw"):  # type: ignore[no-untyped-def]
    """start -> Erfassen(writes kunden_nr) -> Melden(writes status_extern) -> end."""

    schema = create_empty_schema("SQLW", schema_id=schema_id)
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.INTEGER, element_id="kunden_nr")
    schema = add_data_element(schema, "status_extern", DataType.STRING, element_id="status_extern")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = _activity_ids(schema, "Erfassen")[0]
    schema = serial_insert(schema, "Melden", after_node_id=writer)
    reporter = _activity_ids(schema, "Melden")[0]
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
    return schema, reporter


def _rebind_write(schema, binding, element_id="status_extern"):  # type: ignore[no-untyped-def]
    candidate = schema.model_copy(deep=True)
    candidate.data_elements[element_id].write = binding
    return candidate


def test_compile_update_key_unique() -> None:
    compiled = compile_update(_write_binding())
    assert compiled.sql == 'UPDATE "Kunde" SET "status" = :val WHERE "kd_id" = :f0'
    assert compiled.binds == (("f0", "kunden_nr"),)


def test_compile_update_rejects_unsafe_column() -> None:
    with pytest.raises(DataAccessError):
        compile_update(_write_binding(column="status; DROP TABLE kunde"))


def test_valid_scalar_write_passes() -> None:
    schema, _ = _write_base_schema()
    assert validate(schema) == []


def test_scalar_write_silent_without_binding() -> None:
    schema = create_empty_schema("Plain", schema_id="plainw")
    assert validate(schema) == []


def test_c7_rejects_target_type_mismatch() -> None:
    schema, _ = _write_base_schema("c7")
    schema = _rebind_write(schema, _write_binding(column_type=DataType.INTEGER))
    assert any(f.rule == "C7" for f in validate(schema))


def test_c8_rejects_unknown_connector() -> None:
    schema, _ = _write_base_schema("c8a")
    schema = _rebind_write(schema, _write_binding(connector_id="ghost"))
    assert any(f.rule == "C8" for f in validate(schema))


def test_c8_rejects_non_instance_filter_source() -> None:
    schema, _ = _write_base_schema("c8b")
    binding = _write_binding(
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.STRING,
                operator=FilterOperator.EQ,
                key_element_id="status_extern",  # EXTERNAL, not INSTANCE
            )
        ]
    )
    schema = _rebind_write(schema, binding)
    assert any(
        f.rule == "C8" and "must be an INSTANCE" in f.message for f in validate(schema)
    )


def test_c8_d1_coupling_rejects_filter_source_used_before_written() -> None:
    schema, reporter = _write_base_schema("c8c")
    candidate = schema.model_copy(deep=True)
    candidate.data_accesses = [
        a
        for a in candidate.data_accesses
        if not (a.element_id == "kunden_nr" and a.mode is AccessMode.WRITE)
    ]
    findings = validate(candidate)
    assert any(
        f.rule == "C8" and f.node_id == reporter and "before it is written" in f.message
        for f in findings
    )


def test_c9_requires_unique_column() -> None:
    schema, _ = _write_base_schema("c9a")
    schema = _rebind_write(schema, _write_binding(unique_column=""))
    assert any(
        f.rule == "C9" and "no unique column" in f.message for f in validate(schema)
    )


def test_c9_requires_equality_filter_on_unique_column() -> None:
    schema, _ = _write_base_schema("c9b")
    binding = _write_binding(
        filters=[
            QueryFilter(
                column="kd_id",
                column_type=DataType.INTEGER,
                operator=FilterOperator.GT,
                key_element_id="kunden_nr",
            )
        ]
    )
    schema = _rebind_write(schema, binding)
    assert any(
        f.rule == "C9" and "no equality filter" in f.message for f in validate(schema)
    )


def test_c1_rejects_select_and_write_together() -> None:
    schema, _ = _write_base_schema("c1w")
    candidate = schema.model_copy(deep=True)
    candidate.data_elements["status_extern"].select = _valid_binding()
    assert any(
        f.rule == "C1" and "exactly one external binding" in f.message
        for f in validate(candidate)
    )
