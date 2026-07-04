# SPDX-License-Identifier: BUSL-1.1
"""Persistence tests for the SQLAlchemy store (using a SQLite file backend).

These verify round-tripping of a schema (including a nested XOR block) and the
store interface (put/get/list_ids), so the same code path works against
PostgreSQL in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from procworks import (
    AccessMode,
    BranchSpec,
    DataType,
    add_data_element,
    complete_activity,
    conditional_insert,
    connect_data,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
    worklist,
)
from procworks.audit import EventType, InMemoryAuditLog, create_audit_log
from procworks.db import (
    SqlAlchemyAuditLog,
    SqlAlchemyInstanceStore,
    SqlAlchemySchemaStore,
)
from procworks.execution import ExecutionContext
from procworks.model import InstanceState, LifecycleState, NodeType
from procworks.store import (
    InMemoryInstanceStore,
    InMemorySchemaStore,
    create_instance_store,
    create_store,
)


@pytest.fixture()
def store(tmp_path: Path) -> SqlAlchemySchemaStore:
    url = f"sqlite:///{tmp_path / 'schemas.db'}"
    return SqlAlchemySchemaStore(url, create_tables=True)


def test_put_get_roundtrip_preserves_structure(store: SqlAlchemySchemaStore) -> None:
    schema = create_empty_schema("Persistenz", schema_id="s1")
    schema = serial_insert(schema, "Antrag pruefen", after_node_id="start")
    pruefen = next(n.id for n in schema.nodes.values() if n.label == "Antrag pruefen")
    schema = add_data_element(schema, "betrag", DataType.INTEGER, element_id="betrag")
    schema = connect_data(schema, pruefen, "betrag", AccessMode.WRITE)
    schema = conditional_insert(
        schema,
        after_node_id=pruefen,
        discriminator="betrag",
        branches=[
            BranchSpec(label="Freigabe Leitung", upper=1001),
            BranchSpec(label="Freigabe Team"),
        ],
    )
    store.put(schema)

    loaded = store.get("s1")
    assert loaded is not None
    assert loaded.id == schema.id
    assert loaded.name == schema.name
    assert set(loaded.nodes) == set(schema.nodes)
    assert len(loaded.edges) == len(schema.edges)
    conditions = {e.condition for e in loaded.edges if e.condition is not None}
    assert conditions == {"betrag >= 1001", "betrag < 1001"}


def test_put_is_upsert_and_tracks_lifecycle(store: SqlAlchemySchemaStore) -> None:
    schema = create_empty_schema("Lifecycle", schema_id="s2")
    store.put(schema)
    assert store.get("s2").lifecycle_state is LifecycleState.ENTWURF  # type: ignore[union-attr]

    released = release(schema)
    store.put(released)
    reloaded = store.get("s2")
    assert reloaded is not None
    assert reloaded.lifecycle_state is LifecycleState.RELEASED


def test_get_unknown_returns_none(store: SqlAlchemySchemaStore) -> None:
    assert store.get("missing") is None


def test_list_ids(store: SqlAlchemySchemaStore) -> None:
    store.put(create_empty_schema("A", schema_id="a"))
    store.put(create_empty_schema("B", schema_id="b"))
    assert set(store.list_ids()) == {"a", "b"}


def test_create_store_defaults_to_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert isinstance(create_store(), InMemorySchemaStore)


def test_create_store_uses_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'factory.db'}")
    store = create_store()
    assert isinstance(store, SqlAlchemySchemaStore)
    store.put(create_empty_schema("Factory", schema_id="f1"))
    assert store.get("f1") is not None


@pytest.fixture()
def instance_store(tmp_path: Path) -> SqlAlchemyInstanceStore:
    url = f"sqlite:///{tmp_path / 'instances.db'}"
    return SqlAlchemyInstanceStore(url, create_tables=True)


def _released_serial() -> object:
    schema = create_empty_schema("Lauf", schema_id="run")
    schema = serial_insert(schema, "Schritt", after_node_id="start")
    return release(schema)


def test_instance_roundtrip_preserves_markings(
    instance_store: SqlAlchemyInstanceStore,
) -> None:
    schema = _released_serial()
    instance = instantiate(schema)
    instance_store.put(instance)

    loaded = instance_store.get(instance.id)
    assert loaded is not None
    assert loaded.id == instance.id
    assert loaded.schema_id == "run"
    assert loaded.state is InstanceState.RUNNING
    assert loaded.node_states == instance.node_states
    assert loaded.edge_states == instance.edge_states


def test_instance_put_is_upsert_and_tracks_state(
    instance_store: SqlAlchemyInstanceStore,
) -> None:
    schema = _released_serial()
    instance = instantiate(schema)
    instance_store.put(instance)

    act = next(n.id for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    finished = complete_activity(instance, schema, act)
    instance_store.put(finished)

    reloaded = instance_store.get(instance.id)
    assert reloaded is not None
    assert reloaded.state is InstanceState.COMPLETED


def test_instance_get_unknown_returns_none(
    instance_store: SqlAlchemyInstanceStore,
) -> None:
    assert instance_store.get("missing") is None


def test_durable_store_drives_an_instance_to_completion(
    instance_store: SqlAlchemyInstanceStore,
) -> None:
    schema = _released_serial()
    resolver = lambda sid, ver: schema if sid == schema.id else None  # noqa: E731
    context = ExecutionContext(resolver, instance_store)

    inst = instantiate(schema, context=context)
    act = worklist(inst, schema)[0]
    complete_activity(inst, schema, act, context=context)

    persisted = instance_store.get(inst.id)
    assert persisted is not None
    assert persisted.state is InstanceState.COMPLETED


def test_create_instance_store_defaults_to_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert isinstance(create_instance_store(), InMemoryInstanceStore)


def test_create_instance_store_uses_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'inst_factory.db'}")
    store = create_instance_store()
    assert isinstance(store, SqlAlchemyInstanceStore)
    instance = instantiate(_released_serial())
    store.put(instance)
    assert store.get(instance.id) is not None


@pytest.fixture()
def audit_log(tmp_path: Path) -> SqlAlchemyAuditLog:
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    return SqlAlchemyAuditLog(url, create_tables=True)


def test_audit_append_assigns_monotonic_seq(audit_log: SqlAlchemyAuditLog) -> None:
    first = audit_log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    second = audit_log.append(
        EventType.ACTIVITY_COMPLETED, "i1", "s1", node_id="a", label="A", agent_id="u1"
    )
    assert first.seq == 1
    assert second.seq == 2
    assert second.node_id == "a"
    assert second.label == "A"
    assert second.agent_id == "u1"


def test_audit_for_instance_filters_and_orders(audit_log: SqlAlchemyAuditLog) -> None:
    audit_log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    audit_log.append(EventType.INSTANCE_CREATED, "i2", "s1")
    audit_log.append(EventType.INSTANCE_COMPLETED, "i1", "s1")

    timeline = audit_log.for_instance("i1")
    assert [e.event_type for e in timeline] == [
        EventType.INSTANCE_CREATED,
        EventType.INSTANCE_COMPLETED,
    ]
    assert [e.seq for e in timeline] == sorted(e.seq for e in timeline)
    assert audit_log.list_all()[1].instance_id == "i2"


def test_audit_detail_roundtrips(audit_log: SqlAlchemyAuditLog) -> None:
    event = audit_log.append(
        EventType.BRANCH_DECIDED,
        "i1",
        "s1",
        detail={"target_node_id": "n7"},
    )
    reloaded = audit_log.for_instance("i1")[0]
    assert event.detail == {"target_node_id": "n7"}
    assert reloaded.detail == {"target_node_id": "n7"}


def test_create_audit_log_defaults_to_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert isinstance(create_audit_log(), InMemoryAuditLog)


def test_create_audit_log_uses_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'audit_factory.db'}")
    log = create_audit_log()
    assert isinstance(log, SqlAlchemyAuditLog)
    log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    assert len(log.list_all()) == 1

