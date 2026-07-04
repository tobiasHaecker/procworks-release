# SPDX-License-Identifier: BUSL-1.1
"""External-task runtime for the outbound integration boundary (roadmap E11).

This is the *pull* side of the maximally open API (concept §6): automatic
``EXTERNAL_TASK`` activities are exposed as a work queue that outside workers
fetch-and-lock, run, and report back to. The runtime is a thin boundary driver
around the pure engine -- it never mutates engine state itself, it only resolves
the input data package, then calls :func:`procworks.execution.complete_activity`
on completion. The kernel stays pure (the integration layer calls the kernel,
never the other way round).

Robustness properties (concept §6.2):

* **Lazy materialisation** -- a task is created when an automatic step is
  *activated*, discovered on the next fetch-and-lock scan. The engine is not
  hooked, so the core is unaffected.
* **Locking** -- a fetched task is locked to a worker for a visibility window;
  an expired lock is reclaimed automatically on the next fetch.
* **Exactly-once completion** -- completion is only accepted while the task is
  ``LOCKED`` by the reporting worker; a duplicate report finds the task already
  ``COMPLETED`` and is rejected. State transitions are the single source of
  truth.
* **Retries / incidents** -- a failure decrements the remaining retries and
  re-queues the task with a back-off; once retries are exhausted the task
  becomes an ``INCIDENT`` (dead-letter) for an operator to resolve.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping

from procworks import execution as exe
from procworks.dal import DataAccessError, DataAccessLayer
from procworks.model import (
    PRIORITY_RANK,
    READ_MODES,
    WRITE_MODES,
    AutomationKind,
    DataSourceKind,
    ExternalTask,
    ExternalTaskState,
    Incident,
    InstanceState,
    NodeState,
    NodeType,
    PriorityLevel,
    ProcessInstance,
    ProcessSchema,
    ServiceBinding,
    value_matches_type,
)
from procworks.store import ExternalTaskStore, InstanceStore

#: Default base back-off (ms) for a re-queued failure when the worker does not
#: supply an explicit ``retry_timeout_ms``. Doubled per attempt up to the cap.
_BACKOFF_BASE_MS = 2000
#: Upper bound (ms) for the exponential back-off so it never grows unbounded.
_BACKOFF_CAP_MS = 60_000
#: Notional starting retry budget used to derive the attempt index for the
#: exponential back-off when a task does not carry its original budget.
_RETRY_BUDGET = 5

#: Task states that still occupy the step (so no duplicate task is materialised).
_OPEN_STATES = frozenset(
    {
        ExternalTaskState.CREATED,
        ExternalTaskState.LOCKED,
        ExternalTaskState.INCIDENT,
        ExternalTaskState.BPMN_ERROR,
    }
)


class ExternalTaskError(Exception):
    """A boundary error in the external-task runtime, carrying an HTTP status.

    ``status`` lets the API map the failure precisely: ``404`` for an unknown
    task, ``409`` for a lock/state conflict (e.g. a stale or duplicate report),
    ``422`` for an invalid output payload.
    """

    def __init__(self, message: str, status: int = 409) -> None:
        self.message = message
        self.status = status
        super().__init__(message)


def _backoff_ms(retries_left: int) -> float:
    """Exponential back-off (ms) derived from how many attempts were used."""

    attempt = max(0, _RETRY_BUDGET - retries_left)
    return float(min(_BACKOFF_BASE_MS * (2**attempt), _BACKOFF_CAP_MS))


def _resolve_inputs(
    schema: ProcessSchema,
    instance: ProcessInstance,
    node_id: str,
    binding: ServiceBinding | None,
) -> dict[str, object]:
    """Build the input data package handed to the worker (concept §6.2).

    Mapped template parameters are exposed under their parameter name; declared
    READ accesses are additionally exposed under their data-element id, so a
    worker can address either. Only values present on the instance are included.
    """

    data: dict[str, object] = {}
    if binding is not None:
        for param, element_id in binding.parameter_mapping.items():
            if element_id in instance.data_values:
                data[param] = instance.data_values[element_id]
    for access in schema.accesses_of(node_id):
        if access.mode in READ_MODES and access.element_id in instance.data_values:
            data.setdefault(access.element_id, instance.data_values[access.element_id])
    return data


def _resolve_outputs(
    schema: ProcessSchema,
    node_id: str,
    binding: ServiceBinding | None,
    variables: dict[str, object],
) -> tuple[dict[str, object], dict[str, dict[str, object]], dict[str, object]]:
    """Translate reported output variables into validated data-element writes.

    A worker may address an output by template parameter name (mapped back to
    its element) or directly by data-element id. Every target must be a declared
    WRITE access (or a mapped parameter) of the step -- otherwise the report is
    rejected (422). Three kinds of write are returned separately:

    * **INSTANCE** elements: the value must match the element's declared type and
      is written into the process instance by the pure engine.
    * **EXTERNAL record** elements: the value must be a record (mapping of fields)
      and is post-flushed as a whole record through the Data Access Layer.
    * **EXTERNAL scalar-write** elements: the value must be a single typed scalar
      and is post-flushed via a parameterized ``UPDATE`` (C7-C9).
    """

    reverse: dict[str, str] = {}
    writable: set[str] = set()
    if binding is not None:
        reverse = {param: element for param, element in binding.parameter_mapping.items()}
        writable |= set(binding.parameter_mapping.values())
    writable |= {a.element_id for a in schema.accesses_of(node_id) if a.mode in WRITE_MODES}

    instance_out: dict[str, object] = {}
    external_out: dict[str, dict[str, object]] = {}
    scalar_out: dict[str, object] = {}
    for key, value in variables.items():
        element_id = reverse.get(key, key)
        if element_id not in writable:
            raise ExternalTaskError(
                f"'{key}' is not a writable output of activity '{node_id}'", 422
            )
        element = schema.data_elements.get(element_id)
        if element is None:
            raise ExternalTaskError(f"unknown data element '{element_id}'", 422)
        if element.source is DataSourceKind.EXTERNAL:
            if element.write is not None:
                if not value_matches_type(element.data_type, value):
                    raise ExternalTaskError(
                        f"scalar write '{element_id}' is not a "
                        f"{element.data_type.value}",
                        422,
                    )
                scalar_out[element_id] = value
                continue
            if not isinstance(value, Mapping):
                raise ExternalTaskError(
                    f"external write '{element_id}' must be a record (object)", 422
                )
            external_out[element_id] = dict(value)
            continue
        if not value_matches_type(element.data_type, value):
            raise ExternalTaskError(
                f"value for '{element_id}' is not a {element.data_type.value}", 422
            )
        instance_out[element_id] = value
    return instance_out, external_out, scalar_out


class ExternalTaskRuntime:
    """Boundary driver that exposes automatic steps as an external work queue.

    Wires the external-task store to the instance store and the pure engine. The
    ``schema_for`` callable yields the effective (hydrated, possibly ad-hoc)
    schema of an instance, and ``context`` is the engine's execution context so
    a completion can advance composed processes exactly like an interactive one.

    With a ``dal`` (Data Access Layer) the boundary also performs bidirectional
    data exchange (concept §7.1): READ accesses on EXTERNAL elements are
    *pre-fetched* into the worker's input package at lock time, and WRITE accesses
    on EXTERNAL elements are *post-flushed* to the bound connector on completion.
    """

    def __init__(
        self,
        tasks: ExternalTaskStore,
        instances: InstanceStore,
        schema_for: Callable[[ProcessInstance], ProcessSchema],
        context: exe.ExecutionContext,
        *,
        dal: DataAccessLayer | None = None,
        now: Callable[[], float] | None = None,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
        on_push: Callable[[ServiceBinding, dict[str, object]], None] | None = None,
    ) -> None:
        self._tasks = tasks
        self._instances = instances
        self._schema_for = schema_for
        self._context = context
        self._dal = dal
        self._now = now or time.time
        self._on_event = on_event
        self._on_push = on_push

    def _emit(self, event_type: str, **data: object) -> None:
        """Forward a domain event to the optional sink (event side, E13).

        Best-effort and boundary-only: the sink (the webhook outbox) never feeds
        back into the pure engine, and a sink that is not wired is simply a no-op.
        """

        if self._on_event is not None:
            self._on_event(event_type, dict(data))

    # -- materialisation ---------------------------------------------------

    def sync(self) -> list[ExternalTask]:
        """Materialise tasks for newly activated automatic EXTERNAL_TASK steps.

        Scans running instances and creates a ``CREATED`` task for every
        activated automatic ``EXTERNAL_TASK`` activity that has no open task yet.
        Idempotent: re-running it does not duplicate tasks.
        """

        return self._materialise(AutomationKind.EXTERNAL_TASK)

    def _materialise(self, kind: AutomationKind) -> list[ExternalTask]:
        """Create ``CREATED`` tasks for newly activated automatic ``kind`` steps.

        Shared by the pull side (``EXTERNAL_TASK`` via :meth:`sync`) and the push
        side (``HTTP_PUSH`` via :meth:`drive_push`). A push task carries an empty
        ``topic`` (it is never fetch-and-locked); a pull task carries its binding
        topic. Idempotent across both: a step with any open task is skipped.
        """

        open_by_step: set[tuple[str, str]] = {
            (t.instance_id, t.node_id)
            for t in self._tasks.list_tasks()
            if t.state in _OPEN_STATES
        }
        created: list[ExternalTask] = []
        for instance_id in self._instances.list_ids():
            instance = self._instances.get(instance_id)
            if instance is None or instance.state is not InstanceState.RUNNING:
                continue
            schema = self._schema_for(instance)
            for node_id, node_state in instance.node_states.items():
                if node_state is not NodeState.ACTIVATED:
                    continue
                node = schema.nodes.get(node_id)
                if node is None or node.type is not NodeType.ACTIVITY:
                    continue
                binding = schema.service_bindings.get(node_id)
                if binding is None or binding.automation is not kind:
                    continue
                if kind is AutomationKind.EXTERNAL_TASK and not binding.topic:
                    continue
                if kind is AutomationKind.HTTP_PUSH and not binding.endpoint_ref:
                    continue
                if (instance.id, node_id) in open_by_step:
                    continue
                topic = binding.topic if kind is AutomationKind.EXTERNAL_TASK else ""
                task = ExternalTask(
                    id=f"et_{uuid.uuid4().hex}",
                    instance_id=instance.id,
                    node_id=node_id,
                    topic=topic or "",
                    retries_left=binding.retry_max,
                    input_variables=_resolve_inputs(schema, instance, node_id, binding),
                    priority=self._priority_of(schema, node_id),
                )
                self._tasks.put(task)
                open_by_step.add((instance.id, node_id))
                created.append(task)
                self._emit(
                    "task.ready",
                    task_id=task.id,
                    instance_id=task.instance_id,
                    node_id=task.node_id,
                    topic=task.topic,
                )
        return created

    def drive_push(self) -> list[ExternalTask]:
        """Push activated automatic ``HTTP_PUSH`` steps to their tool endpoint.

        The *push* side of the outbound boundary (concept §6.3): instead of a
        worker pulling work, ProcWorks proactively calls a server-configured
        endpoint with the step's input package. This is **best-effort and
        idempotent** -- it never advances or mutates the pure engine itself:

        * Materialises a ``CREATED`` push task per newly activated step (the
          idempotency anchor: a step with an open task is not pushed twice).
        * For each due ``CREATED`` push task it pre-fetches external READ data,
          then hands ``(binding, payload)`` to the ``on_push`` sink (the outbox).
          On success the task moves to ``LOCKED`` with a generated callback
          token (no lock expiry), so the tool can later report via the regular
          ``/v1/external-tasks/{id}/complete`` endpoint (exactly-once).
        * A push that cannot be enqueued (endpoint unconfigured, connector down,
          SSRF-rejected) leaves the task ``CREATED`` to be retried on a later
          drive; the engine and the instance are untouched.

        Returns the tasks that were pushed in this pass. A no-op when no
        ``on_push`` sink is wired.
        """

        if self._on_push is None:
            return []
        self._materialise(AutomationKind.HTTP_PUSH)
        now = self._now()
        pushed: list[ExternalTask] = []
        for task in self._tasks.list_tasks():
            if task.state is not ExternalTaskState.CREATED or task.topic:
                continue  # not a pending push task
            if task.available_at is not None and task.available_at > now:
                continue  # still in back-off after a reported failure
            instance = self._instances.get(task.instance_id)
            if instance is None:
                continue
            schema = self._schema_for(instance)
            binding = schema.service_bindings.get(task.node_id)
            if (
                binding is None
                or binding.automation is not AutomationKind.HTTP_PUSH
                or not binding.endpoint_ref
            ):
                continue
            task.input_variables = _resolve_inputs(
                schema, instance, task.node_id, binding
            )
            try:
                self._prefetch_external(task)
            except ExternalTaskError:
                continue  # connector unavailable -> retry on next drive
            token = f"push_{uuid.uuid4().hex}"
            payload: dict[str, object] = {
                "task_id": task.id,
                "instance_id": task.instance_id,
                "node_id": task.node_id,
                "callback_token": token,
                "variables": dict(task.input_variables),
            }
            try:
                self._on_push(binding, payload)
            except Exception:  # noqa: BLE001 -- a push failure must never corrupt state
                continue  # leave CREATED; retried on a later drive
            task.state = ExternalTaskState.LOCKED
            task.worker_id = token
            task.lock_expires_at = None
            task.available_at = None
            self._tasks.put(task)
            pushed.append(task)
        return pushed

    @staticmethod
    def _priority_of(schema: ProcessSchema, node_id: str) -> PriorityLevel:
        prio = schema.node_priorities.get(node_id)
        return prio.level if prio is not None else PriorityLevel.MEDIUM

    # -- fetch / lock ------------------------------------------------------

    def fetch_and_lock(
        self,
        worker_id: str,
        topics: list[str],
        *,
        lock_ms: int,
        max_tasks: int = 1,
        use_priority: bool = True,
    ) -> list[ExternalTask]:
        """Atomically claim up to ``max_tasks`` tasks for the given topics.

        Materialises pending tasks first, reclaims expired locks, then locks the
        selected tasks to ``worker_id`` for ``lock_ms`` milliseconds. With
        ``use_priority`` the highest-priority tasks are served first.
        """

        self.sync()
        now = self._now()
        wanted = set(topics)
        available: list[ExternalTask] = []
        for task in self._tasks.list_tasks():
            if task.topic not in wanted:
                continue
            if task.state is ExternalTaskState.CREATED:
                if task.available_at is not None and task.available_at > now:
                    continue
            elif (
                task.state is ExternalTaskState.LOCKED
                and task.lock_expires_at is not None
                and task.lock_expires_at <= now
            ):
                pass  # expired lock -> reclaimable
            else:
                continue
            available.append(task)

        if use_priority:
            available.sort(key=lambda t: PRIORITY_RANK[t.priority], reverse=True)

        locked: list[ExternalTask] = []
        for task in available[: max(0, max_tasks)]:
            self._prefetch_external(task)
            task.state = ExternalTaskState.LOCKED
            task.worker_id = worker_id
            task.lock_expires_at = now + lock_ms / 1000.0
            task.available_at = None
            self._tasks.put(task)
            locked.append(task)
        return locked

    def _prefetch_external(self, task: ExternalTask) -> None:
        """Pre-fetch EXTERNAL READ elements into the task's input package.

        Runs at lock time so the worker receives fresh business data alongside
        the instance-local inputs. A connector failure surfaces as a 502 so the
        task stays fetchable rather than being silently served without data.
        """

        if self._dal is None:
            return
        instance = self._instances.get(task.instance_id)
        if instance is None:
            return
        schema = self._schema_for(instance)
        for access in schema.accesses_of(task.node_id):
            if access.mode not in READ_MODES:
                continue
            element = schema.data_elements.get(access.element_id)
            if element is None or element.source is not DataSourceKind.EXTERNAL:
                continue
            try:
                if element.select is not None:
                    value = self._dal.read_scalar(
                        schema, instance.data_values, access.element_id
                    )
                    if value is not None and not value_matches_type(
                        element.data_type, value
                    ):
                        raise ExternalTaskError(
                            f"pre-fetch of '{access.element_id}' returned a value "
                            f"that is not a {element.data_type.value}",
                            502,
                        )
                    task.input_variables[access.element_id] = value
                else:
                    record = self._dal.read(
                        schema, instance.data_values, access.element_id
                    )
                    task.input_variables[access.element_id] = dict(record)
            except DataAccessError as err:
                raise ExternalTaskError(
                    f"pre-fetch of '{access.element_id}' failed: {err}", 502
                ) from err

    def extend_lock(self, task_id: str, worker_id: str, lock_ms: int) -> ExternalTask:
        """Prolong a held lock by ``lock_ms`` for a long-running worker."""

        now = self._now()
        task = self._require_locked(task_id, worker_id, now)
        task.lock_expires_at = now + lock_ms / 1000.0
        return self._tasks.put(task)

    def unlock(self, task_id: str, worker_id: str) -> ExternalTask:
        """Release a held lock, returning the task to the queue immediately."""

        now = self._now()
        task = self._require_locked(task_id, worker_id, now)
        task.state = ExternalTaskState.CREATED
        task.worker_id = None
        task.lock_expires_at = None
        task.available_at = None
        return self._tasks.put(task)

    # -- completion / failure ---------------------------------------------

    def complete(
        self, task_id: str, worker_id: str, variables: dict[str, object]
    ) -> ExternalTask:
        """Apply the worker's outputs and advance the instance (exactly-once).

        Validates the lock, maps the reported variables to data-element writes,
        then calls the pure engine's ``complete_activity``. A duplicate report
        is rejected because the task is no longer ``LOCKED``.
        """

        now = self._now()
        task = self._require_locked(task_id, worker_id, now)
        instance = self._instances.get(task.instance_id)
        if instance is None:
            raise ExternalTaskError(
                f"instance '{task.instance_id}' of task '{task_id}' is gone", 409
            )
        schema = self._schema_for(instance)
        binding = schema.service_bindings.get(task.node_id)
        instance_out, external_out, scalar_out = _resolve_outputs(
            schema, task.node_id, binding, variables
        )
        if (external_out or scalar_out) and self._dal is None:
            raise ExternalTaskError(
                "external write requires a configured connector", 422
            )
        # Post-flush external writes *before* advancing the engine: a connector
        # failure then leaves the task LOCKED and the instance untouched, so the
        # worker can retry without the step having moved on.
        for element_id, record in external_out.items():
            assert self._dal is not None
            try:
                self._dal.write(schema, instance.data_values, element_id, record)
            except DataAccessError as err:
                raise ExternalTaskError(
                    f"post-flush of '{element_id}' failed: {err}", 502
                ) from err
        for element_id, scalar in scalar_out.items():
            assert self._dal is not None
            try:
                self._dal.write_scalar(
                    schema, instance.data_values, element_id, scalar
                )
            except DataAccessError as err:
                raise ExternalTaskError(
                    f"post-flush of '{element_id}' failed: {err}", 502
                ) from err
        try:
            advanced = exe.complete_activity(
                instance, schema, task.node_id, instance_out, context=self._context
            )
        except exe.ExecutionError as err:
            raise ExternalTaskError(err.message, 409) from err
        self._instances.put(advanced)
        task.state = ExternalTaskState.COMPLETED
        task.lock_expires_at = None
        task.available_at = None
        task.instance_revision_guard += 1
        stored = self._tasks.put(task)
        self._emit(
            "task.completed",
            task_id=task.id,
            instance_id=task.instance_id,
            node_id=task.node_id,
            topic=task.topic,
        )
        return stored

    def failure(
        self,
        task_id: str,
        worker_id: str,
        error_message: str,
        *,
        retries: int | None = None,
        retry_timeout_ms: int | None = None,
    ) -> ExternalTask:
        """Report a failure: re-queue with back-off, or raise an incident.

        ``retries`` overrides the remaining retry count (Camunda-style); when
        omitted the count is decremented by one. With retries remaining the task
        is re-queued and becomes fetchable again after a back-off (or after the
        explicit ``retry_timeout_ms``). With none remaining it becomes an
        ``INCIDENT`` for an operator to resolve.
        """

        now = self._now()
        task = self._require_locked(task_id, worker_id, now)
        remaining = retries if retries is not None else task.retries_left - 1
        remaining = max(0, remaining)
        task.worker_id = None
        task.lock_expires_at = None
        if remaining > 0:
            task.retries_left = remaining
            task.state = ExternalTaskState.CREATED
            backoff = (
                float(retry_timeout_ms)
                if retry_timeout_ms is not None
                else _backoff_ms(remaining)
            )
            task.available_at = now + backoff / 1000.0
            return self._tasks.put(task)
        task.retries_left = 0
        task.state = ExternalTaskState.INCIDENT
        task.available_at = None
        self._tasks.put(task)
        incident = Incident(
            id=f"inc_{uuid.uuid4().hex}",
            external_task_id=task.id,
            instance_id=task.instance_id,
            node_id=task.node_id,
            message=error_message,
            created_at=now,
        )
        self._tasks.put_incident(incident)
        self._emit(
            "task.incident",
            task_id=task.id,
            instance_id=task.instance_id,
            node_id=task.node_id,
            topic=task.topic,
            incident_id=incident.id,
            message=error_message,
        )
        return task

    def bpmn_error(self, task_id: str, worker_id: str, error_code: str) -> ExternalTask:
        """Record a business (BPMN) error reported by the worker.

        The step stays activated in the pure engine; the error is surfaced for
        handling (error boundary routing is a later roadmap step).
        """

        now = self._now()
        task = self._require_locked(task_id, worker_id, now)
        task.state = ExternalTaskState.BPMN_ERROR
        task.error_code = error_code
        task.worker_id = None
        task.lock_expires_at = None
        task.available_at = None
        return self._tasks.put(task)

    # -- queries / operator actions ---------------------------------------

    def get(self, task_id: str) -> ExternalTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[ExternalTask]:
        return self._tasks.list_tasks()

    def list_incidents(self, *, unresolved_only: bool = False) -> list[Incident]:
        incidents = self._tasks.list_incidents()
        if unresolved_only:
            return [i for i in incidents if not i.resolved]
        return incidents

    def resolve_incident(self, incident_id: str) -> Incident:
        """Resolve an incident and re-queue its task for another attempt."""

        incident = self._tasks.get_incident(incident_id)
        if incident is None:
            raise ExternalTaskError(f"incident '{incident_id}' not found", 404)
        task = self._tasks.get(incident.external_task_id)
        if task is not None and task.state is ExternalTaskState.INCIDENT:
            instance = self._instances.get(task.instance_id)
            budget = _RETRY_BUDGET
            if instance is not None:
                binding = self._schema_for(instance).service_bindings.get(task.node_id)
                if binding is not None:
                    budget = binding.retry_max
            task.state = ExternalTaskState.CREATED
            task.retries_left = max(1, budget)
            task.worker_id = None
            task.lock_expires_at = None
            task.available_at = None
            self._tasks.put(task)
        incident.resolved = True
        return self._tasks.put_incident(incident)

    # -- internal ----------------------------------------------------------

    def _require_locked(
        self, task_id: str, worker_id: str, now: float
    ) -> ExternalTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise ExternalTaskError(f"external task '{task_id}' not found", 404)
        if task.state is not ExternalTaskState.LOCKED:
            raise ExternalTaskError(
                f"task '{task_id}' is not locked (state {task.state.value})", 409
            )
        if task.worker_id != worker_id:
            raise ExternalTaskError(
                f"task '{task_id}' is locked by another worker", 409
            )
        if task.lock_expires_at is not None and task.lock_expires_at <= now:
            raise ExternalTaskError(f"lock for task '{task_id}' has expired", 409)
        return task
