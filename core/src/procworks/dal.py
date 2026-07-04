# SPDX-License-Identifier: BUSL-1.1
"""Data Access Layer and connector SPI (Section 9, step 12).

EXTERNAL data elements (see ``DataElement.source``) are not stored in the
process instance but resolved against a central database or business
application through a *connector*. The Data Access Layer (DAL) provides a
single, uniform read/write/query interface and routes each access to the
connector a data element is bound to.

Security by design:
  * Credentials/endpoints never live in the schema -- the connector instance
    holds them and is registered server-side.
  * The lookup key and write values are always passed as *parameters*
    (``read(entity, key)`` / ``write(entity, key, values)``); they are never
    concatenated into a query string, so there is no injection surface.

The in-memory connector below is the reference implementation used in tests
and demos; real connectors (MS SQL, MySQL, Dynamics 365, SAP) implement the
same ``Connector`` protocol.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, NamedTuple, Protocol

from procworks.model import (
    AggregateKind,
    Cardinality,
    DataSourceKind,
    DataType,
    ExternalBinding,
    FilterOperator,
    ProcessSchema,
    SqlSelectBinding,
    SqlWriteBinding,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

#: A single external record as returned/accepted by a connector.
Record = Mapping[str, object]

#: A single, unqualified SQL identifier (column / unqualified table name).
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: An entity name, optionally schema-qualified (``schema.table``).
_ENTITY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


class DataAccessError(RuntimeError):
    """Raised when an external data access cannot be resolved or executed."""


class Connector(Protocol):
    """The narrow SPI every data connector implements.

    All accesses are parameterized: ``key`` and ``values`` are data, never
    interpolated into a statement.
    """

    def read(self, entity: str, key: object) -> Record:
        """Return the record of ``entity`` identified by ``key``."""
        ...

    def write(self, entity: str, key: object, values: Record) -> None:
        """Insert or update the record of ``entity`` identified by ``key``."""
        ...

    def query(self, entity: str, filters: Record) -> list[Record]:
        """Return all records of ``entity`` matching every key/value in ``filters``."""
        ...


class InMemoryConnector:
    """A simple dict-backed connector for tests and local demos."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[object, dict[str, object]]] = {}

    def read(self, entity: str, key: object) -> Record:
        try:
            return dict(self._rows[entity][key])
        except KeyError as exc:
            raise DataAccessError(
                f"no record '{key}' in entity '{entity}'"
            ) from exc

    def write(self, entity: str, key: object, values: Record) -> None:
        self._rows.setdefault(entity, {})[key] = dict(values)

    def query(self, entity: str, filters: Record) -> list[Record]:
        rows = self._rows.get(entity, {})
        return [
            dict(row)
            for row in rows.values()
            if all(row.get(field) == value for field, value in filters.items())
        ]


def _safe_identifier(name: str) -> str:
    """Return ``name`` if it is a single safe SQL identifier, else raise.

    Table/column names cannot be passed as bind parameters, so they are the only
    injection surface. Every identifier that reaches a statement is whitelisted
    against a strict pattern (letters/digits/underscore, no quoting tricks) and
    additionally dialect-quoted before interpolation -- values always travel as
    bound parameters.
    """

    if not _IDENTIFIER.match(name):
        raise DataAccessError(f"unsafe SQL identifier '{name}'")
    return name


def _safe_entity(name: str) -> str:
    """Return ``name`` if it is a safe (optionally schema-qualified) entity."""

    if not _ENTITY.match(name):
        raise DataAccessError(f"unsafe SQL entity '{name}'")
    return name


#: SQL text for each structured filter operator (closed whitelist -- filters
#: never carry free-form SQL; values always travel as bound parameters).
_OPERATOR_SQL: dict[FilterOperator, str] = {
    FilterOperator.EQ: "=",
    FilterOperator.NE: "<>",
    FilterOperator.LT: "<",
    FilterOperator.LE: "<=",
    FilterOperator.GT: ">",
    FilterOperator.GE: ">=",
    FilterOperator.LIKE: "LIKE",
    FilterOperator.IN: "IN",
}


class CompiledSelect(NamedTuple):
    """The deterministic result of compiling a :class:`SqlSelectBinding`.

    ``sql`` is the parameterized statement text; ``binds`` maps each bind
    parameter name (``f0``, ``f1``, ...) to the INSTANCE data element that
    supplies its value, in filter order. The runtime (roadmap Q2) resolves the
    actual values -- the compiler itself is DB-free and value-free.
    """

    sql: str
    binds: tuple[tuple[str, str], ...]


def _ansi_quote_identifier(name: str) -> str:
    """Whitelist and ANSI-quote a single identifier (DB-free default quoter)."""

    return '"' + _safe_identifier(name) + '"'


def _ansi_quote_entity(entity: str) -> str:
    """Whitelist and ANSI-quote a (optionally schema-qualified) entity name."""

    _safe_entity(entity)
    return ".".join(_ansi_quote_identifier(part) for part in entity.split("."))


def compile_select(
    binding: SqlSelectBinding,
    *,
    quote_identifier: Callable[[str], str] = _ansi_quote_identifier,
    quote_entity: Callable[[str], str] = _ansi_quote_entity,
) -> CompiledSelect:
    """Compile a structured scalar select into parameterized SQL (§5.1).

    This is the *single* place SQL text is produced from a select skizze, and it
    is deterministic and DB-free: it never touches a connection and never binds a
    value. Identifiers are whitelisted (``_safe_identifier``/``_safe_entity``)
    and quoted; filter values are emitted as bind placeholders (``:f0`` ...), so
    there is no injection surface. The default quoters use ANSI double quotes;
    a real connector passes its dialect's quoter (roadmap Q2).

    The statement is only well-formed for a binding that satisfies rules C4-C6;
    it is meant to be compiled from an already-validated schema.
    """

    column = quote_identifier(binding.column)
    if binding.aggregate is AggregateKind.NONE:
        projection = column
    else:
        projection = f"{binding.aggregate.value}({column})"

    binds: list[tuple[str, str]] = []
    where_parts: list[str] = []
    for index, item in enumerate(binding.filters):
        param = f"f{index}"
        operator = _OPERATOR_SQL[item.operator]
        where_parts.append(f"{quote_identifier(item.column)} {operator} :{param}")
        binds.append((param, item.key_element_id))

    sql = f"SELECT {projection} FROM {quote_entity(binding.entity)}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if binding.cardinality is Cardinality.FIRST_ORDERED and binding.order_by:
        terms = ", ".join(
            f"{quote_identifier(term.column)} DESC"
            if term.descending
            else quote_identifier(term.column)
            for term in binding.order_by
        )
        sql += f" ORDER BY {terms} LIMIT 1"
    return CompiledSelect(sql=sql, binds=tuple(binds))


class CompiledUpdate(NamedTuple):
    """The deterministic result of compiling a :class:`SqlWriteBinding` (Q4).

    ``sql`` is the parameterized ``UPDATE`` text with a ``:val`` placeholder for
    the written value; ``binds`` maps each filter bind name to the INSTANCE data
    element that locates the row. DB-free and value-free, like the select
    compiler.
    """

    sql: str
    binds: tuple[tuple[str, str], ...]


def compile_update(
    binding: SqlWriteBinding,
    *,
    quote_identifier: Callable[[str], str] = _ansi_quote_identifier,
    quote_entity: Callable[[str], str] = _ansi_quote_entity,
) -> CompiledUpdate:
    """Compile a structured scalar write into a parameterized ``UPDATE`` (§7).

    The single place a write statement is produced: the value travels as the
    ``:val`` bind parameter and the row is located by parameterized filters, so
    there is no injection surface. Identifiers are whitelisted and quoted. Only
    a binding that satisfies rules C7-C9 (single-row target) should be compiled.
    """

    binds: list[tuple[str, str]] = []
    where_parts: list[str] = []
    for index, item in enumerate(binding.filters):
        param = f"f{index}"
        operator = _OPERATOR_SQL[item.operator]
        where_parts.append(f"{quote_identifier(item.column)} {operator} :{param}")
        binds.append((param, item.key_element_id))
    sql = f"UPDATE {quote_entity(binding.entity)} SET {quote_identifier(binding.column)} = :val"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    return CompiledUpdate(sql=sql, binds=tuple(binds))


def _sql_type_to_data_type(sql_type: object) -> DataType | None:
    """Map a reflected SQLAlchemy column type onto a ProcWorks data type.

    Returns ``None`` for types that cannot be bound to a scalar data element
    (e.g. binary/JSON columns), so the GUI can grey them out.
    """

    from sqlalchemy import types as sa_types

    if isinstance(sql_type, sa_types.Boolean):
        return DataType.BOOLEAN
    if isinstance(sql_type, sa_types.Integer):
        return DataType.INTEGER
    if isinstance(sql_type, (sa_types.Numeric, sa_types.Float)):
        return DataType.FLOAT
    if isinstance(sql_type, (sa_types.Date, sa_types.DateTime, sa_types.Time)):
        return DataType.DATE
    if isinstance(sql_type, sa_types.String):
        return DataType.STRING
    return None


class SqlAlchemyConnector:
    """A real, parameterized SQL connector built on SQLAlchemy Core.

    Talks to any SQLAlchemy-supported dialect (PostgreSQL, MySQL/MariaDB,
    Microsoft SQL Server, SQLite, ...). The engine carries the credentials and
    is built server-side from the connection registry -- never from the schema.

    Security by design:
      * Lookup keys and written values are always **bound parameters**; they are
        never concatenated into a statement (no injection surface).
      * Table/column **identifiers** are whitelisted against a strict pattern and
        dialect-quoted, so a crafted entity/column name cannot break out either.

    ``key_column`` is the primary-key column used to address a record; per-entity
    overrides may be supplied via ``entity_key_columns``.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        key_column: str = "id",
        entity_key_columns: Mapping[str, str] | None = None,
    ) -> None:
        self._engine = engine
        self._key_column = key_column
        self._entity_key_columns = dict(entity_key_columns or {})

    def _key_col(self, entity: str) -> str:
        return self._entity_key_columns.get(entity, self._key_column)

    def _quote_ident(self, name: str) -> str:
        return self._engine.dialect.identifier_preparer.quote(_safe_identifier(name))

    def _quote_entity(self, entity: str) -> str:
        _safe_entity(entity)
        return ".".join(self._quote_ident(part) for part in entity.split("."))

    def read(self, entity: str, key: object) -> Record:
        from sqlalchemy import text

        table = self._quote_entity(entity)
        key_col = self._quote_ident(self._key_col(entity))
        stmt = text(f"SELECT * FROM {table} WHERE {key_col} = :key")
        with self._engine.connect() as conn:
            row = conn.execute(stmt, {"key": key}).mappings().first()
        if row is None:
            raise DataAccessError(f"no record '{key}' in entity '{entity}'")
        return dict(row)

    def write(self, entity: str, key: object, values: Record) -> None:
        from sqlalchemy import text

        table = self._quote_entity(entity)
        key_name = self._key_col(entity)
        key_col = self._quote_ident(key_name)
        cols = [c for c in values if c != key_name]
        params: dict[str, object] = {f"v_{c}": values[c] for c in cols}
        params["key"] = key
        with self._engine.begin() as conn:
            exists = conn.execute(
                text(f"SELECT 1 FROM {table} WHERE {key_col} = :key"), {"key": key}
            ).first()
            if exists is not None:
                if cols:
                    assignments = ", ".join(
                        f"{self._quote_ident(c)} = :v_{c}" for c in cols
                    )
                    conn.execute(
                        text(f"UPDATE {table} SET {assignments} WHERE {key_col} = :key"),
                        params,
                    )
                return
            insert_cols = ", ".join([key_col, *(self._quote_ident(c) for c in cols)])
            placeholders = ", ".join([":key", *(f":v_{c}" for c in cols)])
            conn.execute(
                text(f"INSERT INTO {table} ({insert_cols}) VALUES ({placeholders})"),
                params,
            )

    def query(self, entity: str, filters: Record) -> list[Record]:
        from sqlalchemy import text

        table = self._quote_entity(entity)
        params: dict[str, object] = {}
        where = ""
        if filters:
            clauses = []
            for i, (field, value) in enumerate(filters.items()):
                placeholder = f"f{i}"
                clauses.append(f"{self._quote_ident(field)} = :{placeholder}")
                params[placeholder] = value
            where = " WHERE " + " AND ".join(clauses)
        with self._engine.connect() as conn:
            rows = conn.execute(text(f"SELECT * FROM {table}{where}"), params).mappings().all()
        return [dict(row) for row in rows]

    def select_scalar(
        self, binding: SqlSelectBinding, key_values: Mapping[str, object]
    ) -> object:
        """Resolve a structured scalar select to a single, typed value (§7).

        Compiles the binding with this connector's dialect quoter (the single
        SQL-producing path) and binds the filter values as parameters -- keyed by
        the INSTANCE element each filter reads. Returns ``None`` when no row
        matches. Only well-formed (C4-C6) bindings should reach this method.
        """

        from sqlalchemy import bindparam, text

        compiled = compile_select(
            binding,
            quote_identifier=self._quote_ident,
            quote_entity=self._quote_entity,
        )
        stmt = text(compiled.sql)
        bind_params: dict[str, object] = {}
        expanding: list[str] = []
        for (param_name, key_element_id), item in zip(
            compiled.binds, binding.filters, strict=True
        ):
            if key_element_id not in key_values:
                raise DataAccessError(
                    f"filter source '{key_element_id}' is not set for the select"
                )
            value = key_values[key_element_id]
            if item.operator is FilterOperator.IN:
                value = list(value) if isinstance(value, (list, tuple, set)) else [value]
                expanding.append(param_name)
            bind_params[param_name] = value
        if expanding:
            stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in expanding))
        with self._engine.connect() as conn:
            row = conn.execute(stmt, bind_params).first()
        return None if row is None else row[0]

    def update_scalar(
        self, binding: SqlWriteBinding, value: object, key_values: Mapping[str, object]
    ) -> int:
        """Write a single typed value back via a parameterized ``UPDATE`` (§7, Q4).

        Compiles the binding with this connector's dialect quoter, binds the
        value as ``:val`` and the row-locating filters as parameters, and returns
        the number of affected rows. Only well-formed (C7-C9) bindings -- which
        target exactly one row -- should reach this method.
        """

        from sqlalchemy import bindparam, text

        compiled = compile_update(
            binding,
            quote_identifier=self._quote_ident,
            quote_entity=self._quote_entity,
        )
        stmt = text(compiled.sql)
        bind_params: dict[str, object] = {"val": value}
        expanding: list[str] = []
        for (param_name, key_element_id), item in zip(
            compiled.binds, binding.filters, strict=True
        ):
            if key_element_id not in key_values:
                raise DataAccessError(
                    f"filter source '{key_element_id}' is not set for the write"
                )
            filter_value = key_values[key_element_id]
            if item.operator is FilterOperator.IN:
                filter_value = (
                    list(filter_value)
                    if isinstance(filter_value, (list, tuple, set))
                    else [filter_value]
                )
                expanding.append(param_name)
            bind_params[param_name] = filter_value
        if expanding:
            stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in expanding))
        with self._engine.begin() as conn:
            result = conn.execute(stmt, bind_params)
        return result.rowcount

    def columns(self, entity: str) -> list[dict[str, object]]:
        """Reflect the columns of ``entity`` for GUI mapping help (§5.2).

        Returns each column's name, its SQL type as text, and the ProcWorks
        :class:`DataType` it maps onto (``None`` when it is not bindable). No row
        data is read -- this is pure schema introspection.
        """

        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.exc import SQLAlchemyError

        _safe_entity(entity)
        parts = entity.split(".")
        schema_name, table = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
        try:
            reflected = sa_inspect(self._engine).get_columns(table, schema=schema_name)
        except SQLAlchemyError as exc:
            raise DataAccessError(f"cannot inspect entity '{entity}': {exc}") from exc
        return [
            {
                "column": column["name"],
                "sql_type": str(column["type"]),
                "data_type": _sql_type_to_data_type(column["type"]),
            }
            for column in reflected
        ]

    def ping(self) -> None:
        """Read-only connection check used by ``/connectors/{id}/test``."""

        from sqlalchemy import text

        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))



class DataAccessLayer:
    """Routes external data accesses of a schema to registered connectors."""

    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector_id: str, connector: Connector) -> None:
        """Make a connector instance available under ``connector_id``."""

        self._connectors[connector_id] = connector

    def connector(self, connector_id: str) -> Connector:
        """Return the registered connector or raise ``DataAccessError``."""

        connector = self._connectors.get(connector_id)
        if connector is None:
            raise DataAccessError(f"connector '{connector_id}' is not registered")
        return connector

    def _binding(self, schema: ProcessSchema, element_id: str) -> ExternalBinding:
        element = schema.data_elements.get(element_id)
        if element is None:
            raise DataAccessError(f"unknown data element '{element_id}'")
        if element.source is not DataSourceKind.EXTERNAL or element.external is None:
            raise DataAccessError(f"data element '{element_id}' is not EXTERNAL")
        return element.external

    def _key(self, binding: ExternalBinding, instance_values: Record) -> object:
        if binding.key_element_id not in instance_values:
            raise DataAccessError(
                f"lookup key '{binding.key_element_id}' is not set in the instance"
            )
        return instance_values[binding.key_element_id]

    def read(
        self, schema: ProcessSchema, instance_values: Record, element_id: str
    ) -> Record:
        """Resolve and read an EXTERNAL element for the given instance values."""

        binding = self._binding(schema, element_id)
        key = self._key(binding, instance_values)
        return self.connector(binding.connector_id).read(binding.entity, key)

    def write(
        self,
        schema: ProcessSchema,
        instance_values: Record,
        element_id: str,
        values: Record,
    ) -> None:
        """Resolve and write an EXTERNAL element for the given instance values."""

        binding = self._binding(schema, element_id)
        key = self._key(binding, instance_values)
        self.connector(binding.connector_id).write(binding.entity, key, values)

    def query(
        self, schema: ProcessSchema, element_id: str, filters: Record
    ) -> list[Record]:
        """Query the entity an EXTERNAL element is bound to."""

        binding = self._binding(schema, element_id)
        return self.connector(binding.connector_id).query(binding.entity, filters)

    def read_scalar(
        self, schema: ProcessSchema, instance_values: Record, element_id: str
    ) -> object:
        """Resolve a scalar-select-bound EXTERNAL element to a single value (§7).

        Collects the filter values from the instance, then delegates to the
        connector's ``select_scalar``. Raises ``DataAccessError`` if the element
        is not scalar-select-bound, a filter source is missing, or the connector
        does not support scalar selects.
        """

        element = schema.data_elements.get(element_id)
        if element is None:
            raise DataAccessError(f"unknown data element '{element_id}'")
        binding = element.select
        if element.source is not DataSourceKind.EXTERNAL or binding is None:
            raise DataAccessError(
                f"data element '{element_id}' is not a scalar SQL select"
            )
        connector = self.connector(binding.connector_id)
        select_scalar = getattr(connector, "select_scalar", None)
        if not callable(select_scalar):
            raise DataAccessError(
                f"connector '{binding.connector_id}' does not support scalar SQL selects"
            )
        key_values: dict[str, object] = {}
        for item in binding.filters:
            if item.key_element_id not in instance_values:
                raise DataAccessError(
                    f"scalar-select filter source '{item.key_element_id}' is not set "
                    f"in the instance"
                )
            key_values[item.key_element_id] = instance_values[item.key_element_id]
        result: object = select_scalar(binding, key_values)
        return result

    def write_scalar(
        self,
        schema: ProcessSchema,
        instance_values: Record,
        element_id: str,
        value: object,
    ) -> int:
        """Write ``value`` back through a scalar-write-bound EXTERNAL element (§7).

        Collects the row-locating filter values from the instance, then delegates
        to the connector's ``update_scalar``. Raises ``DataAccessError`` if the
        element is not scalar-write-bound, a filter source is missing, or the
        connector does not support scalar writes.
        """

        element = schema.data_elements.get(element_id)
        if element is None:
            raise DataAccessError(f"unknown data element '{element_id}'")
        binding = element.write
        if element.source is not DataSourceKind.EXTERNAL or binding is None:
            raise DataAccessError(
                f"data element '{element_id}' is not a scalar SQL write"
            )
        connector = self.connector(binding.connector_id)
        update_scalar = getattr(connector, "update_scalar", None)
        if not callable(update_scalar):
            raise DataAccessError(
                f"connector '{binding.connector_id}' does not support scalar SQL writes"
            )
        key_values: dict[str, object] = {}
        for item in binding.filters:
            if item.key_element_id not in instance_values:
                raise DataAccessError(
                    f"scalar-write filter source '{item.key_element_id}' is not set "
                    f"in the instance"
                )
            key_values[item.key_element_id] = instance_values[item.key_element_id]
        result: int = update_scalar(binding, value, key_values)
        return result
