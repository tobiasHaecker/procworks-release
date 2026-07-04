# SPDX-License-Identifier: BUSL-1.1
"""PostgreSQL persistence via SQLAlchemy (roadmap step 4).

A process schema is stored as a JSON(B) document plus queryable columns
(``name``, ``version``, ``lifecycle_state``). On PostgreSQL the document column
is JSONB; on SQLite (used by the test suite) it falls back to plain JSON. The
store implements the same interface as the in-memory store, so the API does not
care which backend is active.

Process instances are persisted the same way (durable instance persistence):
one ``process_instance`` row per instance, again as a JSON(B) document plus
queryable columns (``schema_id``, ``schema_version``, ``state``).

The append-only audit/event log (roadmap step 15) is persisted durably here as
well (roadmap step 16): one ``audit_event`` row per recorded runtime event, with
a database-assigned monotonic ``seq`` and queryable columns for filtering.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from procworks.audit import AuditEvent, EventType
from procworks.auth_password import User
from procworks.model import (
    ExternalTask,
    Incident,
    OrgModel,
    OutboxEntry,
    ProcessInstance,
    ProcessSchema,
    WebhookDelivery,
    WebhookSubscription,
)

#: JSONB on PostgreSQL, generic JSON elsewhere (e.g. SQLite in tests).
JsonDocument = JSON().with_variant(JSONB(), "postgresql")



class Base(DeclarativeBase):
    """Declarative base for the persistence models."""


class SchemaRow(Base):
    """One row per process schema (keyed by schema id)."""

    __tablename__ = "process_schema"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemySchemaStore:
    """A schema store backed by a SQLAlchemy engine.

    Parameters
    ----------
    url:
        SQLAlchemy database URL (e.g. ``postgresql+psycopg://user:pw@host/db``
        or ``sqlite:///schemas.db``).
    create_tables:
        If True, create the table from the ORM metadata. Useful for SQLite and
        local development; in production prefer Alembic migrations and pass
        ``create_tables=False``.
    """

    def __init__(self, url: str, *, create_tables: bool = False) -> None:
        self._engine = create_engine(url, future=True)
        if create_tables:
            Base.metadata.create_all(self._engine)

    def put(self, schema: ProcessSchema) -> ProcessSchema:
        payload = schema.model_dump(mode="json")
        with Session(self._engine) as session:
            row = session.get(SchemaRow, schema.id)
            if row is None:
                row = SchemaRow(id=schema.id)
                session.add(row)
            row.version = schema.version
            row.name = schema.name
            row.lifecycle_state = schema.lifecycle_state.value
            row.document = payload
            session.commit()
        return schema

    def get(self, schema_id: str) -> ProcessSchema | None:
        with Session(self._engine) as session:
            row = session.get(SchemaRow, schema_id)
            if row is None:
                return None
            return ProcessSchema.model_validate(row.document)

    def list_ids(self) -> list[str]:
        with Session(self._engine) as session:
            return list(session.scalars(select(SchemaRow.id)))

    def clear(self) -> None:
        with Session(self._engine) as session:
            session.execute(delete(SchemaRow))
            session.commit()


class OrgRow(Base):
    """One row per shared, standalone org model (keyed by org id)."""

    __tablename__ = "org_model"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyOrgStore:
    """A shared-org-model store backed by a SQLAlchemy engine.

    Mirrors ``SqlAlchemySchemaStore``: same engine/URL conventions and the same
    ``put``/``get``/``list_ids`` interface as the in-memory org store, so the
    API is agnostic of the backend.
    """

    def __init__(self, url: str, *, create_tables: bool = False) -> None:
        self._engine = create_engine(url, future=True)
        if create_tables:
            Base.metadata.create_all(self._engine)

    def put(self, org: OrgModel) -> OrgModel:
        if org.id is None:
            raise ValueError("a shared org model must have an id before it is stored")
        payload = org.model_dump(mode="json")
        with Session(self._engine) as session:
            row = session.get(OrgRow, org.id)
            if row is None:
                row = OrgRow(id=org.id)
                session.add(row)
            row.name = org.name
            row.document = payload
            session.commit()
        return org

    def get(self, org_id: str) -> OrgModel | None:
        with Session(self._engine) as session:
            row = session.get(OrgRow, org_id)
            if row is None:
                return None
            return OrgModel.model_validate(row.document)

    def list_ids(self) -> list[str]:
        with Session(self._engine) as session:
            return list(session.scalars(select(OrgRow.id)))

    def clear(self) -> None:
        with Session(self._engine) as session:
            session.execute(delete(OrgRow))
            session.commit()


class InstanceRow(Base):
    """One row per process instance (keyed by instance id)."""

    __tablename__ = "process_instance"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_id: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyInstanceStore:
    """A process-instance store backed by a SQLAlchemy engine.

    Mirrors ``SqlAlchemySchemaStore``: same engine/URL conventions and the same
    ``put``/``get``/``list_ids`` interface as the in-memory instance store, so
    the execution engine and API are agnostic of the backend.
    """

    def __init__(self, url: str, *, create_tables: bool = False) -> None:
        self._engine = create_engine(url, future=True)
        if create_tables:
            Base.metadata.create_all(self._engine)

    def put(self, instance: ProcessInstance) -> ProcessInstance:
        payload = instance.model_dump(mode="json")
        with Session(self._engine) as session:
            row = session.get(InstanceRow, instance.id)
            if row is None:
                row = InstanceRow(id=instance.id)
                session.add(row)
            row.schema_id = instance.schema_id
            row.schema_version = instance.schema_version
            row.state = instance.state.value
            row.document = payload
            session.commit()
        return instance

    def get(self, instance_id: str) -> ProcessInstance | None:
        with Session(self._engine) as session:
            row = session.get(InstanceRow, instance_id)
            if row is None:
                return None
            return ProcessInstance.model_validate(row.document)

    def list_ids(self) -> list[str]:
        with Session(self._engine) as session:
            return list(session.scalars(select(InstanceRow.id)))

    def clear(self) -> None:
        with Session(self._engine) as session:
            session.execute(delete(InstanceRow))
            session.commit()


class AuditEventRow(Base):
    """One row per recorded runtime event (append-only event log)."""

    __tablename__ = "audit_event"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    schema_id: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[dict[str, str]] = mapped_column(JsonDocument, nullable=False)


def _event_from_row(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        seq=row.seq,
        timestamp=row.timestamp,
        event_type=EventType(row.event_type),
        instance_id=row.instance_id,
        schema_id=row.schema_id,
        schema_version=row.schema_version,
        node_id=row.node_id,
        label=row.label,
        agent_id=row.agent_id,
        detail=dict(row.detail),
    )


class SqlAlchemyAuditLog:
    """A durable, append-only event log backed by a SQLAlchemy engine.

    Implements the same ``append``/``list_all``/``for_instance`` interface as
    :class:`procworks.audit.InMemoryAuditLog`, so the API records events the
    same way regardless of the backend. The monotonic ``seq`` is assigned by the
    database (autoincrement primary key); events are returned ordered by ``seq``.
    """

    def __init__(self, url: str, *, create_tables: bool = False) -> None:
        self._engine = create_engine(url, future=True)
        if create_tables:
            Base.metadata.create_all(self._engine)

    def append(
        self,
        event_type: EventType,
        instance_id: str,
        schema_id: str,
        *,
        schema_version: int = 1,
        node_id: str | None = None,
        label: str | None = None,
        agent_id: str | None = None,
        detail: dict[str, str] | None = None,
    ) -> AuditEvent:
        with Session(self._engine) as session:
            row = AuditEventRow(
                timestamp=datetime.now(UTC),
                event_type=event_type.value,
                instance_id=instance_id,
                schema_id=schema_id,
                schema_version=schema_version,
                node_id=node_id,
                label=label,
                agent_id=agent_id,
                detail=detail or {},
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _event_from_row(row)

    def list_all(self) -> list[AuditEvent]:
        with Session(self._engine) as session:
            rows = session.scalars(select(AuditEventRow).order_by(AuditEventRow.seq))
            return [_event_from_row(row) for row in rows]

    def for_instance(self, instance_id: str) -> list[AuditEvent]:
        with Session(self._engine) as session:
            stmt = (
                select(AuditEventRow)
                .where(AuditEventRow.instance_id == instance_id)
                .order_by(AuditEventRow.seq)
            )
            return [_event_from_row(row) for row in session.scalars(stmt)]

    def revision(self) -> int:
        """Return the highest assigned ``seq`` (0 when the log is empty).

        Mirrors :meth:`procworks.audit.InMemoryAuditLog.revision`: a cheap,
        monotonic counter that clients poll to detect new runtime progress.
        """

        with Session(self._engine) as session:
            value = session.scalar(select(func.max(AuditEventRow.seq)))
            return int(value or 0)

    def clear(self) -> None:
        with Session(self._engine) as session:
            session.execute(delete(AuditEventRow))
            session.commit()


class UserRow(Base):
    """One row per login user (durable part of the password auth backend)."""

    __tablename__ = "auth_user"

    login: Mapped[str] = mapped_column(String, primary_key=True)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    roles: Mapped[list[str]] = mapped_column(JsonDocument, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    must_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


def _user_from_row(row: UserRow) -> User:
    return User(
        login=row.login,
        password_hash=row.password_hash,
        subject=row.subject,
        agent_id=row.agent_id,
        roles=frozenset(row.roles),
        display_name=row.display_name,
        must_change=row.must_change,
    )


class SqlAlchemyCredentialStore:
    """A login-user store backed by a SQLAlchemy engine.

    Mirrors the other stores' engine/URL conventions and implements the
    ``get_user``/``put_user``/``list_users``/``delete_user`` interface, so the
    password backend is agnostic of the backend. Sessions are intentionally
    *not* persisted here -- they are ephemeral in-memory state of the backend.
    """

    def __init__(self, url: str, *, create_tables: bool = False) -> None:
        self._engine = create_engine(url, future=True)
        if create_tables:
            Base.metadata.create_all(self._engine)

    def get_user(self, login: str) -> User | None:
        with Session(self._engine) as session:
            row = session.get(UserRow, login)
            return _user_from_row(row) if row is not None else None

    def put_user(self, user: User) -> User:
        with Session(self._engine) as session:
            row = session.get(UserRow, user.login)
            if row is None:
                row = UserRow(login=user.login)
                session.add(row)
            row.subject = user.subject
            row.password_hash = user.password_hash
            row.agent_id = user.agent_id
            row.roles = sorted(user.roles)
            row.display_name = user.display_name
            row.must_change = user.must_change
            session.commit()
        return user

    def list_users(self) -> list[User]:
        with Session(self._engine) as session:
            return [_user_from_row(row) for row in session.scalars(select(UserRow))]

    def delete_user(self, login: str) -> None:
        with Session(self._engine) as session:
            row = session.get(UserRow, login)
            if row is not None:
                session.delete(row)
                session.commit()


class ExternalTaskRow(Base):
    """One row per external task (outbound integration queue, roadmap E11)."""

    __tablename__ = "external_task"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    topic: Mapped[str] = mapped_column(String, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String, nullable=False, index=True)
    available_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class IncidentRow(Base):
    """One row per raised incident (dead-letter of an exhausted task)."""

    __tablename__ = "incident"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    external_task_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyExternalTaskStore:
    """An external-task store backed by a SQLAlchemy engine.

    Mirrors the other stores: each task/incident is a JSON document plus a few
    queryable columns, and the runtime is agnostic of the backend.
    """

    def __init__(self, url: str, *, create_tables: bool = False) -> None:
        self._engine = create_engine(url, future=True)
        if create_tables:
            Base.metadata.create_all(self._engine)

    def put(self, task: ExternalTask) -> ExternalTask:
        payload = task.model_dump(mode="json")
        with Session(self._engine) as session:
            row = session.get(ExternalTaskRow, task.id)
            if row is None:
                row = ExternalTaskRow(id=task.id)
                session.add(row)
            row.instance_id = task.instance_id
            row.node_id = task.node_id
            row.topic = task.topic
            row.state = task.state.value
            row.available_at = task.available_at
            row.document = payload
            session.commit()
        return task

    def get(self, task_id: str) -> ExternalTask | None:
        with Session(self._engine) as session:
            row = session.get(ExternalTaskRow, task_id)
            if row is None:
                return None
            return ExternalTask.model_validate(row.document)

    def list_tasks(self) -> list[ExternalTask]:
        with Session(self._engine) as session:
            rows = session.scalars(select(ExternalTaskRow))
            return [ExternalTask.model_validate(row.document) for row in rows]

    def put_incident(self, incident: Incident) -> Incident:
        payload = incident.model_dump(mode="json")
        with Session(self._engine) as session:
            row = session.get(IncidentRow, incident.id)
            if row is None:
                row = IncidentRow(id=incident.id)
                session.add(row)
            row.external_task_id = incident.external_task_id
            row.instance_id = incident.instance_id
            row.resolved = incident.resolved
            row.document = payload
            session.commit()
        return incident

    def get_incident(self, incident_id: str) -> Incident | None:
        with Session(self._engine) as session:
            row = session.get(IncidentRow, incident_id)
            if row is None:
                return None
            return Incident.model_validate(row.document)

    def list_incidents(self) -> list[Incident]:
        with Session(self._engine) as session:
            rows = session.scalars(select(IncidentRow))
            return [Incident.model_validate(row.document) for row in rows]

    def clear(self) -> None:
        with Session(self._engine) as session:
            session.execute(delete(ExternalTaskRow))
            session.execute(delete(IncidentRow))
            session.commit()


class WebhookSubscriptionRow(Base):
    """One row per webhook subscription (event side of the open API, E13)."""

    __tablename__ = "webhook_subscription"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class OutboxEntryRow(Base):
    """One row per queued webhook delivery (transactional outbox, E13)."""

    __tablename__ = "webhook_outbox"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String, nullable=False, index=True)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class WebhookDeliveryRow(Base):
    """One row per delivery attempt (append-only delivery log, E13)."""

    __tablename__ = "webhook_delivery"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    outbox_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyWebhookStore:
    """A webhook store backed by a SQLAlchemy engine.

    Mirrors the other stores: subscriptions, outbox entries and deliveries are
    each a JSON document plus a few queryable columns, and the dispatcher is
    agnostic of the backend.
    """

    def __init__(self, url: str, *, create_tables: bool = False) -> None:
        self._engine = create_engine(url, future=True)
        if create_tables:
            Base.metadata.create_all(self._engine)

    def put_subscription(self, sub: WebhookSubscription) -> WebhookSubscription:
        payload = sub.model_dump(mode="json")
        with Session(self._engine) as session:
            row = session.get(WebhookSubscriptionRow, sub.id)
            if row is None:
                row = WebhookSubscriptionRow(id=sub.id)
                session.add(row)
            row.url = sub.url
            row.active = sub.active
            row.document = payload
            session.commit()
        return sub

    def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        with Session(self._engine) as session:
            row = session.get(WebhookSubscriptionRow, subscription_id)
            if row is None:
                return None
            return WebhookSubscription.model_validate(row.document)

    def list_subscriptions(self) -> list[WebhookSubscription]:
        with Session(self._engine) as session:
            rows = session.scalars(select(WebhookSubscriptionRow))
            return [WebhookSubscription.model_validate(row.document) for row in rows]

    def delete_subscription(self, subscription_id: str) -> None:
        with Session(self._engine) as session:
            row = session.get(WebhookSubscriptionRow, subscription_id)
            if row is not None:
                session.delete(row)
                session.commit()

    def put_entry(self, entry: OutboxEntry) -> OutboxEntry:
        payload = entry.model_dump(mode="json")
        with Session(self._engine) as session:
            row = session.get(OutboxEntryRow, entry.id)
            if row is None:
                row = OutboxEntryRow(id=entry.id)
                session.add(row)
            row.subscription_id = entry.subscription_id
            row.event_type = entry.event_type
            row.state = entry.state.value
            row.next_attempt_at = entry.next_attempt_at
            row.document = payload
            session.commit()
        return entry

    def get_entry(self, entry_id: str) -> OutboxEntry | None:
        with Session(self._engine) as session:
            row = session.get(OutboxEntryRow, entry_id)
            if row is None:
                return None
            return OutboxEntry.model_validate(row.document)

    def list_entries(self) -> list[OutboxEntry]:
        with Session(self._engine) as session:
            rows = session.scalars(select(OutboxEntryRow))
            return [OutboxEntry.model_validate(row.document) for row in rows]

    def put_delivery(self, delivery: WebhookDelivery) -> WebhookDelivery:
        payload = delivery.model_dump(mode="json")
        with Session(self._engine) as session:
            row = session.get(WebhookDeliveryRow, delivery.id)
            if row is None:
                row = WebhookDeliveryRow(id=delivery.id)
                session.add(row)
            row.subscription_id = delivery.subscription_id
            row.outbox_id = delivery.outbox_id
            row.at = delivery.at
            row.document = payload
            session.commit()
        return delivery

    def list_deliveries(
        self, subscription_id: str | None = None
    ) -> list[WebhookDelivery]:
        with Session(self._engine) as session:
            stmt = select(WebhookDeliveryRow).order_by(WebhookDeliveryRow.at)
            if subscription_id is not None:
                stmt = stmt.where(
                    WebhookDeliveryRow.subscription_id == subscription_id
                )
            rows = session.scalars(stmt)
            return [WebhookDelivery.model_validate(row.document) for row in rows]

    def clear(self) -> None:
        with Session(self._engine) as session:
            session.execute(delete(WebhookDeliveryRow))
            session.execute(delete(OutboxEntryRow))
            session.execute(delete(WebhookSubscriptionRow))
            session.commit()



