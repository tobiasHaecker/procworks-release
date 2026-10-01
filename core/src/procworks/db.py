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

import uuid
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel
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

from procworks.audit import AuditEvent, EventType, chain_hash
from procworks.auth_password import User, _SessionInfo
from procworks.licensing import AgentBinding, License, PendingClaim, TimeAnchor
from procworks.model import (
    AbsenceEntry,
    ExternalTask,
    Incident,
    MailOutboxEntry,
    OrgModel,
    OutboxEntry,
    ProcessInstance,
    ProcessSchema,
    ProcessTemplate,
    WebhookDelivery,
    WebhookSubscription,
)

#: JSONB on PostgreSQL, generic JSON elsewhere (e.g. SQLite in tests).
JsonDocument = JSON().with_variant(JSONB(), "postgresql")



class Base(DeclarativeBase):
    """Declarative base for the persistence models."""


class _DocumentRow(Protocol):
    """Strukturtyp jeder Zeilenklasse, die ein Pydantic-Dokument trägt.

    Fast alle Tabellen dieses Moduls speichern das vollständige Objekt als
    JSON(B)-Spalte ``document`` (daneben nur abfragbare Kopien einzelner
    Felder). Der Protokolltyp erlaubt :meth:`_SqlAlchemyStore._load`, für all
    diese Zeilenklassen typsicher auf ``document`` zuzugreifen, ohne ihnen eine
    gemeinsame gemappte Basisklasse zu geben — das würde die Spaltenreihenfolge
    der Tabellen berühren, die hier bewusst unverändert bleibt.
    """

    @property
    def document(self) -> dict[str, object]: ...


def _get_or_add[RowT: Base](
    session: Session, row_type: type[RowT], key: str, *, key_column: str = "id"
) -> RowT:
    """Liefere die Zeile mit Primärschlüssel ``key`` oder lege sie neu an.

    Das Upsert-Muster aller ``put``-Methoden: Existiert die Zeile, wird sie
    zurückgegeben und vom Aufrufer überschrieben; sonst entsteht eine neue
    Zeile, die nur den Primärschlüssel trägt und bereits der Session
    hinzugefügt ist. Die übrigen Spalten setzt der Aufrufer, **bevor** er
    ``session.commit()`` ruft — erst dann wird geschrieben, sodass Pflicht-
    spalten zum Zeitpunkt des INSERT gefüllt sind.

    Parameters
    ----------
    session:
        Die offene Session, in der der Aufrufer auch committet.
    row_type:
        Die gemappte Zeilenklasse (z. B. ``SchemaRow``).
    key:
        Wert des Primärschlüssels.
    key_column:
        Name der Primärschlüsselspalte für den Konstruktor einer neuen Zeile;
        meist ``id``, bei einzelnen Tabellen abweichend (``login``,
        ``license_id``, ``agent_id``, ``key``).

    Returns
    -------
    Die vorhandene oder die neu angelegte (noch nicht geschriebene) Zeile.
    """

    row = session.get(row_type, key)
    if row is None:
        row = row_type(**{key_column: key})
        session.add(row)
    return row


class _SqlAlchemyStore:
    """Gemeinsame Basis aller SQLAlchemy-Stores dieses Moduls.

    Bündelt, was jeder Store bisher wörtlich wiederholte: den Aufbau der Engine
    aus der Datenbank-URL, das optionale Anlegen der Tabellen und die drei
    wiederkehrenden Einzelzeilen-Zugriffe (Dokument laden, Zeile löschen,
    Tabellen leeren). Jeder Zugriff öffnet eine eigene Session und committet
    selbst — die Transaktionsgrenzen sind damit dieselben wie zuvor in den
    einzelnen Methoden.

    Parameters
    ----------
    url:
        SQLAlchemy database URL (e.g. ``postgresql+psycopg://user:pw@host/db``
        or ``sqlite:///schemas.db``).
    create_tables:
        If True, create the tables from the ORM metadata. Useful for SQLite and
        local development; in production prefer Alembic migrations and pass
        ``create_tables=False``.
    """

    def __init__(self, url: str, *, create_tables: bool = False) -> None:
        self._engine = create_engine(url, future=True)
        if create_tables:
            Base.metadata.create_all(self._engine)

    def _load[RowT: _DocumentRow, ModelT: BaseModel](
        self, row_type: type[RowT], key: str, model: type[ModelT]
    ) -> ModelT | None:
        """Lade das Dokument der Zeile ``key`` als Pydantic-Modell ``model``.

        Returns
        -------
        Das aus der Spalte ``document`` validierte Modell, oder ``None``, wenn
        es keine Zeile mit diesem Primärschlüssel gibt.
        """

        with Session(self._engine) as session:
            row = session.get(row_type, key)
            if row is None:
                return None
            return model.model_validate(row.document)

    def _delete_row(self, row_type: type[Base], key: str) -> bool:
        """Lösche die Zeile mit Primärschlüssel ``key``, falls vorhanden.

        Idempotent: Fehlt die Zeile, geschieht nichts (auch kein Commit).

        Returns
        -------
        ``True``, wenn eine Zeile gelöscht wurde, sonst ``False`` — Aufrufer,
        deren Schnittstelle nichts zurückgibt, ignorieren den Wert.
        """

        with Session(self._engine) as session:
            row = session.get(row_type, key)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def _delete_all(self, *row_types: type[Base]) -> None:
        """Leere die Tabellen ``row_types`` in genau dieser Reihenfolge.

        Alle Löschungen laufen in einer Session und werden gemeinsam
        committet (eine Transaktion). Die Reihenfolge ist Sache des Aufrufers:
        abhängige Tabellen zuerst.
        """

        with Session(self._engine) as session:
            for row_type in row_types:
                session.execute(delete(row_type))
            session.commit()


class SchemaRow(Base):
    """One row per process schema (keyed by schema id)."""

    __tablename__ = "process_schema"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemySchemaStore(_SqlAlchemyStore):
    """A schema store backed by a SQLAlchemy engine.

    Constructor parameters (``url``, ``create_tables``) as in
    :class:`_SqlAlchemyStore`.
    """

    def put(self, schema: ProcessSchema) -> ProcessSchema:
        payload = schema.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, SchemaRow, schema.id)
            row.version = schema.version
            row.name = schema.name
            row.lifecycle_state = schema.lifecycle_state.value
            row.document = payload
            session.commit()
        return schema

    def get(self, schema_id: str) -> ProcessSchema | None:
        return self._load(SchemaRow, schema_id, ProcessSchema)

    def list_ids(self) -> list[str]:
        with Session(self._engine) as session:
            return list(session.scalars(select(SchemaRow.id)))

    def clear(self) -> None:
        self._delete_all(SchemaRow)


class OrgRow(Base):
    """One row per shared, standalone org model (keyed by org id)."""

    __tablename__ = "org_model"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyOrgStore(_SqlAlchemyStore):
    """A shared-org-model store backed by a SQLAlchemy engine.

    Mirrors ``SqlAlchemySchemaStore``: same engine/URL conventions and the same
    ``put``/``get``/``list_ids`` interface as the in-memory org store, so the
    API is agnostic of the backend.
    """

    def put(self, org: OrgModel) -> OrgModel:
        if org.id is None:
            raise ValueError("a shared org model must have an id before it is stored")
        payload = org.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, OrgRow, org.id)
            row.name = org.name
            row.document = payload
            session.commit()
        return org

    def get(self, org_id: str) -> OrgModel | None:
        return self._load(OrgRow, org_id, OrgModel)

    def list_ids(self) -> list[str]:
        with Session(self._engine) as session:
            return list(session.scalars(select(OrgRow.id)))

    def clear(self) -> None:
        self._delete_all(OrgRow)


class InstanceRow(Base):
    """One row per process instance (keyed by instance id)."""

    __tablename__ = "process_instance"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_id: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String, nullable=False)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyInstanceStore(_SqlAlchemyStore):
    """A process-instance store backed by a SQLAlchemy engine.

    Mirrors ``SqlAlchemySchemaStore``: same engine/URL conventions and the same
    ``put``/``get``/``list_ids`` interface as the in-memory instance store, so
    the execution engine and API are agnostic of the backend.
    """

    def put(self, instance: ProcessInstance) -> ProcessInstance:
        payload = instance.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, InstanceRow, instance.id)
            row.schema_id = instance.schema_id
            row.schema_version = instance.schema_version
            row.state = instance.state.value
            row.document = payload
            session.commit()
        return instance

    def get(self, instance_id: str) -> ProcessInstance | None:
        return self._load(InstanceRow, instance_id, ProcessInstance)

    def list_ids(self) -> list[str]:
        with Session(self._engine) as session:
            return list(session.scalars(select(InstanceRow.id)))

    def clear(self) -> None:
        self._delete_all(InstanceRow)


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
    #: Append-only hash chain (see procworks.audit.AuditEvent). Defaulted to ""
    #: so rows written before the chain existed remain valid.
    prev_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    entry_hash: Mapped[str] = mapped_column(String, nullable=False, default="")


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
        prev_hash=row.prev_hash or "",
        entry_hash=row.entry_hash or "",
    )


class SqlAlchemyAuditLog(_SqlAlchemyStore):
    """A durable, append-only event log backed by a SQLAlchemy engine.

    Implements the same ``append``/``list_all``/``for_instance`` interface as
    :class:`procworks.audit.InMemoryAuditLog`, so the API records events the
    same way regardless of the backend. The monotonic ``seq`` is assigned by the
    database (autoincrement primary key); events are returned ordered by ``seq``.
    """

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
        at: datetime | None = None,
    ) -> AuditEvent:
        with Session(self._engine) as session:
            timestamp = at or datetime.now(UTC)
            detail = detail or {}
            prev_hash = self._head_hash(session)
            row = AuditEventRow(
                timestamp=timestamp,
                event_type=event_type.value,
                instance_id=instance_id,
                schema_id=schema_id,
                schema_version=schema_version,
                node_id=node_id,
                label=label,
                agent_id=agent_id,
                detail=detail,
                prev_hash=prev_hash,
            )
            session.add(row)
            session.flush()  # assign the autoincrement seq before hashing
            row.entry_hash = chain_hash(
                prev_hash,
                seq=row.seq,
                timestamp=timestamp,
                event_type=event_type,
                instance_id=instance_id,
                schema_id=schema_id,
                schema_version=schema_version,
                node_id=node_id,
                label=label,
                agent_id=agent_id,
                detail=detail,
            )
            session.commit()
            session.refresh(row)
            return _event_from_row(row)

    @staticmethod
    def _head_hash(session: Session) -> str:
        row = session.scalars(
            select(AuditEventRow).order_by(AuditEventRow.seq.desc()).limit(1)
        ).first()
        return (row.entry_hash or "") if row is not None else ""

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

    def head_hash(self) -> str:
        """Return the newest entry's chain hash ("" when the log is empty)."""

        with Session(self._engine) as session:
            return self._head_hash(session)

    def max_event_time(self) -> float:
        """Return the newest recorded timestamp as epoch seconds (0.0 if empty).

        A monotone lower bound on real time for the licensing ratchet.
        The explicit annotation matters: since SQLAlchemy 2.1 ``func.max``
        is typed as ``Any``, which ``mypy --strict`` rejects on return.
        """

        with Session(self._engine) as session:
            value: datetime | None = session.scalar(select(func.max(AuditEventRow.timestamp)))
            if value is None:
                return 0.0
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.timestamp()

    def clear(self) -> None:
        self._delete_all(AuditEventRow)


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


class SqlAlchemyCredentialStore(_SqlAlchemyStore):
    """A login-user store backed by a SQLAlchemy engine.

    Mirrors the other stores' engine/URL conventions and implements the
    ``get_user``/``put_user``/``list_users``/``delete_user`` interface, so the
    password backend is agnostic of the backend. Sessions are intentionally
    *not* persisted here -- they are ephemeral in-memory state of the backend.
    """

    def get_user(self, login: str) -> User | None:
        with Session(self._engine) as session:
            row = session.get(UserRow, login)
            return _user_from_row(row) if row is not None else None

    def put_user(self, user: User) -> User:
        with Session(self._engine) as session:
            row = _get_or_add(session, UserRow, user.login, key_column="login")
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
        self._delete_row(UserRow, login)


class SessionRow(Base):
    """One login session (password mode); keyed by the token's SHA-256 digest.

    Sessions live in the database, so a restart or update does not log
    everybody out. The clear token is never stored -- only its digest.
    """

    __tablename__ = "auth_session"

    digest: Mapped[str] = mapped_column(String, primary_key=True)
    login: Mapped[str] = mapped_column(String, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemySessionStore(_SqlAlchemyStore):
    """Login sessions in the database (``auth_session``), see ``SessionStore``.

    Same engine/URL conventions as the credential store. ``expires_at`` is
    stored timezone-aware; SQLite returns it naive, so it is re-attached as UTC
    on read.
    """

    def get(self, digest: str) -> _SessionInfo | None:
        with Session(self._engine) as session:
            row = session.get(SessionRow, digest)
            if row is None:
                return None
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            return _SessionInfo(login=row.login, expires_at=expires)

    def put(self, digest: str, info: _SessionInfo) -> None:
        with Session(self._engine) as session:
            session.merge(SessionRow(digest=digest, login=info.login, expires_at=info.expires_at))
            session.commit()

    def delete(self, digest: str) -> None:
        with Session(self._engine) as session:
            session.execute(delete(SessionRow).where(SessionRow.digest == digest))
            session.commit()

    def delete_for_login(self, login: str) -> None:
        with Session(self._engine) as session:
            session.execute(delete(SessionRow).where(SessionRow.login == login))
            session.commit()

    def purge_expired(self, now: datetime) -> None:
        with Session(self._engine) as session:
            session.execute(delete(SessionRow).where(SessionRow.expires_at <= now))
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


class SqlAlchemyExternalTaskStore(_SqlAlchemyStore):
    """An external-task store backed by a SQLAlchemy engine.

    Mirrors the other stores: each task/incident is a JSON document plus a few
    queryable columns, and the runtime is agnostic of the backend.
    """

    def put(self, task: ExternalTask) -> ExternalTask:
        payload = task.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, ExternalTaskRow, task.id)
            row.instance_id = task.instance_id
            row.node_id = task.node_id
            row.topic = task.topic
            row.state = task.state.value
            row.available_at = task.available_at
            row.document = payload
            session.commit()
        return task

    def get(self, task_id: str) -> ExternalTask | None:
        return self._load(ExternalTaskRow, task_id, ExternalTask)

    def list_tasks(self) -> list[ExternalTask]:
        with Session(self._engine) as session:
            rows = session.scalars(select(ExternalTaskRow))
            return [ExternalTask.model_validate(row.document) for row in rows]

    def put_incident(self, incident: Incident) -> Incident:
        payload = incident.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, IncidentRow, incident.id)
            row.external_task_id = incident.external_task_id
            row.instance_id = incident.instance_id
            row.resolved = incident.resolved
            row.document = payload
            session.commit()
        return incident

    def get_incident(self, incident_id: str) -> Incident | None:
        return self._load(IncidentRow, incident_id, Incident)

    def list_incidents(self) -> list[Incident]:
        with Session(self._engine) as session:
            rows = session.scalars(select(IncidentRow))
            return [Incident.model_validate(row.document) for row in rows]

    def clear(self) -> None:
        self._delete_all(ExternalTaskRow, IncidentRow)


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


class SqlAlchemyWebhookStore(_SqlAlchemyStore):
    """A webhook store backed by a SQLAlchemy engine.

    Mirrors the other stores: subscriptions, outbox entries and deliveries are
    each a JSON document plus a few queryable columns, and the dispatcher is
    agnostic of the backend.
    """

    def put_subscription(self, sub: WebhookSubscription) -> WebhookSubscription:
        payload = sub.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, WebhookSubscriptionRow, sub.id)
            row.url = sub.url
            row.active = sub.active
            row.document = payload
            session.commit()
        return sub

    def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        return self._load(WebhookSubscriptionRow, subscription_id, WebhookSubscription)

    def list_subscriptions(self) -> list[WebhookSubscription]:
        with Session(self._engine) as session:
            rows = session.scalars(select(WebhookSubscriptionRow))
            return [WebhookSubscription.model_validate(row.document) for row in rows]

    def delete_subscription(self, subscription_id: str) -> None:
        self._delete_row(WebhookSubscriptionRow, subscription_id)

    def put_entry(self, entry: OutboxEntry) -> OutboxEntry:
        payload = entry.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, OutboxEntryRow, entry.id)
            row.subscription_id = entry.subscription_id
            row.event_type = entry.event_type
            row.state = entry.state.value
            row.next_attempt_at = entry.next_attempt_at
            row.document = payload
            session.commit()
        return entry

    def get_entry(self, entry_id: str) -> OutboxEntry | None:
        return self._load(OutboxEntryRow, entry_id, OutboxEntry)

    def list_entries(self) -> list[OutboxEntry]:
        with Session(self._engine) as session:
            rows = session.scalars(select(OutboxEntryRow))
            return [OutboxEntry.model_validate(row.document) for row in rows]

    def put_delivery(self, delivery: WebhookDelivery) -> WebhookDelivery:
        payload = delivery.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, WebhookDeliveryRow, delivery.id)
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
        self._delete_all(WebhookDeliveryRow, OutboxEntryRow, WebhookSubscriptionRow)


class MailOutboxEntryRow(Base):
    """One row per queued modelled e-mail notification (mail outbox, rule N)."""

    __tablename__ = "mail_outbox"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String, nullable=False, index=True)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyMailOutboxStore(_SqlAlchemyStore):
    """A durable mail outbox backed by a SQLAlchemy engine.

    Mirrors the webhook outbox: each queued notification is a JSON document plus
    a few queryable columns (``dedup_key`` for idempotency lookups, ``state`` and
    ``next_attempt_at`` for the dispatcher). The dispatcher stays backend-agnostic.
    """

    def put_entry(self, entry: MailOutboxEntry) -> MailOutboxEntry:
        payload = entry.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, MailOutboxEntryRow, entry.id)
            row.dedup_key = entry.dedup_key
            row.instance_id = entry.instance_id
            row.state = entry.state.value
            row.next_attempt_at = entry.next_attempt_at
            row.document = payload
            session.commit()
        return entry

    def get_entry(self, entry_id: str) -> MailOutboxEntry | None:
        return self._load(MailOutboxEntryRow, entry_id, MailOutboxEntry)

    def find_by_dedup_key(self, dedup_key: str) -> MailOutboxEntry | None:
        with Session(self._engine) as session:
            row = session.scalars(
                select(MailOutboxEntryRow).where(
                    MailOutboxEntryRow.dedup_key == dedup_key
                )
            ).first()
            if row is None:
                return None
            return MailOutboxEntry.model_validate(row.document)

    def list_entries(self) -> list[MailOutboxEntry]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(MailOutboxEntryRow).order_by(MailOutboxEntryRow.next_attempt_at)
            )
            return [MailOutboxEntry.model_validate(row.document) for row in rows]

    def clear(self) -> None:
        self._delete_all(MailOutboxEntryRow)


class AbsenceEntryRow(Base):
    """One row per recorded agent absence (deputy substitution window)."""

    __tablename__ = "absence_entry"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyAbsenceStore(_SqlAlchemyStore):
    """A durable absence store backed by a SQLAlchemy engine.

    Each entry is a JSON document plus an ``agent_id`` column for lookups. Like
    every other store the API stays backend-agnostic; the boundary lists the
    (small) set of entries and resolves the currently-absent agents in Python.
    """

    def put_entry(self, entry: AbsenceEntry) -> AbsenceEntry:
        payload = entry.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, AbsenceEntryRow, entry.id)
            row.agent_id = entry.agent_id
            row.document = payload
            session.commit()
        return entry

    def get_entry(self, entry_id: str) -> AbsenceEntry | None:
        return self._load(AbsenceEntryRow, entry_id, AbsenceEntry)

    def list_entries(self) -> list[AbsenceEntry]:
        with Session(self._engine) as session:
            rows = session.scalars(select(AbsenceEntryRow).order_by(AbsenceEntryRow.id))
            return [AbsenceEntry.model_validate(row.document) for row in rows]

    def delete_entry(self, entry_id: str) -> bool:
        return self._delete_row(AbsenceEntryRow, entry_id)

    def clear(self) -> None:
        self._delete_all(AbsenceEntryRow)


class TemplateRow(Base):
    """One row per user-created process template (keyed by template id)."""

    __tablename__ = "process_template"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, default="")
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyTemplateStore(_SqlAlchemyStore):
    """A durable user-template store backed by a SQLAlchemy engine.

    Stores only user-created templates as JSON documents; built-in templates
    are provided by :mod:`procworks.templates` and never persisted. Mirrors the
    other stores' ``put``/``get``/``list_ids``/``delete``/``clear`` interface so
    the API stays backend-agnostic.
    """

    def put(self, template: ProcessTemplate) -> ProcessTemplate:
        payload = template.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, TemplateRow, template.id)
            row.name = template.name
            row.category = template.category
            row.document = payload
            session.commit()
        return template

    def get(self, template_id: str) -> ProcessTemplate | None:
        return self._load(TemplateRow, template_id, ProcessTemplate)

    def list_ids(self) -> list[str]:
        with Session(self._engine) as session:
            return list(session.scalars(select(TemplateRow.id).order_by(TemplateRow.id)))

    def delete(self, template_id: str) -> bool:
        return self._delete_row(TemplateRow, template_id)

    def clear(self) -> None:
        self._delete_all(TemplateRow)


class LicenseRow(Base):
    """One row per license contingent (free quota / bought pack)."""

    __tablename__ = "license"

    license_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    slots: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    install_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class AgentBindingRow(Base):
    """One row per agent->license assignment (explicit binding)."""

    __tablename__ = "agent_license_binding"

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    license_id: Mapped[str] = mapped_column(String, nullable=False, index=True)


class LicenseMetaRow(Base):
    """Key/value slot for the install id and the persisted time anchor."""

    __tablename__ = "license_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    document: Mapped[dict[str, object]] = mapped_column(JsonDocument, nullable=False)


class SqlAlchemyLicenseStore(_SqlAlchemyStore):
    """A durable license store backed by a SQLAlchemy engine.

    Mirrors the other stores: each license is a JSON document plus a few
    queryable columns; bindings and the small meta records (install id, time
    anchor) are their own tables. The manager stays backend-agnostic.
    """

    _INSTALL_KEY = "install_id"
    _ANCHOR_KEY = "time_anchor"
    #: Open auto-pull claims share the generic meta table under this key prefix
    #: (they are ephemeral, best-effort state that needs no dedicated schema).
    _CLAIM_PREFIX = "claim:"

    def list_licenses(self) -> list[License]:
        with Session(self._engine) as session:
            rows = session.scalars(select(LicenseRow))
            return [License.model_validate(row.document) for row in rows]

    def put_license(self, lic: License) -> None:
        payload = lic.model_dump(mode="json")
        with Session(self._engine) as session:
            row = _get_or_add(session, LicenseRow, lic.license_id, key_column="license_id")
            row.kind = lic.kind.value
            row.slots = lic.slots
            row.expires_at = lic.expires_at
            row.install_id = lic.install_id
            row.document = payload
            session.commit()

    def get_license(self, license_id: str) -> License | None:
        return self._load(LicenseRow, license_id, License)

    def remove_license(self, license_id: str) -> None:
        self._delete_row(LicenseRow, license_id)

    def list_bindings(self) -> list[AgentBinding]:
        with Session(self._engine) as session:
            rows = session.scalars(select(AgentBindingRow))
            return [
                AgentBinding(agent_id=row.agent_id, license_id=row.license_id)
                for row in rows
            ]

    def bind(self, agent_id: str, license_id: str) -> None:
        with Session(self._engine) as session:
            row = _get_or_add(session, AgentBindingRow, agent_id, key_column="agent_id")
            row.license_id = license_id
            session.commit()

    def unbind(self, agent_id: str) -> None:
        self._delete_row(AgentBindingRow, agent_id)

    def get_time_anchor(self) -> TimeAnchor | None:
        return self._load(LicenseMetaRow, self._ANCHOR_KEY, TimeAnchor)

    def put_time_anchor(self, anchor: TimeAnchor) -> None:
        self._put_meta(self._ANCHOR_KEY, anchor.model_dump(mode="json"))

    def list_claims(self) -> list[PendingClaim]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(LicenseMetaRow).where(
                    LicenseMetaRow.key.like(f"{self._CLAIM_PREFIX}%")
                )
            )
            return [PendingClaim.model_validate(row.document) for row in rows]

    def put_claim(self, claim: PendingClaim) -> None:
        key = self._CLAIM_PREFIX + claim.claim_token
        self._put_meta(key, claim.model_dump(mode="json"))

    def _put_meta(self, key: str, payload: dict[str, object]) -> None:
        """Schreibe ``payload`` als Dokument des Meta-Eintrags ``key`` (Upsert).

        Gemeinsamer Schreibweg für Zeitanker und offene Claims, die sich die
        generische Tabelle ``license_meta`` teilen. Ein fehlender Eintrag wird
        angelegt, ein vorhandener überschrieben; ein Commit je Aufruf.

        Parameters
        ----------
        key:
            Primärschlüssel in ``license_meta`` (``time_anchor`` oder
            ``claim:<token>``).
        payload:
            Das JSON-serialisierte Modell (``model_dump(mode="json")``).
        """

        with Session(self._engine) as session:
            row = _get_or_add(session, LicenseMetaRow, key, key_column="key")
            row.document = payload
            session.commit()

    def remove_claim(self, claim_token: str) -> None:
        self._delete_row(LicenseMetaRow, self._CLAIM_PREFIX + claim_token)

    def install_id(self) -> str:
        with Session(self._engine) as session:
            row = session.get(LicenseMetaRow, self._INSTALL_KEY)
            if row is not None:
                return str(row.document["value"])
            value = uuid.uuid4().hex
            session.add(
                LicenseMetaRow(key=self._INSTALL_KEY, document={"value": value})
            )
            session.commit()
            return value

    def clear(self) -> None:
        with Session(self._engine) as session:
            session.execute(delete(LicenseRow))
            session.execute(delete(AgentBindingRow))
            # The install id survives a data reset (it identifies the install,
            # and bought packs are bound to it); the anchor and any open
            # auto-pull claims are dropped.
            session.execute(
                delete(LicenseMetaRow).where(
                    (LicenseMetaRow.key == self._ANCHOR_KEY)
                    | (LicenseMetaRow.key.like(f"{self._CLAIM_PREFIX}%"))
                )
            )
            session.commit()



