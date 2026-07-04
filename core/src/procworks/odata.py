# SPDX-License-Identifier: BUSL-1.1
"""OData v4 connector for the structured data layer (roadmap Q5).

Dynamics 365 (Dataverse) and SAP (via SAP Gateway) expose their business objects
as **OData v4** services. This connector fulfils the same narrow SPI as the SQL
connector (``read``/``write``/``query`` plus ``select_scalar``/``update_scalar``/
``columns``) by translating the *same* structured sketch
(:class:`~procworks.model.SqlSelectBinding` / :class:`SqlWriteBinding`) into OData
query options -- ``$select`` / ``$filter`` / ``$orderby`` / ``$top`` / ``$count``
/ ``$apply`` for reads and a keyed ``PATCH`` for writes.

Security by design (mirrors the SQL connector):
  * Column/entity **identifiers** are whitelisted against a strict pattern.
  * Filter **values** are OData-escaped literals (strings single-quoted with
    ``'`` doubled), so a crafted value cannot break out of the ``$filter``.
  * The service URL and bearer token live **server-side** in the connection
    registry, never in the schema.

HTTP uses the standard library (``urllib``) through an injectable transport, so
no new runtime dependency is added and tests run without a network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote

from procworks.dal import DataAccessError, Record, _safe_identifier
from procworks.model import (
    AggregateKind,
    Cardinality,
    DataType,
    FilterOperator,
    QueryFilter,
    SqlSelectBinding,
    SqlWriteBinding,
)

#: OData comparison operators for the scalar filter operators.
_ODATA_OP: dict[FilterOperator, str] = {
    FilterOperator.EQ: "eq",
    FilterOperator.NE: "ne",
    FilterOperator.LT: "lt",
    FilterOperator.LE: "le",
    FilterOperator.GT: "gt",
    FilterOperator.GE: "ge",
}
#: OData aggregation transforms for ``$apply=aggregate(...)`` (COUNT uses $count).
_ODATA_AGG: dict[AggregateKind, str] = {
    AggregateKind.SUM: "sum",
    AggregateKind.MIN: "min",
    AggregateKind.MAX: "max",
    AggregateKind.AVG: "average",
}


def _odata_literal(value: object) -> str:
    """Return ``value`` as an OData literal (the injection-safe value encoding)."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _odata_like(column: str, pattern: str) -> str:
    """Translate a SQL ``LIKE`` pattern into an OData string function."""

    starts = pattern.startswith("%")
    ends = pattern.endswith("%")
    core = _odata_literal(pattern.strip("%"))
    if starts and ends:
        return f"contains({column},{core})"
    if ends:
        return f"startswith({column},{core})"
    if starts:
        return f"endswith({column},{core})"
    return f"{column} eq {core}"


def _odata_clause(item: QueryFilter, value: object) -> str:
    """Build one OData ``$filter`` clause for a structured filter."""

    column = _safe_identifier(item.column)
    if item.operator in _ODATA_OP:
        return f"{column} {_ODATA_OP[item.operator]} {_odata_literal(value)}"
    if item.operator is FilterOperator.IN:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        joined = ",".join(_odata_literal(v) for v in values)
        return f"{column} in ({joined})"
    if item.operator is FilterOperator.LIKE:
        return _odata_like(column, str(value))
    raise DataAccessError(
        f"operator '{item.operator}' is not supported by the OData connector"
    )


def _odata_filter(filters: list[QueryFilter], key_values: Mapping[str, object]) -> str:
    """Join the structured filters into a single OData ``$filter`` expression."""

    clauses: list[str] = []
    for item in filters:
        if item.key_element_id not in key_values:
            raise DataAccessError(
                f"filter source '{item.key_element_id}' is not set for the query"
            )
        clauses.append(_odata_clause(item, key_values[item.key_element_id]))
    return " and ".join(clauses)


def _json_to_data_type(value: object) -> DataType | None:
    """Infer a ProcWorks data type from a JSON value (for column introspection)."""

    if isinstance(value, bool):
        return DataType.BOOLEAN
    if isinstance(value, int):
        return DataType.INTEGER
    if isinstance(value, float):
        return DataType.FLOAT
    if isinstance(value, str):
        return DataType.STRING
    return None


class HttpTransport(Protocol):
    """The minimal HTTP transport the OData connector talks through."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, str]:
        """Perform an HTTP request and return ``(status_code, response_text)``."""
        ...


class UrllibHttpTransport:
    """Default :class:`HttpTransport` backed by the standard library."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, str]:
        req = urllib_request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8")
        except urllib_error.HTTPError as err:
            return err.code, err.read().decode("utf-8", "replace")


class ODataConnector:
    """A connector that resolves the structured sketch against an OData service.

    ``base_url`` is the OData service root; ``token`` (optional) is sent as a
    bearer token. Entity sets are addressed by name and single-valued keys are
    used for keyed reads/writes.
    """

    def __init__(
        self,
        base_url: str,
        *,
        transport: HttpTransport | None = None,
        token: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._transport = transport or UrllibHttpTransport()
        self._token = token
        self._timeout = timeout_s

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        return headers

    def _request(self, method: str, url: str, body: object | None = None) -> str:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        status, text = self._transport.request(
            method,
            url,
            headers=self._headers(json_body=body is not None),
            body=payload,
            timeout=self._timeout,
        )
        if status >= 400:
            raise DataAccessError(
                f"OData {method} {url} failed: HTTP {status}: {text[:200]}"
            )
        return text

    def _rows(self, url: str) -> list[dict[str, object]]:
        data = json.loads(self._request("GET", url))
        value = data.get("value", []) if isinstance(data, dict) else []
        return [
            {k: v for k, v in row.items() if not str(k).startswith("@")}
            for row in value
        ]

    # -- structured scalar read/write -------------------------------------

    def select_scalar(
        self, binding: SqlSelectBinding, key_values: Mapping[str, object]
    ) -> object:
        entity = _safe_identifier(binding.entity)
        column = _safe_identifier(binding.column)
        flt = _odata_filter(binding.filters, key_values)

        if binding.aggregate is AggregateKind.COUNT:
            url = f"{self._base}/{entity}/$count"
            if flt:
                url += "?$filter=" + quote(flt)
            return int(self._request("GET", url).strip())

        if binding.aggregate is not AggregateKind.NONE:
            transform = f"aggregate({column} with {_ODATA_AGG[binding.aggregate]} as val)"
            apply_expr = f"filter({flt})/{transform}" if flt else transform
            rows = self._rows(f"{self._base}/{entity}?$apply=" + quote(apply_expr))
            return rows[0].get("val") if rows else None

        parts = [f"$select={column}", "$top=1"]
        if flt:
            parts.append("$filter=" + quote(flt))
        if binding.cardinality is Cardinality.FIRST_ORDERED and binding.order_by:
            order = ",".join(
                f"{_safe_identifier(o.column)} desc"
                if o.descending
                else _safe_identifier(o.column)
                for o in binding.order_by
            )
            parts.append("$orderby=" + quote(order))
        rows = self._rows(f"{self._base}/{entity}?" + "&".join(parts))
        return rows[0].get(binding.column) if rows else None

    def update_scalar(
        self, binding: SqlWriteBinding, value: object, key_values: Mapping[str, object]
    ) -> int:
        key_value: object | None = None
        for item in binding.filters:
            if item.operator is FilterOperator.EQ and item.column == binding.unique_column:
                if item.key_element_id not in key_values:
                    raise DataAccessError(
                        f"filter source '{item.key_element_id}' is not set for the write"
                    )
                key_value = key_values[item.key_element_id]
                break
        if key_value is None:
            raise DataAccessError(
                "OData scalar write needs an equality filter on the unique column"
            )
        entity = _safe_identifier(binding.entity)
        _safe_identifier(binding.column)
        url = f"{self._base}/{entity}({_odata_literal(key_value)})"
        self._request("PATCH", url, body={binding.column: value})
        return 1

    # -- record SPI --------------------------------------------------------

    def read(self, entity: str, key: object) -> Record:
        url = f"{self._base}/{_safe_identifier(entity)}({_odata_literal(key)})"
        data = json.loads(self._request("GET", url))
        if not isinstance(data, dict):
            raise DataAccessError(f"unexpected OData response for '{entity}'")
        return {k: v for k, v in data.items() if not str(k).startswith("@")}

    def write(self, entity: str, key: object, values: Record) -> None:
        url = f"{self._base}/{_safe_identifier(entity)}({_odata_literal(key)})"
        self._request("PATCH", url, body=dict(values))

    def query(self, entity: str, filters: Record) -> list[Record]:
        url = f"{self._base}/{_safe_identifier(entity)}"
        if filters:
            clauses = " and ".join(
                f"{_safe_identifier(field)} eq {_odata_literal(value)}"
                for field, value in filters.items()
            )
            url += "?$filter=" + quote(clauses)
        return list(self._rows(url))

    def columns(self, entity: str) -> list[dict[str, object]]:
        rows = self._rows(f"{self._base}/{_safe_identifier(entity)}?$top=1")
        if not rows:
            return []
        return [
            {
                "column": column,
                "sql_type": type(value).__name__,
                "data_type": _json_to_data_type(value),
            }
            for column, value in rows[0].items()
        ]

    def ping(self) -> None:
        """Read-only reachability check against the service metadata document."""

        self._request("GET", f"{self._base}/$metadata")
