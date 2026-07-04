# SPDX-License-Identifier: BUSL-1.1
"""Connection registry and secret store for real data connectors (roadmap P3).

The schema only ever carries connector *metadata* (``ConnectorDescriptor``:
``id``, ``name``, ``kind``). The actual connection details -- URL/DSN, account,
and the secret -- live **server-side** in this registry, sourced from a small
secret store. This keeps credentials out of the model and out of version
control (concept §7.3).

A :class:`ConnectionConfig` describes one connector technically; its ``url`` may
embed ``${ENV_VAR}`` placeholders that are resolved from the process environment
at connect time, so secrets are never stored inline. The registry builds and
caches the real :class:`~procworks.dal.Connector` lazily on first use and can
expose a fully wired :class:`~procworks.dal.DataAccessLayer` for the boundary
runtime's Pre-Fetch/Post-Flush.
"""

from __future__ import annotations

import json
import os
import re

from pydantic import BaseModel, Field
from sqlalchemy import create_engine

from procworks.dal import (
    Connector,
    DataAccessError,
    DataAccessLayer,
    Record,
    SqlAlchemyConnector,
)
from procworks.model import ConnectorKind, SqlSelectBinding, SqlWriteBinding
from procworks.odata import ODataConnector

#: ``${ENV_VAR}`` secret reference inside a connection URL/DSN.
_SECRET_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Environment variable naming the connection-config source (a JSON file path or
#: inline JSON array of :class:`ConnectionConfig`).
_CONNECTIONS_ENV = "PROCWORKS_CONNECTIONS"


def _resolve_secrets(value: str) -> str:
    """Replace ``${VAR}`` references in ``value`` with environment secrets."""

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        secret = os.environ.get(name)
        if secret is None:
            raise DataAccessError(f"secret '{name}' is not set in the environment")
        return secret

    return _SECRET_REF.sub(repl, value)


class ConnectionConfig(BaseModel):
    """Server-side technical configuration of one registered connector.

    ``connector_id`` matches the schema's :class:`ConnectorDescriptor`; ``url``
    is a SQLAlchemy URL whose secret parts may be ``${ENV}`` references.
    ``key_column`` is the default primary-key column used to address a record,
    with optional per-entity overrides in ``entity_key_columns``.
    """

    connector_id: str
    kind: ConnectorKind = ConnectorKind.CUSTOM
    url: str
    key_column: str = "id"
    entity_key_columns: dict[str, str] = Field(default_factory=dict)
    #: For OData connectors (Dynamics 365 / SAP Gateway): the name of the
    #: environment variable holding the bearer token (never the token itself).
    token_env: str = ""


class _LazyConnector:
    """A :class:`~procworks.dal.Connector` proxy that builds on first access.

    Registered into a :class:`~procworks.dal.DataAccessLayer` so engines are only
    created for connectors that are actually used during a request.
    """

    def __init__(self, registry: ConnectionRegistry, connector_id: str) -> None:
        self._registry = registry
        self._connector_id = connector_id

    def read(self, entity: str, key: object) -> Record:
        return self._registry.connector(self._connector_id).read(entity, key)

    def write(self, entity: str, key: object, values: Record) -> None:
        self._registry.connector(self._connector_id).write(entity, key, values)

    def query(self, entity: str, filters: Record) -> list[Record]:
        return self._registry.connector(self._connector_id).query(entity, filters)

    def select_scalar(
        self, binding: SqlSelectBinding, key_values: Record
    ) -> object:
        connector = self._registry.connector(self._connector_id)
        method = getattr(connector, "select_scalar", None)
        if not callable(method):
            raise DataAccessError(
                f"connector '{self._connector_id}' does not support scalar SQL selects"
            )
        result: object = method(binding, key_values)
        return result

    def update_scalar(
        self, binding: SqlWriteBinding, value: object, key_values: Record
    ) -> int:
        connector = self._registry.connector(self._connector_id)
        method = getattr(connector, "update_scalar", None)
        if not callable(method):
            raise DataAccessError(
                f"connector '{self._connector_id}' does not support scalar SQL writes"
            )
        result: int = method(binding, value, key_values)
        return result


class ConnectionRegistry:
    """Maps ``connector_id`` to a built, cached :class:`Connector`."""

    def __init__(self) -> None:
        self._configs: dict[str, ConnectionConfig] = {}
        self._cache: dict[str, Connector] = {}

    def register(self, config: ConnectionConfig) -> None:
        """Add or replace a connection config (drops any cached connector)."""

        self._configs[config.connector_id] = config
        self._cache.pop(config.connector_id, None)

    def configs(self) -> list[ConnectionConfig]:
        """Return all registered configs (no secrets are materialised here)."""

        return list(self._configs.values())

    def has(self, connector_id: str) -> bool:
        return connector_id in self._configs

    def connector(self, connector_id: str) -> Connector:
        """Return the built connector, creating and caching it on first use."""

        cached = self._cache.get(connector_id)
        if cached is not None:
            return cached
        config = self._configs.get(connector_id)
        if config is None:
            raise DataAccessError(f"connector '{connector_id}' is not configured")
        connector = self._build(config)
        self._cache[connector_id] = connector
        return connector

    @staticmethod
    def _build(config: ConnectionConfig) -> Connector:
        if config.kind in (ConnectorKind.DYNAMICS_365, ConnectorKind.SAP):
            token: str | None = None
            if config.token_env:
                token = os.environ.get(config.token_env)
                if token is None:
                    raise DataAccessError(
                        f"secret '{config.token_env}' is not set in the environment"
                    )
            return ODataConnector(_resolve_secrets(config.url), token=token)
        engine = create_engine(_resolve_secrets(config.url))
        return SqlAlchemyConnector(
            engine,
            key_column=config.key_column,
            entity_key_columns=config.entity_key_columns,
        )

    def test(self, connector_id: str) -> None:
        """Run a read-only connection check (no secrets are revealed)."""

        connector = self.connector(connector_id)
        ping = getattr(connector, "ping", None)
        if callable(ping):
            ping()

    def sample_read(self, connector_id: str, entity: str, *, limit: int = 1) -> list[Record]:
        """Return up to ``limit`` sample records for GUI mapping help."""

        rows = self.connector(connector_id).query(entity, {})
        return rows[: max(0, limit)]

    def columns(self, connector_id: str, entity: str) -> list[dict[str, object]]:
        """Reflect a connector entity's columns for the GUI mapping assistant."""

        connector = self.connector(connector_id)
        method = getattr(connector, "columns", None)
        if not callable(method):
            raise DataAccessError(
                f"connector '{connector_id}' does not support column introspection"
            )
        result: list[dict[str, object]] = method(entity)
        return result

    def data_access_layer(self) -> DataAccessLayer:
        """Return a DAL wired with a lazy connector per registered config."""

        dal = DataAccessLayer()
        for connector_id in self._configs:
            dal.register(connector_id, _LazyConnector(self, connector_id))
        return dal


def _load_source(raw: str) -> str:
    """Return the connection-config JSON, from a file path or inline JSON."""

    if os.path.isfile(raw):
        with open(raw, encoding="utf-8") as handle:
            return handle.read()
    return raw


def build_connection_registry() -> ConnectionRegistry:
    """Build the registry from ``PROCWORKS_CONNECTIONS`` (file path or JSON).

    Returns an empty registry when the variable is unset, so the kernel runs
    fully in-memory by default. The source is a JSON array of connection configs;
    secret values stay as ``${ENV}`` references and are resolved only at connect
    time.
    """

    registry = ConnectionRegistry()
    raw = os.environ.get(_CONNECTIONS_ENV, "").strip()
    if not raw:
        return registry
    data = json.loads(_load_source(raw))
    if not isinstance(data, list):
        raise DataAccessError(f"{_CONNECTIONS_ENV} must be a JSON array of connections")
    for item in data:
        registry.register(ConnectionConfig.model_validate(item))
    return registry
