# SPDX-License-Identifier: BUSL-1.1
"""Schema store interface, in-memory implementation, and a store factory.

The API depends only on the ``SchemaStore`` protocol, so the backing store can
be swapped (in-memory for tests/demo, PostgreSQL for real deployments) without
touching the endpoints.

Cases (instances) and models (schemas) are saved **conditionally** in every
backend: ``get`` hands out a detached copy that remembers the stored version
it was read with, and ``put`` refuses with
:class:`procworks.unit_of_work.ConcurrentModification` when the stored version
has moved on since -- a change made in between is never overwritten
(:func:`stored_version`, :class:`_VersionedDocuments`).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Collection
from typing import Protocol

from pydantic import BaseModel

from procworks import unit_of_work
from procworks.model import (
    DUE_MAIL_OUTBOX_STATES,
    DUE_OUTBOX_STATES,
    OPEN_EXTERNAL_TASK_STATES,
    AbsenceEntry,
    ExternalTask,
    Incident,
    InstanceState,
    LifecycleState,
    MailOutboxEntry,
    OrgModel,
    OutboxEntry,
    OutboxState,
    ProcessInstance,
    ProcessSchema,
    ProcessTemplate,
    WebhookDelivery,
    WebhookSubscription,
)
from procworks.unit_of_work import ConcurrentModification


def stored_version(
    obj: ProcessSchema | ProcessInstance | ExternalTask | OrgModel,
) -> int | None:
    """The stored version ``obj`` was read (or last saved) with.

    :param obj: a schema, instance, external task or shared org model.
    :returns: the version, or ``None`` for an object that was never stored --
        ``put`` then treats it as new and refuses an id that already exists.
        Also ``None`` for a copy that got another id since (a revision is
        a copy of the released schema under a new id: it is a new object).
    """

    stored_as = obj._stored_as
    if stored_as is None or stored_as[0] != obj.id:
        return None
    return stored_as[1]


def remember_version(
    obj: ProcessSchema | ProcessInstance | ExternalTask | OrgModel, version: int
) -> None:
    """Attach the stored ``version`` to ``obj`` (stores only: after load and save).

    Saving updates the caller's object too, so a request that stores the same
    case several times (engine, activation stamps, audit hand-over) does not
    conflict with itself.
    """

    obj._stored_as = (stored_key(obj), version)


def stored_key(obj: ProcessSchema | ProcessInstance | ExternalTask | OrgModel) -> str:
    """The id under which ``obj`` is stored.

    Only a shared org model can lack one (an embedded model is never stored on
    its own); the org stores refuse that before saving.

    :param obj: the object to save or that was loaded.
    :returns: ``obj.id``.
    :raises ValueError: ``obj`` has no id.
    """

    if obj.id is None:
        raise ValueError("only an object with an id can be stored")
    return obj.id


class _VersionedDocuments[ModelT: (ProcessSchema, ProcessInstance, OrgModel)]:
    """In-memory rows ``id -> (version, detached copy)`` with the conditional save.

    Behaves like the SQL stores: ``get`` returns a deep copy that knows its
    version, so changing it changes nothing until it is saved, and ``put``
    stores a deep copy only if the stored version is still the one the object
    was read with. Before, the store handed out the stored object itself; an
    in-place change was visible to every other request at once, and the last
    save won.

    :param kind: ``"schema"``, ``"instance"`` or ``"org"``, named in a conflict.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._rows: dict[str, tuple[int, ModelT]] = {}
        self._lock = threading.Lock()

    def put(self, obj: ModelT) -> ModelT:
        """Store a copy of ``obj`` if its version is current (see the class).

        :raises ConcurrentModification: the stored version moved on since
            ``obj`` was read, the row is gone, or a new object (no version)
            uses a stored id.
        """

        base = stored_version(obj)
        key = stored_key(obj)
        with self._lock:
            row = self._rows.get(key)
            found = row[0] if row is not None else None
            if base != found:
                raise unit_of_work.note_conflict(ConcurrentModification(self._kind, key))
            version = (found or 0) + 1
            self._rows[key] = (version, obj.model_copy(deep=True))
        remember_version(obj, version)
        return obj

    def get(self, key: str) -> ModelT | None:
        """A detached copy of the stored object, carrying its version."""

        with self._lock:
            row = self._rows.get(key)
            if row is None:
                return None
            version, stored = row
            copy = stored.model_copy(deep=True)
        remember_version(copy, version)
        return copy

    def list_ids(self) -> list[str]:
        with self._lock:
            return list(self._rows.keys())

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


class SchemaSummary(BaseModel):
    """What a selection of schemas shows, without the whole model.

    One entry per stored schema (``GET /schema-summaries``): a client lists
    hundreds of schemas with one request instead of loading each model.
    ``revision_of`` is the lineage (``None`` for a first version or a schema
    from before lineage was recorded).
    """

    id: str
    name: str
    version: int
    lifecycle_state: LifecycleState
    revision_of: str | None = None


def summary_of(schema: ProcessSchema) -> SchemaSummary:
    """The :class:`SchemaSummary` of one model (in-memory stores, fallbacks)."""

    return SchemaSummary(
        id=schema.id,
        name=schema.name,
        version=schema.version,
        lifecycle_state=schema.lifecycle_state,
        revision_of=schema.revision_of,
    )


class SchemaStore(Protocol):
    """Minimal persistence interface for process schemas.

    ``put`` saves conditionally: it raises
    :class:`~procworks.unit_of_work.ConcurrentModification` when the schema was
    saved by someone else since it was read with ``get`` (or when a new schema
    uses a stored id). ``get`` returns a detached copy.
    """

    def put(self, schema: ProcessSchema) -> ProcessSchema: ...

    def get(self, schema_id: str) -> ProcessSchema | None: ...

    def list_ids(self) -> list[str]: ...

    def summaries(self) -> list[SchemaSummary]:
        """One :class:`SchemaSummary` per stored schema, in ``list_ids`` order."""
        ...

    def clear(self) -> None: ...


def make_resolver(
    store: SchemaStore,
) -> Callable[[str, int | None], ProcessSchema | None]:
    """Build a schema resolver for the composition rules (H1-H4, F1-F3).

    Resolves a schema by id and, if a version is pinned, only returns it when
    the stored version matches. With the simplified single-version store this
    is sufficient to enforce the pinned-version semantics.
    """

    def resolve(schema_id: str, version: int | None) -> ProcessSchema | None:
        schema = store.get(schema_id)
        if schema is None:
            return None
        if version is not None and schema.version != version:
            return None
        return schema

    return resolve




class InMemorySchemaStore(_VersionedDocuments[ProcessSchema]):
    """Schemas in memory, keyed by id (default for tests and the demo).

    Saves conditionally and hands out detached copies (:class:`_VersionedDocuments`).
    """

    def __init__(self) -> None:
        super().__init__("schema")

    def summaries(self) -> list[SchemaSummary]:
        """One summary per schema, in insertion order (as :meth:`list_ids`).

        Read under the lock without copying the models: only immutable
        scalars leave it.
        """

        with self._lock:
            return [summary_of(stored) for _, stored in self._rows.values()]


def create_store() -> SchemaStore:
    """Build the store from the environment.

    If ``DATABASE_URL`` is set, use the SQLAlchemy-backed store (tables are
    created on first use for convenience; production should rely on Alembic).
    Otherwise fall back to the in-memory store.
    """

    url = os.environ.get("DATABASE_URL")
    if url:
        # Imported lazily so the in-memory path has no SQLAlchemy import cost.
        from procworks.db import SqlAlchemySchemaStore

        return SqlAlchemySchemaStore(url, create_tables=True)
    return InMemorySchemaStore()


class InstanceStore(Protocol):
    """Minimal persistence interface for process instances.

    Conditional save and detached copies as in :class:`SchemaStore`.

    Besides the single-case access (``get``) the store answers the two
    collective reads of the overview paths in **one** access each
    (``running_schema_ids``, ``list_instances``): worklists, monitoring,
    titles, the escalation sweep and the push drive read many cases at
    once, and loading them one by one made every such read linear in the
    stock -- with one database round trip per case.
    """

    def put(self, instance: ProcessInstance) -> ProcessInstance: ...

    def get(self, instance_id: str) -> ProcessInstance | None: ...

    def list_ids(self) -> list[str]: ...

    def running_schema_ids(self) -> set[str]: ...

    def list_instances(
        self,
        *,
        running_only: bool = False,
        schema_ids: Collection[str] | None = None,
    ) -> list[ProcessInstance]: ...

    def clear(self) -> None: ...


class InMemoryInstanceStore(_VersionedDocuments[ProcessInstance]):
    """Running instances in memory, keyed by id.

    This is the default store without configuration; with ``DATABASE_URL`` set,
    ``create_instance_store`` returns the durable ``SqlAlchemyInstanceStore``
    instead (mirroring the schema store).

    Saves conditionally and hands out detached copies (:class:`_VersionedDocuments`).
    """

    def __init__(self) -> None:
        super().__init__("instance")

    def running_schema_ids(self) -> set[str]:
        """The schema ids that at least one RUNNING case runs on.

        Lets a pass that only concerns some schemas (those with escalation
        rules, those with push steps) decide on the schemas first and load
        only their cases.

        :returns: the distinct ``schema_id`` of every RUNNING case; empty
            without running cases. Ad-hoc changed cases count under their
            base schema id (the id they carry).
        """

        with self._lock:
            return {
                stored.schema_id
                for _version, stored in self._rows.values()
                if stored.state is InstanceState.RUNNING
            }

    def list_instances(
        self,
        *,
        running_only: bool = False,
        schema_ids: Collection[str] | None = None,
    ) -> list[ProcessInstance]:
        """Many cases in one access, each a detached copy with its version.

        :param running_only: only cases in state RUNNING.
        :param schema_ids: only cases of these schema ids; ``None`` means
            every schema, an empty collection means none (no case).
        :returns: the matching cases in the order of :meth:`list_ids`
            (insertion order); each copy carries its stored version, so it
            could be changed and saved like one loaded with :meth:`get`.
        """

        wanted = None if schema_ids is None else set(schema_ids)
        with self._lock:
            rows = [
                (version, stored.model_copy(deep=True))
                for version, stored in self._rows.values()
                if (not running_only or stored.state is InstanceState.RUNNING)
                and (wanted is None or stored.schema_id in wanted)
            ]
        for version, copy in rows:
            remember_version(copy, version)
        return [copy for _version, copy in rows]


def create_instance_store() -> InstanceStore:
    """Build the instance store from the environment.

    If ``DATABASE_URL`` is set, use the SQLAlchemy-backed store (durable
    instance persistence; tables are created on first use for convenience,
    production should rely on Alembic). Otherwise fall back to in-memory.
    """

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyInstanceStore

        return SqlAlchemyInstanceStore(url, create_tables=True)
    return InMemoryInstanceStore()


class IdClaimStore(Protocol):
    """Atomic reservation of generated ids shared by all API processes.

    Several API processes (e.g. Kubernetes replicas) write into the same
    database, and each allocates ids from its own counter. A lookup in the
    instance or schema store cannot stop two of them from choosing the same
    unused number in the same instant -- and since those stores save by upsert,
    the later save would silently replace the earlier object. A claim is a
    single unique insert: exactly one process can win a given id.
    """

    def claim(self, key: str) -> bool:
        """Reserve ``key``; ``True`` if this call reserved it, ``False`` if it was taken."""
        ...

    def list_ids(self) -> list[str]:
        """Every reserved id, so a restarted process lifts its counters past them."""
        ...

    def clear(self) -> None:
        """Forget all reservations. Only for tests: ``/admin/reset`` keeps them,
        because another process may hold a reservation it has not saved yet."""
        ...


class InMemoryIdClaimStore:
    """Process-local claims for the in-memory configuration.

    Without a database there is exactly one API process, so a set under a lock
    is already atomic. It still runs so the allocation path is the same in
    every configuration.
    """

    def __init__(self) -> None:
        self._claimed: set[str] = set()
        self._lock = threading.Lock()

    def claim(self, key: str) -> bool:
        """Reserve ``key`` unless it is already reserved.

        :param key: the generated id, e.g. ``instance_26``.
        :returns: ``True`` if newly reserved, ``False`` if reserved before.
        """

        with self._lock:
            if key in self._claimed:
                return False
            self._claimed.add(key)
            return True

    def list_ids(self) -> list[str]:
        """Return every id reserved in this process."""

        with self._lock:
            return list(self._claimed)

    def clear(self) -> None:
        """Forget all reservations (tests only)."""

        with self._lock:
            self._claimed.clear()


def create_id_claim_store() -> IdClaimStore:
    """Build the claim store from the environment.

    With ``DATABASE_URL`` the claims live in the shared table ``id_claim``
    (created on first use for convenience, production relies on Alembic), so
    every API process against that database sees them. Otherwise in memory.
    """

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyIdClaimStore

        return SqlAlchemyIdClaimStore(url, create_tables=True)
    return InMemoryIdClaimStore()


class OrgStore(Protocol):
    """Minimal persistence interface for shared, standalone org models."""

    def put(self, org: OrgModel) -> OrgModel: ...

    def get(self, org_id: str) -> OrgModel | None: ...

    def list_ids(self) -> list[str]: ...

    def clear(self) -> None: ...


class InMemoryOrgStore:
    """Shared org models keyed by id, saved conditionally like schemas.

    ``get`` hands out a detached copy that knows its stored version, ``put``
    refuses (``ConcurrentModification``, kind ``"org"``) when the stored
    version moved on since, or when a new org uses a stored id. Before, the
    store handed out the stored object and the last save won: two changes of
    an organisation that no schema referenced lost one of them.
    """

    def __init__(self) -> None:
        self._orgs: _VersionedDocuments[OrgModel] = _VersionedDocuments("org")

    def put(self, org: OrgModel) -> OrgModel:
        """Save ``org`` if nobody saved it since it was read.

        :param org: a shared org model (``id`` set).
        :returns: ``org``, carrying its new stored version.
        :raises ValueError: ``org`` has no id (an embedded model).
        :raises ConcurrentModification: see the class.
        """

        if org.id is None:
            raise ValueError("a shared org model must have an id before it is stored")
        return self._orgs.put(org)

    def get(self, org_id: str) -> OrgModel | None:
        """A detached copy of the stored org, carrying its version."""

        return self._orgs.get(org_id)

    def list_ids(self) -> list[str]:
        return self._orgs.list_ids()

    def clear(self) -> None:
        self._orgs.clear()


def create_org_store() -> OrgStore:
    """Build the shared-org store from the environment (mirrors the others)."""

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyOrgStore

        return SqlAlchemyOrgStore(url, create_tables=True)
    return InMemoryOrgStore()


def make_org_resolver(store: OrgStore) -> Callable[[str | None], OrgModel | None]:
    """Build a resolver that maps a (possibly absent) org id to its model."""

    def resolve(org_id: str | None) -> OrgModel | None:
        if org_id is None:
            return None
        return store.get(org_id)

    return resolve


def hydrate_org(
    schema: ProcessSchema, org_resolver: Callable[[str | None], OrgModel | None]
) -> ProcessSchema:
    """Fill ``schema.org_model`` from the shared registry when linked.

    A schema that references a shared org model carries only an empty embedded
    ``org_model`` in storage; before any validation / resolution it must be
    *hydrated* with the live shared model. Unlinked schemas are returned
    unchanged. If the referenced model is missing, the (empty) embedded model
    is left in place so validation surfaces the dangling references.
    """

    if schema.org_model_id is None:
        return schema
    org = org_resolver(schema.org_model_id)
    if org is None:
        return schema
    return schema.model_copy(update={"org_model": org.model_copy(deep=True)})


def dehydrate_org(schema: ProcessSchema) -> ProcessSchema:
    """Clear the hydrated org master data before persisting a linked schema.

    Keeps the shared org registry the single source of truth: a linked schema
    is stored with an empty embedded ``org_model`` (only ``org_model_id`` is
    persisted). Unlinked schemas are returned unchanged.
    """

    if schema.org_model_id is None:
        return schema
    return schema.model_copy(update={"org_model": OrgModel()})


class ExternalTaskStore(Protocol):
    """Persistence interface for external tasks and their incidents (E11).

    Holds the outbound work queue: automatic ``EXTERNAL_TASK`` steps that have
    been exposed to outside workers, plus the incidents raised when a task's
    retries are exhausted. The runtime is agnostic of the backend, exactly like
    the schema/instance stores.

    Two guarantees hold in every backend, so that parallel workers (threads,
    worker instances, API processes) can never share a task:

    * **One open task per step.** A step (``instance_id``, ``node_id``) holds
      at most one task in :data:`procworks.model.OPEN_EXTERNAL_TASK_STATES`;
      a second one is refused by the store itself (in the database by a
      partial unique index), never left to a check the caller made on an
      older read. A new loop round gets its task once the previous one has
      ended.
    * **Conditional save.** Tasks are saved like cases and models: ``get``
      and ``list_tasks`` hand out detached copies that remember the stored
      version, and a save succeeds only while that version is still stored.
      Incidents have no version (they are only ever added or resolved).
    """

    def add(self, task: ExternalTask) -> bool:
        """Store a **new** task -- unless its step already has an open one.

        :param task: a task that was never stored.
        :returns: ``True`` when it was stored (it then carries version 1);
            ``False`` when its id is taken or (for an open task) its step
            already holds an open task -- someone else created the step's
            task first. Nothing is recorded on the unit of work: losing this
            race is the normal outcome for all but one creator.
        """
        ...

    def put(self, task: ExternalTask) -> ExternalTask:
        """Save ``task`` -- only if nobody saved it since it was read.

        :param task: a task read from this store (or a new one, as in
            :meth:`add`).
        :returns: ``task``, now carrying the new stored version.
        :raises ConcurrentModification: (``kind`` ``"external_task"``) the
            stored version moved on, the task is gone, or a new task's id or
            step is taken. Recorded on the running unit of work, which then
            repeats the request on fresh data.
        """
        ...

    def put_if_unchanged(self, task: ExternalTask) -> bool:
        """Save ``task`` like :meth:`put`, but report a lost race instead of raising.

        For the queue paths that try one candidate after another (fetch,
        push): a task another caller changed since it was read is simply
        skipped, the caller goes on with the next one.

        :param task: a task read from this store.
        :returns: ``True`` when saved (``task`` carries the new version),
            ``False`` when the stored version moved on or the task is gone;
            then nothing is written and nothing is recorded on the unit.
        """
        ...

    def get(self, task_id: str) -> ExternalTask | None: ...

    def list_tasks(self) -> list[ExternalTask]: ...

    def put_incident(self, incident: Incident) -> Incident: ...

    def get_incident(self, incident_id: str) -> Incident | None: ...

    def list_incidents(self) -> list[Incident]: ...

    def clear(self) -> None: ...


class InMemoryExternalTaskStore:
    """A dict-backed store of external tasks and incidents keyed by id.

    The default store without configuration; with ``DATABASE_URL`` set,
    ``create_external_task_store`` returns the durable SQLAlchemy variant
    instead (mirroring the other stores).

    Behaves like the database (see :class:`ExternalTaskStore`): rows
    ``id -> (version, detached copy)``, a conditional save, and at most one
    open task per step -- all checked under one lock, so it also holds for
    callers outside a unit of work (pushes run after the commit, unserialised).
    """

    def __init__(self) -> None:
        self._tasks: dict[str, tuple[int, ExternalTask]] = {}
        self._incidents: dict[str, Incident] = {}
        self._lock = threading.Lock()

    def _write(self, task: ExternalTask) -> bool:
        """Store a copy of ``task`` if the rules allow it (caller holds no lock).

        :param task: the task to save (new, or read from this store).
        :returns: whether it was stored; on ``True`` ``task`` carries the new
            version. ``False``: the stored version moved on (or the row is
            gone), a new task's id is taken, or its step already holds
            another open task.
        """

        base = stored_version(task)
        with self._lock:
            row = self._tasks.get(task.id)
            found = row[0] if row is not None else None
            if base != found:
                return False
            if task.state in OPEN_EXTERNAL_TASK_STATES and any(
                other.id != task.id
                and other.instance_id == task.instance_id
                and other.node_id == task.node_id
                and other.state in OPEN_EXTERNAL_TASK_STATES
                for _, other in self._tasks.values()
            ):
                return False
            version = (found or 0) + 1
            self._tasks[task.id] = (version, task.model_copy(deep=True))
        remember_version(task, version)
        return True

    def add(self, task: ExternalTask) -> bool:
        """See :meth:`ExternalTaskStore.add` (a task carrying a version is not new)."""

        if stored_version(task) is not None:
            return False
        return self._write(task)

    def put(self, task: ExternalTask) -> ExternalTask:
        """See :meth:`ExternalTaskStore.put`."""

        if not self._write(task):
            raise unit_of_work.note_conflict(ConcurrentModification("external_task", task.id))
        return task

    def put_if_unchanged(self, task: ExternalTask) -> bool:
        """See :meth:`ExternalTaskStore.put_if_unchanged`."""

        return self._write(task)

    @staticmethod
    def _detached(row: tuple[int, ExternalTask]) -> ExternalTask:
        version, stored = row
        copy = stored.model_copy(deep=True)
        remember_version(copy, version)
        return copy

    def get(self, task_id: str) -> ExternalTask | None:
        """A detached copy of the stored task, carrying its version."""

        with self._lock:
            row = self._tasks.get(task_id)
            return self._detached(row) if row is not None else None

    def list_tasks(self) -> list[ExternalTask]:
        """Detached copies of all tasks (in the order they were created)."""

        with self._lock:
            return [self._detached(row) for row in self._tasks.values()]

    def put_incident(self, incident: Incident) -> Incident:
        self._incidents[incident.id] = incident
        return incident

    def get_incident(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    def list_incidents(self) -> list[Incident]:
        return list(self._incidents.values())

    def clear(self) -> None:
        self._tasks.clear()
        self._incidents.clear()


def create_external_task_store() -> ExternalTaskStore:
    """Build the external-task store from the environment (mirrors the others)."""

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyExternalTaskStore

        return SqlAlchemyExternalTaskStore(url, create_tables=True)
    return InMemoryExternalTaskStore()


class WebhookStore(Protocol):
    """Persistence for webhook subscriptions, the outbox and the delivery log (E13).

    Backs the event side of the open API: tool subscriptions, the transactional
    outbox of queued deliveries, and the append-only delivery log. The dispatcher
    is agnostic of the backend, exactly like the other stores.
    """

    def put_subscription(self, sub: WebhookSubscription) -> WebhookSubscription: ...

    def get_subscription(self, subscription_id: str) -> WebhookSubscription | None: ...

    def list_subscriptions(self) -> list[WebhookSubscription]: ...

    def delete_subscription(self, subscription_id: str) -> None: ...

    def put_entry(self, entry: OutboxEntry) -> OutboxEntry: ...

    def get_entry(self, entry_id: str) -> OutboxEntry | None: ...

    def list_entries(self) -> list[OutboxEntry]: ...

    def claim_entry(
        self, entry_id: str, *, now: float, lease_until: float
    ) -> OutboxEntry | None:
        """Reserve a due entry for one delivery -- for exactly one caller.

        Due: a state in :data:`~procworks.model.DUE_OUTBOX_STATES` and
        ``next_attempt_at <= now`` (a fresh entry, a back-off that has run out,
        or a lease whose holder never came back). The reservation counts the
        attempt (``attempts + 1``) and moves ``next_attempt_at`` to
        ``lease_until``, decided against the *stored* row in one conditional
        step: of two callers that saw the entry due, only one gets it.

        :param entry_id: the entry to reserve.
        :param now: the caller's clock.
        :param lease_until: end of the reservation; after it the entry is due
            again (the holder is presumed dead).
        :returns: the reserved entry (detached copy), or ``None`` if it is
            unknown, not due, or another caller reserved it first.
        """
        ...

    def discard_entry(self, entry_id: str, *, now: float, reason: str) -> bool:
        """Mark a still open entry ``DISCARDED`` -- also while it is reserved.

        For an entry that must never be delivered again (its subscription was
        deleted). Open means a state in
        :data:`~procworks.model.DUE_OUTBOX_STATES`, whether due, waiting for
        its back-off or reserved by a dispatcher: a reserved delivery that is
        under way completes, but its holder's :meth:`finish_entry` is then
        refused, so the discard stands and nothing is attempted again.

        :param entry_id: the entry.
        :param now: the time of the discard (``next_attempt_at``).
        :param reason: stored in ``last_error``.
        :returns: whether the entry was discarded (``False``: unknown, or
            already delivered, dead or discarded).
        """
        ...

    def finish_entry(self, entry: OutboxEntry, *, lease_until: float) -> bool:
        """Record the outcome of a reserved delivery -- only by the lease holder.

        Stores ``entry`` if the stored row is still reserved with exactly
        ``lease_until`` (the holder's lease). A holder whose lease ran out and
        was taken over by another dispatcher is refused, so it never
        overwrites the newer outcome.

        :param entry: the entry with its outcome (state, attempts, back-off).
        :param lease_until: the lease the caller reserved it with.
        :returns: whether the outcome was stored.
        """
        ...

    def put_delivery(self, delivery: WebhookDelivery) -> WebhookDelivery: ...

    def list_deliveries(self, subscription_id: str | None = None) -> list[WebhookDelivery]: ...

    def clear(self) -> None: ...


class _LeasedEntries[EntryT: (OutboxEntry, MailOutboxEntry)]:
    """In-memory outbox entries with the reservation rule of the database stores.

    Shared by the webhook and the mail outbox. Deliveries run after the
    commit, outside the in-memory serialisation of requests, so two threads
    may dispatch at the same time: every read and write happens under one
    lock, and entries go in and out as **copies** -- a dispatcher changing
    its entry while delivering must not change what another one reads, and
    the outcome check compares against what is stored, not against the
    caller's own object.

    :param due_states: the states in which an entry may still be delivered
        (:data:`~procworks.model.DUE_OUTBOX_STATES` or
        :data:`~procworks.model.DUE_MAIL_OUTBOX_STATES`).
    """

    def __init__(self, due_states: Collection[object]) -> None:
        self._due = due_states
        self._entries: dict[str, EntryT] = {}
        self._lock = threading.Lock()

    def put(self, entry: EntryT) -> EntryT:
        """Store a copy of ``entry`` unconditionally; return ``entry`` itself."""

        with self._lock:
            self._entries[entry.id] = entry.model_copy(deep=True)
        return entry

    def get(self, entry_id: str) -> EntryT | None:
        """A copy of the stored entry, or ``None``."""

        with self._lock:
            stored = self._entries.get(entry_id)
            return stored.model_copy(deep=True) if stored is not None else None

    def list(self) -> list[EntryT]:
        """Copies of all stored entries, in insertion order."""

        with self._lock:
            return [e.model_copy(deep=True) for e in self._entries.values()]

    def claim(self, entry_id: str, *, now: float, lease_until: float) -> EntryT | None:
        """Reserve a due entry (see :meth:`WebhookStore.claim_entry`).

        :returns: a copy of the reserved entry, or ``None`` if it is unknown,
            not due or already reserved.
        """

        with self._lock:
            stored = self._entries.get(entry_id)
            if (
                stored is None
                or stored.state not in self._due
                or stored.next_attempt_at > now
            ):
                return None
            claimed = stored.model_copy(deep=True)
            claimed.attempts += 1
            claimed.next_attempt_at = lease_until
            self._entries[entry_id] = claimed.model_copy(deep=True)
            return claimed

    def finish(self, entry: EntryT, *, lease_until: float) -> bool:
        """Store the outcome if the caller still holds the lease (see
        :meth:`WebhookStore.finish_entry`)."""

        with self._lock:
            stored = self._entries.get(entry.id)
            if (
                stored is None
                or stored.state not in self._due
                or stored.next_attempt_at != lease_until
            ):
                return False
            self._entries[entry.id] = entry.model_copy(deep=True)
            return True

    def discard(self, entry_id: str, *, now: float, reason: str, state: object) -> bool:
        """Close a still open entry for good (see :meth:`WebhookStore.discard_entry`).

        :param state: the closing state (``OutboxState.DISCARDED``).
        :returns: whether the entry was open and is now closed.
        """

        with self._lock:
            stored = self._entries.get(entry_id)
            if stored is None or stored.state not in self._due:
                return False
            self._entries[entry_id] = stored.model_copy(
                update={"state": state, "next_attempt_at": now, "last_error": reason},
                deep=True,
            )
            return True

    def find(self, predicate: Callable[[EntryT], bool]) -> EntryT | None:
        """A copy of the first stored entry matching ``predicate``, or ``None``."""

        with self._lock:
            for entry in self._entries.values():
                if predicate(entry):
                    return entry.model_copy(deep=True)
        return None

    def clear(self) -> None:
        """Remove every entry."""

        with self._lock:
            self._entries.clear()


class InMemoryWebhookStore:
    """A dict-backed store of subscriptions, outbox entries and deliveries.

    The default store without configuration; with ``DATABASE_URL`` set,
    ``create_webhook_store`` returns the durable SQLAlchemy variant instead
    (mirroring the other stores).
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, WebhookSubscription] = {}
        self._entries: _LeasedEntries[OutboxEntry] = _LeasedEntries(DUE_OUTBOX_STATES)
        self._deliveries: list[WebhookDelivery] = []

    def put_subscription(self, sub: WebhookSubscription) -> WebhookSubscription:
        self._subscriptions[sub.id] = sub
        return sub

    def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        return self._subscriptions.get(subscription_id)

    def list_subscriptions(self) -> list[WebhookSubscription]:
        return list(self._subscriptions.values())

    def delete_subscription(self, subscription_id: str) -> None:
        self._subscriptions.pop(subscription_id, None)

    def put_entry(self, entry: OutboxEntry) -> OutboxEntry:
        return self._entries.put(entry)

    def get_entry(self, entry_id: str) -> OutboxEntry | None:
        return self._entries.get(entry_id)

    def list_entries(self) -> list[OutboxEntry]:
        return self._entries.list()

    def claim_entry(
        self, entry_id: str, *, now: float, lease_until: float
    ) -> OutboxEntry | None:
        """See :meth:`WebhookStore.claim_entry` (decided under the store's lock)."""

        return self._entries.claim(entry_id, now=now, lease_until=lease_until)

    def finish_entry(self, entry: OutboxEntry, *, lease_until: float) -> bool:
        """See :meth:`WebhookStore.finish_entry` (decided under the store's lock)."""

        return self._entries.finish(entry, lease_until=lease_until)

    def discard_entry(self, entry_id: str, *, now: float, reason: str) -> bool:
        """See :meth:`WebhookStore.discard_entry` (decided under the store's lock)."""

        return self._entries.discard(
            entry_id, now=now, reason=reason, state=OutboxState.DISCARDED
        )

    def put_delivery(self, delivery: WebhookDelivery) -> WebhookDelivery:
        self._deliveries.append(delivery)
        return delivery

    def list_deliveries(
        self, subscription_id: str | None = None
    ) -> list[WebhookDelivery]:
        if subscription_id is None:
            return list(self._deliveries)
        return [d for d in self._deliveries if d.subscription_id == subscription_id]

    def clear(self) -> None:
        self._subscriptions.clear()
        self._entries.clear()
        self._deliveries.clear()


def create_webhook_store() -> WebhookStore:
    """Build the webhook store from the environment (mirrors the others)."""

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyWebhookStore

        return SqlAlchemyWebhookStore(url, create_tables=True)
    return InMemoryWebhookStore()


class MailOutboxStore(Protocol):
    """Persistence for the durable mail outbox (modelled notifications, rule N).

    The SMTP-channel sibling of :class:`WebhookStore`: it holds the transactional
    queue of rendered e-mail notifications so a task-ready notification survives a
    crash and can be retried. ``find_by_dedup_key`` backs the per-activation
    idempotency (an activation is enqueued at most once). The dispatcher in
    :mod:`procworks.mail_runtime` is agnostic of the backend, exactly like the
    other stores.
    """

    def put_entry(self, entry: MailOutboxEntry) -> MailOutboxEntry: ...

    def get_entry(self, entry_id: str) -> MailOutboxEntry | None: ...

    def find_by_dedup_key(self, dedup_key: str) -> MailOutboxEntry | None: ...

    def list_entries(self) -> list[MailOutboxEntry]: ...

    def claim_entry(
        self, entry_id: str, *, now: float, lease_until: float
    ) -> MailOutboxEntry | None:
        """Reserve a due entry for one send -- for exactly one caller.

        The rule of :meth:`WebhookStore.claim_entry`, with
        :data:`~procworks.model.DUE_MAIL_OUTBOX_STATES`.
        """
        ...

    def finish_entry(self, entry: MailOutboxEntry, *, lease_until: float) -> bool:
        """Record the outcome of a reserved send -- only by the lease holder.

        The rule of :meth:`WebhookStore.finish_entry`.
        """
        ...

    def clear(self) -> None: ...


class InMemoryMailOutboxStore:
    """A dict-backed mail outbox, keyed by entry id with a dedup-key index.

    The default store without configuration; with ``DATABASE_URL`` set,
    ``create_mail_outbox_store`` returns the durable SQLAlchemy variant instead
    (mirroring the webhook outbox).
    """

    def __init__(self) -> None:
        self._entries: _LeasedEntries[MailOutboxEntry] = _LeasedEntries(
            DUE_MAIL_OUTBOX_STATES
        )

    def put_entry(self, entry: MailOutboxEntry) -> MailOutboxEntry:
        return self._entries.put(entry)

    def get_entry(self, entry_id: str) -> MailOutboxEntry | None:
        return self._entries.get(entry_id)

    def find_by_dedup_key(self, dedup_key: str) -> MailOutboxEntry | None:
        """Return the (single) entry queued for ``dedup_key``, or ``None``.

        Backs enqueue idempotency: a re-observed activation resolves to the same
        key and is not queued twice. Linear scan is fine for the in-memory store;
        the SQLAlchemy variant uses an indexed column.
        """

        return self._entries.find(lambda entry: entry.dedup_key == dedup_key)

    def list_entries(self) -> list[MailOutboxEntry]:
        return self._entries.list()

    def claim_entry(
        self, entry_id: str, *, now: float, lease_until: float
    ) -> MailOutboxEntry | None:
        """See :meth:`MailOutboxStore.claim_entry` (decided under the store's lock)."""

        return self._entries.claim(entry_id, now=now, lease_until=lease_until)

    def finish_entry(self, entry: MailOutboxEntry, *, lease_until: float) -> bool:
        """See :meth:`MailOutboxStore.finish_entry` (decided under the store's lock)."""

        return self._entries.finish(entry, lease_until=lease_until)

    def clear(self) -> None:
        self._entries.clear()


def create_mail_outbox_store() -> MailOutboxStore:
    """Build the mail outbox store from the environment (mirrors the others)."""

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyMailOutboxStore

        return SqlAlchemyMailOutboxStore(url, create_tables=True)
    return InMemoryMailOutboxStore()


class AbsenceStore(Protocol):
    """Persistence for recorded agent absences (deputy substitution windows).

    An operational store like the mail outbox: it holds the (small) set of
    absence entries that gate runtime deputy substitution. It carries no
    correctness logic -- the API boundary reads it, resolves which agents are
    absent *now*, and hands that set to the eligibility resolution. Swappable
    in-memory / SQLAlchemy exactly like the other stores.
    """

    def put_entry(self, entry: AbsenceEntry) -> AbsenceEntry: ...

    def get_entry(self, entry_id: str) -> AbsenceEntry | None: ...

    def list_entries(self) -> list[AbsenceEntry]: ...

    def delete_entry(self, entry_id: str) -> bool: ...

    def clear(self) -> None: ...


class InMemoryAbsenceStore:
    """A dict-backed absence store, keyed by entry id.

    The default store without configuration; with ``DATABASE_URL`` set,
    ``create_absence_store`` returns the durable SQLAlchemy variant instead.
    """

    def __init__(self) -> None:
        self._entries: dict[str, AbsenceEntry] = {}

    def put_entry(self, entry: AbsenceEntry) -> AbsenceEntry:
        self._entries[entry.id] = entry
        return entry

    def get_entry(self, entry_id: str) -> AbsenceEntry | None:
        return self._entries.get(entry_id)

    def list_entries(self) -> list[AbsenceEntry]:
        return list(self._entries.values())

    def delete_entry(self, entry_id: str) -> bool:
        """Remove an entry; return whether it existed (idempotent delete)."""

        return self._entries.pop(entry_id, None) is not None

    def clear(self) -> None:
        self._entries.clear()


def create_absence_store() -> AbsenceStore:
    """Build the absence store from the environment (mirrors the others)."""

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyAbsenceStore

        return SqlAlchemyAbsenceStore(url, create_tables=True)
    return InMemoryAbsenceStore()


class TemplateStore(Protocol):
    """Persistence for *user-created* process templates (blueprints).

    Only user templates live here; built-in templates ship as code
    (:mod:`procworks.templates`) so they are always available and survive a
    reset. The store is swappable in-memory / SQLAlchemy exactly like the other
    stores, and carries no correctness logic -- a template's embedded blueprint
    is validated by the operation that produces it (validate-before-commit).
    """

    def put(self, template: ProcessTemplate) -> ProcessTemplate: ...

    def get(self, template_id: str) -> ProcessTemplate | None: ...

    def list_ids(self) -> list[str]: ...

    def delete(self, template_id: str) -> bool: ...

    def clear(self) -> None: ...


class InMemoryTemplateStore:
    """A dict-backed template store keyed by template id (default for tests)."""

    def __init__(self) -> None:
        self._templates: dict[str, ProcessTemplate] = {}

    def put(self, template: ProcessTemplate) -> ProcessTemplate:
        self._templates[template.id] = template
        return template

    def get(self, template_id: str) -> ProcessTemplate | None:
        return self._templates.get(template_id)

    def list_ids(self) -> list[str]:
        return list(self._templates.keys())

    def delete(self, template_id: str) -> bool:
        """Remove a template; return whether it existed (idempotent delete)."""

        return self._templates.pop(template_id, None) is not None

    def clear(self) -> None:
        self._templates.clear()


def create_template_store() -> TemplateStore:
    """Build the template store from the environment (mirrors the others)."""

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyTemplateStore

        return SqlAlchemyTemplateStore(url, create_tables=True)
    return InMemoryTemplateStore()

