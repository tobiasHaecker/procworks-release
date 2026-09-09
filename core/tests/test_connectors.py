# SPDX-License-Identifier: BUSL-1.1
"""Data connector correctness + Data Access Layer tests (Section 9, step 12).

An EXTERNAL data element is resolved through a registered connector. Binding it
must reference a registered connector (C1), use an existing INSTANCE element as
its lookup key (C2), and name a non-empty entity (C3). The Data Access Layer
routes parameterized read/write/query accesses to the bound connector.
"""

from __future__ import annotations

import pytest

from procworks import (
    AccessMode,
    ConnectorKind,
    DataAccessError,
    DataAccessLayer,
    DataSourceKind,
    DataType,
    ExternalBinding,
    InMemoryConnector,
    ProcessSchema,
    add_data_element,
    bind_external_data,
    connect_data,
    create_empty_schema,
    register_connector,
    serial_insert,
    validate,
)
from procworks.validator import CorrectnessError


def _schema_with_key() -> ProcessSchema:
    schema = create_empty_schema("Conn", schema_id="conn")
    schema = register_connector(schema, "ERP", ConnectorKind.MS_SQL, connector_id="erp")
    schema = add_data_element(schema, "kunden_nr", DataType.STRING, element_id="key")
    schema = add_data_element(schema, "kunde", DataType.STRING, element_id="kunde")
    return schema


# --- model + operations --------------------------------------------------


def test_register_connector_stores_descriptor() -> None:
    schema = create_empty_schema("Conn", schema_id="conn")
    schema = register_connector(schema, "ERP", ConnectorKind.SAP, connector_id="erp")
    assert schema.connectors["erp"].kind is ConnectorKind.SAP


def test_register_connector_rejects_duplicate() -> None:
    schema = create_empty_schema("Conn", schema_id="conn")
    schema = register_connector(schema, "ERP", ConnectorKind.SAP, connector_id="erp")
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        register_connector(schema, "ERP2", ConnectorKind.MYSQL, connector_id="erp")


def test_bind_external_data_sets_source() -> None:
    schema = _schema_with_key()
    schema = bind_external_data(
        schema, "kunde", connector_id="erp", entity="Kunde", key_element_id="key"
    )
    element = schema.data_elements["kunde"]
    assert element.source is DataSourceKind.EXTERNAL
    assert element.external == ExternalBinding(
        connector_id="erp", entity="Kunde", key_element_id="key"
    )
    assert validate(schema) == []


def test_bind_external_data_unknown_element() -> None:
    schema = _schema_with_key()
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        bind_external_data(
            schema, "ghost", connector_id="erp", entity="Kunde", key_element_id="key"
        )


# --- validator C1-C3 -----------------------------------------------------


def test_c1_unknown_connector() -> None:
    schema = _schema_with_key()
    with pytest.raises(CorrectnessError) as exc:
        bind_external_data(
            schema, "kunde", connector_id="nope", entity="Kunde", key_element_id="key"
        )
    assert any(f.rule == "C1" for f in exc.value.findings)


def test_c1_instance_element_must_not_carry_binding() -> None:
    schema = _schema_with_key()
    schema.data_elements["kunde"].external = ExternalBinding(
        connector_id="erp", entity="Kunde", key_element_id="key"
    )
    findings = validate(schema)
    assert any(f.rule == "C1" for f in findings)


def test_c1_external_without_binding() -> None:
    schema = _schema_with_key()
    schema.data_elements["kunde"].source = DataSourceKind.EXTERNAL
    findings = validate(schema)
    assert any(f.rule == "C1" for f in findings)


def test_c2_unknown_key_element() -> None:
    schema = _schema_with_key()
    with pytest.raises(CorrectnessError) as exc:
        bind_external_data(
            schema, "kunde", connector_id="erp", entity="Kunde", key_element_id="ghost"
        )
    assert any(f.rule == "C2" for f in exc.value.findings)


def test_c2_self_key() -> None:
    schema = _schema_with_key()
    with pytest.raises(CorrectnessError) as exc:
        bind_external_data(
            schema, "kunde", connector_id="erp", entity="Kunde", key_element_id="kunde"
        )
    assert any(f.rule == "C2" for f in exc.value.findings)


def test_c2_external_key_rejected() -> None:
    schema = _schema_with_key()
    schema = add_data_element(schema, "auftrag", DataType.STRING, element_id="auftrag")
    schema = bind_external_data(
        schema, "kunde", connector_id="erp", entity="Kunde", key_element_id="key"
    )
    # 'kunde' is now EXTERNAL; using it as another element's key violates C2.
    with pytest.raises(CorrectnessError) as exc:
        bind_external_data(
            schema,
            "auftrag",
            connector_id="erp",
            entity="Auftrag",
            key_element_id="kunde",
        )
    assert any(f.rule == "C2" for f in exc.value.findings)


def test_c3_empty_entity() -> None:
    schema = _schema_with_key()
    with pytest.raises(CorrectnessError) as exc:
        bind_external_data(
            schema, "kunde", connector_id="erp", entity="   ", key_element_id="key"
        )
    assert any(f.rule == "C3" for f in exc.value.findings)


# --- Data Access Layer ---------------------------------------------------


def _external_schema() -> ProcessSchema:
    schema = _schema_with_key()
    return bind_external_data(
        schema, "kunde", connector_id="erp", entity="Kunde", key_element_id="key"
    )


def test_dal_read_writes_via_connector() -> None:
    schema = _external_schema()
    connector = InMemoryConnector()
    connector.write("Kunde", "K-1", {"name": "Erika"})
    dal = DataAccessLayer()
    dal.register("erp", connector)
    record = dal.read(schema, {"key": "K-1"}, "kunde")
    assert record == {"name": "Erika"}


def test_dal_write_then_read_roundtrip() -> None:
    schema = _external_schema()
    dal = DataAccessLayer()
    dal.register("erp", InMemoryConnector())
    dal.write(schema, {"key": "K-2"}, "kunde", {"name": "Max"})
    assert dal.read(schema, {"key": "K-2"}, "kunde") == {"name": "Max"}


def test_dal_query_filters_records() -> None:
    schema = _external_schema()
    connector = InMemoryConnector()
    connector.write("Kunde", "K-1", {"name": "Erika", "ort": "Ulm"})
    connector.write("Kunde", "K-2", {"name": "Max", "ort": "Berlin"})
    dal = DataAccessLayer()
    dal.register("erp", connector)
    hits = dal.query(schema, "kunde", {"ort": "Ulm"})
    assert hits == [{"name": "Erika", "ort": "Ulm"}]


def test_dal_missing_key_raises() -> None:
    schema = _external_schema()
    dal = DataAccessLayer()
    dal.register("erp", InMemoryConnector())
    with pytest.raises(DataAccessError):
        dal.read(schema, {}, "kunde")


def test_dal_unregistered_connector_raises() -> None:
    schema = _external_schema()
    dal = DataAccessLayer()
    with pytest.raises(DataAccessError):
        dal.read(schema, {"key": "K-1"}, "kunde")


def test_dal_non_external_element_raises() -> None:
    schema = _schema_with_key()
    dal = DataAccessLayer()
    dal.register("erp", InMemoryConnector())
    with pytest.raises(DataAccessError):
        dal.read(schema, {"key": "K-1"}, "kunde")


# --- D1 / C2: connector-supplied elements are readable as mandatory inputs ---
#
# An EXTERNAL element bound for reading has no process step that writes it --
# the DAL resolves it from the connector. Demanding a prior mandatory write (D1)
# made such an element impossible to read as a mandatory input, so the whole
# connector-read feature could only be modelled with optional reads. Its supply
# guarantee is carried by the lookup key instead (concept 9.2).


def _readable_external(*, write_key: bool) -> tuple[ProcessSchema, str]:
    """Key writer, then a reader of the record-bound external element."""

    schema = _schema_with_key()
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    writer = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    if write_key:
        schema = connect_data(schema, writer, "key", AccessMode.WRITE)
    schema = bind_external_data(
        schema, "kunde", connector_id="erp", entity="Kunde", key_element_id="key"
    )
    schema = serial_insert(schema, "Anzeigen", after_node_id=writer)
    reader = next(n.id for n in schema.nodes.values() if n.label == "Anzeigen")
    return schema, reader


def test_mandatory_read_of_connector_supplied_element_is_allowed() -> None:
    schema, reader = _readable_external(write_key=True)
    schema = connect_data(schema, reader, "kunde", AccessMode.READ, mandatory=True)
    assert validate(schema) == []


def test_external_read_requires_the_lookup_key_before_every_reader() -> None:
    """C2 coupling -- the counterpart C5 already enforces for scalar selects."""

    schema, reader = _readable_external(write_key=False)
    with pytest.raises(CorrectnessError, match=r"\[C2\]") as exc:
        connect_data(schema, reader, "kunde", AccessMode.READ, mandatory=True)
    assert any("lookup key" in f.message for f in exc.value.findings), exc.value.findings


def test_optional_external_read_also_requires_the_lookup_key() -> None:
    """A missing key is a runtime *error*, not an absent value -- so an optional
    read does not excuse it (same scope as C5)."""

    schema, reader = _readable_external(write_key=False)
    with pytest.raises(CorrectnessError, match=r"\[C2\]"):
        connect_data(schema, reader, "kunde", AccessMode.READ, mandatory=False)
