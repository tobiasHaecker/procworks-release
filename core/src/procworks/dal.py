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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, NamedTuple, Protocol

from procworks.model import (
    AggregateKind,
    Cardinality,
    DataSourceKind,
    DataType,
    ExternalBinding,
    FilterOperator,
    ProcessSchema,
    QueryFilter,
    SqlSelectBinding,
    SqlWriteBinding,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.sql.elements import TextClause

#: A single external record as returned/accepted by a connector.
Record = Mapping[str, object]

#: A single, unqualified SQL identifier (column / unqualified table name).
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: An entity name, optionally schema-qualified (``schema.table``).
_ENTITY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

#: Schemas that hold a database's *system* catalogue (compared in lower case):
#: the ANSI ``information_schema`` (PostgreSQL, MySQL, MS SQL), PostgreSQL's
#: ``pg_catalog``, MS SQL's and MySQL's ``sys``, MySQL's ``mysql`` and
#: ``performance_schema``. Every ``pg_*`` schema is covered by the prefixes.
_SYSTEM_SCHEMAS = frozenset(
    {"information_schema", "pg_catalog", "sys", "mysql", "performance_schema"}
)
#: Name prefixes of system tables, views and schemas (lower case): SQLite's
#: ``sqlite_master``/``sqlite_sequence``/``sqlite_temp_master`` and its
#: table-valued ``pragma_*`` functions (readable like tables), PostgreSQL's
#: reserved ``pg_`` names (``pg_authid``, ``pg_toast``, ``pg_temp_3`` ...).
_SYSTEM_NAME_PREFIXES = ("sqlite_", "pragma_", "pg_")
#: System tables and views that carry *no* system prefix, compared with the
#: table part of a name (lower case). SQLite's virtual table ``dbstat`` lists
#: every table and index of the database file (names, sizes) and is readable
#: as ``main.dbstat``; MS SQL's compatibility views (``sysobjects``,
#: ``syscolumns`` ...) are readable without the ``sys`` schema. A business
#: table merely *starting* like one (``dbstat_archiv``, ``systeme``) stays
#: usable -- only the exact names are refused.
_SYSTEM_TABLE_NAMES = frozenset(
    {
        "dbstat",
        "sysaltfiles", "syscacheobjects", "syscharsets", "syscolumns",
        "syscomments", "sysconfigures", "sysconstraints", "syscurconfigs",
        "sysdatabases", "sysdepends", "sysdevices", "sysfilegroups", "sysfiles",
        "sysforeignkeys", "sysfulltextcatalogs", "sysindexes", "sysindexkeys",
        "syslanguages", "syslockinfo", "syslogins", "sysmembers", "sysmessages",
        "sysobjects", "sysoledbusers", "sysperfinfo", "syspermissions",
        "sysprocesses", "sysprotects", "sysreferences", "sysremotelogins",
        "sysservers", "systypes", "sysusers",
    }
)


class DataAccessError(RuntimeError):
    """Raised when an external data access cannot be resolved or executed."""


class UnsafeIdentifierError(DataAccessError):
    """An entity/column name failed the identifier whitelist.

    A subclass of :class:`DataAccessError`, so every existing handler keeps
    catching it; the boundary can tell it apart, because it is the *caller's*
    input that is wrong (HTTP 422), not the external system (HTTP 502).
    Without it an unsafe entity name in the sample read would come back as
    502 "Bad Gateway". ``code`` tells the two cases apart for the client
    catalog:

    * ``DAL.unsafe-name`` -- not a plain (optionally schema-qualified) SQL
      identifier: quotes, ``;``, comment markers, blanks, three-part names;
    * ``DAL.system-entity`` -- a database system catalogue, which ProcWorks
      never reads or writes (see :func:`check_entity_name`).
    """

    def __init__(self, message: str, *, code: str = "DAL.unsafe-name") -> None:
        """Create the error.

        :param message: technical description (logs, API ``message``).
        :param code: ``DAL.unsafe-name`` (default) or ``DAL.system-entity``.
        """

        super().__init__(message)
        self.code = code


def is_system_entity(name: str) -> bool:
    """Whether ``name`` denotes a database system catalogue.

    Compared case-insensitively (SQL folds unquoted names, so
    ``PG_CATALOG.pg_authid`` is the same table as ``pg_catalog.pg_authid``).
    A name is a system catalogue when its schema part is a system schema
    (:data:`_SYSTEM_SCHEMAS`), any of its parts starts with a system prefix
    (:data:`_SYSTEM_NAME_PREFIXES`) or its table part is one of the
    unprefixed system tables (:data:`_SYSTEM_TABLE_NAMES`, e.g. SQLite's
    ``dbstat``) -- so ``main.sqlite_master``, ``public.pg_shadow`` and
    ``main.dbstat`` are caught as well as the unqualified names.

    :param name: an entity name, qualified (``schema.table``) or not; it need
        not be a valid identifier (the caller checks that separately).
    :returns: ``True`` for a system catalogue, ``False`` otherwise. A business
        table merely *containing* such a word (``system_log``) is no system
        catalogue.
    """

    parts = name.lower().split(".")
    if len(parts) > 1 and parts[0] in _SYSTEM_SCHEMAS:
        return True
    if parts[-1] in _SYSTEM_TABLE_NAMES:
        return True
    return any(part.startswith(_SYSTEM_NAME_PREFIXES) for part in parts)


def check_entity_name(name: str) -> str:
    """Return ``name`` if it may be used as a table; raise otherwise.

    The one check for every table name of external SQL access -- the modelled
    bindings (at modelling time, :mod:`procworks.operations`), the compiled
    statements and the SQL connector (at run time), the sample read and the
    column reflection (at the boundary). Two promises:

    * only a plain identifier, optionally schema-qualified (``kunden``,
      ``public.kunden``), matched over the *whole* string (a trailing newline
      is refused too) -- table names cannot travel as bind parameters, so they
      are the only injection surface;
    * never a system catalogue (:func:`is_system_entity`), whatever its
      spelling or schema qualification.

    :param name: the entity name as given by the caller.
    :returns: ``name`` unchanged.
    :raises UnsafeIdentifierError: code ``DAL.unsafe-name`` for an unsafe name
        (including the empty string), ``DAL.system-entity`` for a system
        catalogue. The unsafe check comes first.
    """

    if not _ENTITY.fullmatch(name):
        raise UnsafeIdentifierError(f"unsafe SQL entity '{name}'", code="DAL.unsafe-name")
    if is_system_entity(name):
        raise UnsafeIdentifierError(
            f"system catalogue entity '{name}' is never accessed", code="DAL.system-entity"
        )
    return name


def check_column_name(name: str) -> str:
    """Return ``name`` if it is a single safe SQL identifier; raise otherwise.

    The column counterpart of :func:`check_entity_name`: letters, digits and
    underscore, not starting with a digit, matched over the whole string; no
    schema qualification.

    :param name: the column name as given by the caller.
    :returns: ``name`` unchanged.
    :raises UnsafeIdentifierError: code ``DAL.unsafe-name`` (also for ``""``).
    """

    if not _IDENTIFIER.fullmatch(name):
        raise UnsafeIdentifierError(f"unsafe SQL identifier '{name}'", code="DAL.unsafe-name")
    return name


class RowCountError(DataAccessError):
    """An access that targets one row found another number of rows.

    The common base of :class:`WriteRowCountError` and
    :class:`ReadRowCountError`: ``code`` (``DAL.no-row``, ``DAL.many-rows``,
    ``DAL.row-count-unknown``) travels to the API answer and the client
    catalogue. A subclass of :class:`DataAccessError`, so every handler of
    connector failures keeps catching it.
    """

    def __init__(self, message: str, *, code: str) -> None:
        """Create the error.

        :param message: technical description (logs, API ``message``); never
            contains row values.
        :param code: one of the ``DAL.*`` codes, for the client catalog.
        """

        super().__init__(message)
        self.code = code


class ReadRowCountError(RowCountError):
    """A read by a unique key found several rows, so no value was taken.

    A ``KEY_UNIQUE`` scalar select and a keyed record read promise one row.
    When the key column is not unique after all, taking the first match would
    hand an arbitrary row to the process -- while the write-back on the same
    key already fails (``DAL.many-rows``). The read fails the same way. No row
    at all is not an error here: the select yields ``None`` (no value) and the
    record read its own "no record" failure, as before.
    """


def _refuse_several_rows(count: int, entity: str) -> None:
    """Raise :class:`ReadRowCountError` when a one-row read found more rows.

    :param count: rows fetched (the callers fetch at most two -- enough to
        tell one from several without reading the whole result).
    :param entity: the read entity, for the message.
    :raises ReadRowCountError: ``DAL.many-rows`` for ``count > 1``.
    """

    if count > 1:
        raise ReadRowCountError(
            f"read of '{entity}' by a unique key matched several rows",
            code="DAL.many-rows",
        )


class WriteRowCountError(RowCountError):
    """A scalar write did not hit exactly one row, so it was not applied.

    A modelled scalar write targets a single row (its filter names a unique
    column, rules C7-C9). Anything else must fail instead of passing as a
    success -- a key without a row used to store the worker's value nowhere
    while task and case counted as done. ``code`` tells the cases apart:

    * ``DAL.no-row`` -- no row matches the key;
    * ``DAL.many-rows`` -- several rows match (the column is not unique);
    * ``DAL.row-count-unknown`` -- a connector could not say how many rows it
      changed (a driver's ``rowcount == -1``); failing closed is the only safe
      reading, since the value may have been stored nowhere.

    A subclass of :class:`DataAccessError`: every existing handler keeps
    catching it (502 at the boundary, the task stays locked).
    """


def _require_one_row(rows: object, entity: str) -> int:
    """Check the one-row contract of a scalar write; return ``1`` or raise.

    :param rows: the number of matching/affected rows a connector reported.
    :param entity: the written entity, for the message.
    :returns: ``1`` when exactly one row was hit.
    :raises WriteRowCountError: ``DAL.no-row`` for 0, ``DAL.many-rows`` for more
        than one, ``DAL.row-count-unknown`` for a negative or non-integer count
        (never read as "no row" and never as success).
    """

    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise WriteRowCountError(
            f"write to '{entity}' reported no usable row count ({rows!r})",
            code="DAL.row-count-unknown",
        )
    if rows == 0:
        raise WriteRowCountError(
            f"write to '{entity}' matched no row", code="DAL.no-row"
        )
    if rows > 1:
        raise WriteRowCountError(
            f"write to '{entity}' matched {rows} rows instead of one",
            code="DAL.many-rows",
        )
    return 1


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
    bound parameters. Delegates to :func:`check_column_name` (the OData
    connector uses it for entity sets, too -- they are single identifiers).
    """

    return check_column_name(name)


def _safe_entity(name: str) -> str:
    """Return ``name`` if it is a safe, non-system SQL entity, else raise.

    Delegates to :func:`check_entity_name`, so every SQL statement the DAL
    builds -- a compiled select or update, a record read/write/query, a column
    reflection -- refuses a system catalogue at run time as well, also for a
    binding that was stored before the modelling check existed.
    """

    return check_entity_name(name)


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


def _compile_filters(
    filters: list[QueryFilter], quote_identifier: Callable[[str], str]
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Compile structured filters into a parameterized ``WHERE`` clause.

    Shared by :func:`compile_select` and :func:`compile_update`, so both
    statement kinds place their filters identically: each filter becomes
    ``<quoted column> <operator> :f<i>`` (operator from the closed
    :data:`_OPERATOR_SQL` whitelist), joined with ``AND``. No value is bound
    here -- ``binds`` maps each placeholder to the INSTANCE element that will
    supply it.

    :param filters: the binding's filters, in declaration order.
    :param quote_identifier: whitelisting quoter for the column names.
    :returns: ``(where, binds)`` -- ``where`` is ``" WHERE ..."`` or ``""``
        when there are no filters.
    """

    binds: list[tuple[str, str]] = []
    where_parts: list[str] = []
    for index, item in enumerate(filters):
        param = f"f{index}"
        operator = _OPERATOR_SQL[item.operator]
        where_parts.append(f"{quote_identifier(item.column)} {operator} :{param}")
        binds.append((param, item.key_element_id))
    where = " WHERE " + " AND ".join(where_parts) if where_parts else ""
    return where, tuple(binds)


def compile_select(
    binding: SqlSelectBinding,
    *,
    quote_identifier: Callable[[str], str] = _ansi_quote_identifier,
    quote_entity: Callable[[str], str] = _ansi_quote_entity,
) -> CompiledSelect:
    """Compile a structured scalar select into parameterized SQL.

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

    where, binds = _compile_filters(binding.filters, quote_identifier)
    sql = f"SELECT {projection} FROM {quote_entity(binding.entity)}{where}"
    if binding.cardinality is Cardinality.FIRST_ORDERED and binding.order_by:
        terms = ", ".join(
            f"{quote_identifier(term.column)} DESC"
            if term.descending
            else quote_identifier(term.column)
            for term in binding.order_by
        )
        sql += f" ORDER BY {terms} LIMIT 1"
    return CompiledSelect(sql=sql, binds=binds)


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
    """Compile a structured scalar write into a parameterized ``UPDATE``.

    The single place a write statement is produced: the value travels as the
    ``:val`` bind parameter and the row is located by parameterized filters, so
    there is no injection surface. Identifiers are whitelisted and quoted. Only
    a binding that satisfies rules C7-C9 (single-row target) should be compiled.
    """

    where, binds = _compile_filters(binding.filters, quote_identifier)
    sql = f"UPDATE {quote_entity(binding.entity)} SET {quote_identifier(binding.column)} = :val"
    return CompiledUpdate(sql=sql + where, binds=binds)


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


def _bind_filters(
    sql: str,
    binds: tuple[tuple[str, str], ...],
    filters: list[QueryFilter],
    key_values: Mapping[str, object],
    purpose: str,
) -> tuple[TextClause, dict[str, object]]:
    """Turn compiled SQL plus filter values into an executable statement.

    Shared by :meth:`SqlAlchemyConnector.select_scalar` and
    :meth:`SqlAlchemyConnector.update_scalar`. Each placeholder of ``binds``
    gets the value of the INSTANCE element it names, always as a bound
    parameter. An ``IN`` filter takes a collection: a single value is wrapped
    into a one-element list and its parameter is declared *expanding*, so
    SQLAlchemy renders one placeholder per element.

    :param sql: the statement text from :func:`compile_select` /
        :func:`compile_update`.
    :param binds: its ``(placeholder, element id)`` pairs, in filter order.
    :param filters: the binding's filters (same order), for the operators.
    :param key_values: the filter values, keyed by element id.
    :param purpose: ``"select"`` or ``"write"``, for the error message.
    :returns: the statement and its parameters (the caller may add more, such
        as the written ``val``).
    :raises DataAccessError: when a filter's source value is missing.
    """

    from sqlalchemy import bindparam, text

    stmt = text(sql)
    bind_params: dict[str, object] = {}
    expanding: list[str] = []
    for (param_name, key_element_id), item in zip(binds, filters, strict=True):
        if key_element_id not in key_values:
            raise DataAccessError(
                f"filter source '{key_element_id}' is not set for the {purpose}"
            )
        value = key_values[key_element_id]
        if item.operator is FilterOperator.IN:
            value = list(value) if isinstance(value, (list, tuple, set)) else [value]
            expanding.append(param_name)
        bind_params[param_name] = value
    if expanding:
        stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in expanding))
    return stmt, bind_params


@contextmanager
def external_system_errors(action: str) -> Iterator[None]:
    """Report a failure of the external SQL system as :class:`DataAccessError`.

    Every statement a connector sends (and building its engine) can fail in the
    driver: system unreachable, login refused, table missing, driver not
    installed. Those surface as ``SQLAlchemyError``, which no caller handled --
    the API answered 500 where an outage of the connected system is a 502 like
    for every other connector failure. Wrapping the statement in this context
    turns the driver error into the one error type every caller already maps.

    The message names the action and the driver's error *class* only. The
    driver's text is deliberately left out: it can carry the connection URL
    (with the password, e.g. when the URL cannot be parsed), host names and
    statement parameters taken from instance data. The original exception
    stays chained (``__cause__``) for server-side diagnosis.

    :param action: what was attempted, e.g. ``"connection check"``; leads the
        message.
    :raises DataAccessError: instead of any ``SQLAlchemyError`` raised inside.
        A :class:`DataAccessError` raised inside (e.g. a
        :class:`WriteRowCountError`) passes through unchanged.
    """

    from sqlalchemy.exc import SQLAlchemyError

    try:
        yield
    except SQLAlchemyError as exc:
        driver_error = getattr(exc, "orig", None) or exc
        raise DataAccessError(
            f"{action}: external system failed ({type(driver_error).__name__})"
        ) from exc


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
        with external_system_errors("read"), self._engine.connect() as conn:
            rows = conn.execute(stmt, {"key": key}).mappings().fetchmany(2)
        if not rows:
            raise DataAccessError(f"no record '{key}' in entity '{entity}'")
        # The key column names one record; several mean it is not unique.
        _refuse_several_rows(len(rows), entity)
        return dict(rows[0])

    def write(self, entity: str, key: object, values: Record) -> None:
        from sqlalchemy import text

        table = self._quote_entity(entity)
        key_name = self._key_col(entity)
        key_col = self._quote_ident(key_name)
        cols = [c for c in values if c != key_name]
        params: dict[str, object] = {f"v_{c}": values[c] for c in cols}
        params["key"] = key
        with external_system_errors("write"), self._engine.begin() as conn:
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
        with external_system_errors("query"), self._engine.connect() as conn:
            rows = conn.execute(text(f"SELECT * FROM {table}{where}"), params).mappings().all()
        return [dict(row) for row in rows]

    def select_scalar(
        self, binding: SqlSelectBinding, key_values: Mapping[str, object]
    ) -> object:
        """Resolve a structured scalar select to a single, typed value.

        Compiles the binding with this connector's dialect quoter (the single
        SQL-producing path) and binds the filter values as parameters -- keyed by
        the INSTANCE element each filter reads. Returns ``None`` when no row
        matches. Only well-formed (C4-C6) bindings should reach this method.

        A ``KEY_UNIQUE`` select without aggregate fetches up to two rows: two
        mean the "unique" column is not, and the read fails with
        :class:`ReadRowCountError` (``DAL.many-rows``) instead of returning the
        first match. ``AGGREGATE`` and ``FIRST_ORDERED`` yield one row by
        construction and are read as before.
        """

        compiled = compile_select(
            binding,
            quote_identifier=self._quote_ident,
            quote_entity=self._quote_entity,
        )
        stmt, bind_params = _bind_filters(
            compiled.sql, compiled.binds, binding.filters, key_values, "select"
        )
        by_key = (
            binding.cardinality is Cardinality.KEY_UNIQUE
            and binding.aggregate is AggregateKind.NONE
        )
        with external_system_errors("scalar select"), self._engine.connect() as conn:
            rows = conn.execute(stmt, bind_params).fetchmany(2 if by_key else 1)
        if by_key:
            _refuse_several_rows(len(rows), binding.entity)
        return rows[0][0] if rows else None

    def update_scalar(
        self, binding: SqlWriteBinding, value: object, key_values: Mapping[str, object]
    ) -> int:
        """Write a single typed value back via a parameterized ``UPDATE`` (Q4).

        Compiles the binding with this connector's dialect quoter, binds the
        value as ``:val`` and the row-locating filters as parameters. Only
        well-formed (C7-C9) bindings -- which target exactly one row -- should
        reach this method, and exactly one row is what it writes:

        * In the same transaction it first **counts** the rows the filters
          match (``SELECT COUNT(*)`` over the very same ``WHERE``). Anything
          but one raises before anything is written. This is what decides --
          not the driver's ``rowcount``, which some drivers report as ``-1``
          (unknown).
        * After the ``UPDATE`` a *known* ``rowcount`` (``>= 0``) must agree (a
          row inserted or deleted in between); otherwise it raises and the
          transaction is rolled back. An unknown one (``-1``) leaves the
          decision with the count.

        :param binding: the scalar write binding (entity, column, filters).
        :param value: the typed value to store.
        :param key_values: filter values, keyed by INSTANCE element id.
        :returns: ``1`` -- the number of rows written.
        :raises WriteRowCountError: no row (``DAL.no-row``) or several rows
            (``DAL.many-rows``) match; nothing is changed.
        :raises DataAccessError: a filter value is missing.
        """

        compiled = compile_update(
            binding,
            quote_identifier=self._quote_ident,
            quote_entity=self._quote_entity,
        )
        stmt, bind_params = _bind_filters(
            compiled.sql, compiled.binds, binding.filters, key_values, "write"
        )
        where, count_binds = _compile_filters(binding.filters, self._quote_ident)
        count_stmt, count_params = _bind_filters(
            f"SELECT COUNT(*) FROM {self._quote_entity(binding.entity)}{where}",
            count_binds,
            binding.filters,
            key_values,
            "write",
        )
        bind_params["val"] = value
        with external_system_errors("scalar write"), self._engine.begin() as conn:
            # Raising inside ``begin()`` rolls the transaction back.
            _require_one_row(conn.execute(count_stmt, count_params).scalar_one(), binding.entity)
            affected = conn.execute(stmt, bind_params).rowcount
            if affected >= 0:
                _require_one_row(affected, binding.entity)
        return 1

    def entities(self) -> list[str]:
        """List the readable entities (tables and views) of the connection.

        Pure schema introspection for the GUI: the mapping assistant and the
        sample read offer these names instead of asking a modeller to guess a
        table name. No row data is read. Views are included because a customer
        integration is regularly exposed as one; both lists are merged and
        sorted so the offer is deterministic.

        Returns an empty list when the dialect cannot reflect names; a genuine
        connection failure surfaces as :class:`DataAccessError` naming only the
        action and the driver's error class (:func:`external_system_errors`) --
        the driver's text carries host and port of the external system, and
        this list is readable for every role with data access.
        """

        from sqlalchemy import inspect as sa_inspect

        with external_system_errors("cannot list entities"):
            inspector = sa_inspect(self._engine)
            names = set(inspector.get_table_names())
            names |= set(inspector.get_view_names())
        return sorted(names)

    def columns(self, entity: str) -> list[dict[str, object]]:
        """Reflect the columns of ``entity`` for GUI mapping help.

        Returns each column's name, its SQL type as text, and the ProcWorks
        :class:`DataType` it maps onto (``None`` when it is not bindable). No row
        data is read -- this is pure schema introspection.

        A failure of the external system is a :class:`DataAccessError` naming
        the entity and the driver's error class only
        (:func:`external_system_errors`), never the driver's text with host
        and port.
        """

        from sqlalchemy import inspect as sa_inspect

        _safe_entity(entity)
        parts = entity.split(".")
        schema_name, table = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
        with external_system_errors(f"cannot inspect entity '{entity}'"):
            reflected = sa_inspect(self._engine).get_columns(table, schema=schema_name)
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

        with external_system_errors("connection check"), self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))


def _filter_values(
    filters: list[QueryFilter], instance_values: Record, kind: str
) -> dict[str, object]:
    """Collect the instance values a scalar binding's filters read.

    Shared by :meth:`DataAccessLayer.read_scalar` and
    :meth:`DataAccessLayer.write_scalar`: every filter names the INSTANCE
    element that supplies its value, and that value must be set before the
    external system is asked.

    :param filters: the binding's filters.
    :param instance_values: the instance's current data values.
    :param kind: ``"scalar-select"`` or ``"scalar-write"``, for the message.
    :returns: the filter values, keyed by element id.
    :raises DataAccessError: when a filter source is not set in the instance.
    """

    key_values: dict[str, object] = {}
    for item in filters:
        if item.key_element_id not in instance_values:
            raise DataAccessError(
                f"{kind} filter source '{item.key_element_id}' is not set in the instance"
            )
        key_values[item.key_element_id] = instance_values[item.key_element_id]
    return key_values


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
        """Resolve a scalar-select-bound EXTERNAL element to a single value.

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
        key_values = _filter_values(binding.filters, instance_values, "scalar-select")
        result: object = select_scalar(binding, key_values)
        return result

    def write_scalar(
        self,
        schema: ProcessSchema,
        instance_values: Record,
        element_id: str,
        value: object,
    ) -> int:
        """Write ``value`` back through a scalar-write-bound EXTERNAL element.

        Collects the row-locating filter values from the instance, then delegates
        to the connector's ``update_scalar``. Raises ``DataAccessError`` if the
        element is not scalar-write-bound, a filter source is missing, or the
        connector does not support scalar writes.

        The one-row contract holds for **every** connector, not only the SQL
        one: the reported count must be exactly ``1``, else
        :class:`WriteRowCountError` (``0`` no row, ``> 1`` several rows, a
        negative or missing count unknown -- failing closed). Returns ``1``.
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
        key_values = _filter_values(binding.filters, instance_values, "scalar-write")
        result: object = update_scalar(binding, value, key_values)
        return _require_one_row(result, binding.entity)
