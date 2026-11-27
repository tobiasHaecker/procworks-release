# SPDX-License-Identifier: BUSL-1.1
"""External-task runtime for the outbound integration boundary (roadmap E11).

This is the *pull* side of the maximally open API: automatic
``EXTERNAL_TASK`` activities are exposed as a work queue that outside workers
fetch-and-lock, run, and report back to. The runtime is a thin boundary driver
around the pure engine -- it never mutates engine state itself, it only resolves
the input data package, then calls :func:`procworks.execution.complete_activity`
on completion. The kernel stays pure (the integration layer calls the kernel,
never the other way round).

Robustness properties:

* **Lazy materialisation** -- a task is created when an automatic step is
  *activated*, discovered on the next fetch-and-lock scan. The engine is not
  hooked, so the core is unaffected.
* **Locking** -- a fetched task is locked to a worker for a visibility window;
  an expired lock is reclaimed automatically on the next fetch.
* **Handed out once** -- parallel fetches (threads, worker instances, API
  processes) never share a task: a step holds at most one open task (the
  store refuses a second, in the database by a unique index), and a task is
  locked by a conditional save -- of two callers that read it, only the first
  locks it, the other moves on to the next candidate. A push is claimed the
  same way *before* it is delivered.
* **Only for active steps** -- a task whose case has ended or whose step is
  no longer active is never handed out or pushed; the queue closes it as
  ``CANCELLED`` (``cancel_reason`` ``"step-ended"``).
* **Exactly-once completion** -- completion is only accepted while the task is
  ``LOCKED`` by the reporting worker; a duplicate report finds the task already
  ``COMPLETED`` and is rejected. State transitions are the single source of
  truth.
* **Retries / incidents** -- a failure decrements the remaining retries and
  re-queues the task with a back-off; once retries are exhausted the task
  becomes an ``INCIDENT`` (dead-letter) for an operator to resolve.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping

from procworks import execution as exe
from procworks import unit_of_work
from procworks.dal import DataAccessError, DataAccessLayer, RowCountError
from procworks.mail_runtime import completion_baseline
from procworks.model import (
    MAX_REQUEST_TIMEOUT_MS,
    MAX_RETRY_BACKOFF_MS,
    MAX_RETRY_MAX,
    OPEN_EXTERNAL_TASK_STATES,
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
from procworks.unit_of_work import ConcurrentModification
from procworks.validator import completion_value_findings, node_name

logger = logging.getLogger("procworks.integration")

#: Base back-off (ms) for a task whose binding is gone (the instance or its
#: step no longer exists); otherwise the binding's ``retry_backoff_ms`` is the
#: base. Same value as the model default.
_BACKOFF_BASE_MS = 2000
#: Upper bound (ms) for the doubling, so the wait never grows unbounded. A
#: modelled base above it is honoured as it is (never cut down).
_BACKOFF_CAP_MS = 60_000
#: Retry budget assumed when a task's binding is gone; otherwise the binding's
#: ``retry_max``. Same value as the model default.
_RETRY_BUDGET = 5

#: Task states that still occupy the step (so no duplicate task is
#: materialised). The stores enforce it: a step holds at most one task in
#: these states.
_OPEN_STATES = OPEN_EXTERNAL_TASK_STATES

#: Node states in which a step still waits for its task (the engine completes
#: a step only from these).
_ACTIVE_NODE_STATES = frozenset({NodeState.ACTIVATED, NodeState.RUNNING})

#: Seconds a push delivery may take beyond the step's request timeout before
#: its claim counts as abandoned (the pushing process died between claim and
#: delivery) and a later drive pushes again. Generous on purpose: a second
#: push of a slow but living delivery is the very thing the claim prevents.
_PUSH_CLAIM_MARGIN_S = 60.0
#: The delivery timeout assumed for a step without one (the dispatcher's
#: default, see ``OutboxDispatcher``).
_PUSH_DEFAULT_TIMEOUT_S = 5.0


class ExternalTaskError(Exception):
    """A boundary error in the external-task runtime, carrying an HTTP status.

    ``status`` lets the API map the failure precisely: ``404`` for an unknown
    task, ``409`` for a lock/state conflict (e.g. a stale or duplicate report),
    ``422`` for an invalid output payload, ``502`` for a failed external
    read/write. ``code`` (optional) is a stable, language-neutral reason the
    API passes on next to ``message`` -- e.g. ``DAL.no-row`` when a write-back
    matched no row, or the engine's ``EX.*`` code when the engine refused the
    completion -- so a worker can react to the cause, not to the text.
    ``params`` carries the values the catalogued text names (e.g. ``step``).
    """

    def __init__(
        self,
        message: str,
        status: int = 409,
        *,
        code: str | None = None,
        params: dict[str, str] | None = None,
    ) -> None:
        """Create the error.

        :param message: technical description, returned as ``message``.
        :param status: the HTTP status the API answers with.
        :param code: optional catalogued reason code, returned as ``code``.
        :param params: optional values for the catalogued text (step names,
            never secrets), returned as ``params``; only meaningful together
            with ``code``. ``None`` means the answer carries no ``params``.
        """

        self.message = message
        self.status = status
        self.code = code
        self.params = params
        super().__init__(message)


class UndeliverablePush(Exception):
    """A push sink's verdict that this push can **never** be delivered.

    Raised by an ``on_push`` sink when the configured target itself is
    unusable -- not a passing outage, but e.g. a URL with port ``0`` or a
    scheme other than ``http``/``https``. Retrying cannot help, so
    :meth:`ExternalTaskRuntime.drive_push` turns the task into an incident at
    once instead of leaving it pending without any signal. ``message`` must not
    carry the URL (it may hold a token in its path); name the endpoint
    reference and the reason code instead.
    """

    def __init__(self, message: str) -> None:
        """Create the verdict.

        :param message: the reason kept on the incident (no URL, no secret).
        """

        self.message = message
        super().__init__(message)


def _backoff_ms(base_ms: int, budget: int, retries_left: int) -> float:
    """The wait (ms) before the next attempt of a task that just failed.

    ``base_ms`` is the modelled back-off (``ServiceBinding.retry_backoff_ms``):
    the wait after the **first** failure. Every further failure of the same
    task doubles it -- the number of failures so far is ``budget -
    retries_left`` -- up to :data:`_BACKOFF_CAP_MS`, or up to ``base_ms`` when
    the modeller chose a longer wait (a modelled value is never cut down).

    Edge cases: ``base_ms`` ``0`` gives ``0`` (fetchable again at once); a
    negative base (only possible from a hand-built model, the API refuses it)
    counts as ``0``; a base above
    :data:`~procworks.model.MAX_RETRY_BACKOFF_MS` (only from a model stored
    before the validator bounded it, e.g. ``10**400``) counts as that bound
    -- the one place a modelled value *is* cut down, because a larger wait
    could not even become a point in time; a worker that raised the
    remaining retries above the budget (``retries`` in its report) counts as
    a first failure.

    :param base_ms: the modelled back-off in milliseconds.
    :param budget: the step's retry budget (``retry_max``).
    :param retries_left: the retries remaining *after* this failure.
    :returns: the wait in milliseconds.
    """

    base = min(max(0, base_ms), MAX_RETRY_BACKOFF_MS)
    failures = max(1, budget - retries_left)
    cap = max(_BACKOFF_CAP_MS, base)
    # Bound the exponent first: a large budget must not build a huge integer.
    return float(min(base * (2 ** min(failures - 1, 32)), cap))


def retry_budget(binding: ServiceBinding) -> int:
    """The retry budget of a step, bounded to ``0 .. MAX_RETRY_MAX``.

    ``retry_max`` is bounded by the validator
    (:data:`~procworks.model.MAX_RETRY_MAX`, ``I1.retries-too-large``). A
    model stored before that bound may still carry ``10**400`` -- a step that
    would retry for ever instead of becoming an incident. Such a value counts
    as the bound; a negative one (only from a hand-built model, the API
    refuses it) as ``0``. Compared on the integer, so nothing overflows.

    :param binding: the step's service binding.
    :returns: the number of retries the step's task gets.
    """

    return min(max(0, binding.retry_max), MAX_RETRY_MAX)


def request_timeout_seconds(binding: ServiceBinding) -> float:
    """The push timeout of a step in seconds, bounded; ``0.0`` = the default.

    ``request_timeout_ms`` is modelled in milliseconds and bounded by the
    validator (:data:`~procworks.model.MAX_REQUEST_TIMEOUT_MS`). A model
    stored before that bound may still carry ``10**12`` -- a request that
    drives the push would wait for days -- or ``10**400``, which cannot even be
    divided into a float. Such a value counts as the bound. The comparison
    happens on the integer, before any division.

    :param binding: the step's service binding.
    :returns: the timeout in seconds, at most 30; ``0.0`` for a value ``<= 0``
        (the caller then uses its default, as before).
    """

    millis = binding.request_timeout_ms
    if millis <= 0:
        return 0.0
    return min(millis, MAX_REQUEST_TIMEOUT_MS) / 1000.0


def _resolve_inputs(
    schema: ProcessSchema,
    instance: ProcessInstance,
    node_id: str,
    binding: ServiceBinding | None,
) -> dict[str, object]:
    """Build the input data package handed to the worker.

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

    Every refusal is a 422 :class:`ExternalTaskError` with a ``code`` and the
    reported variable as ``params.name``: ``ET.output-not-writable`` (no WRITE
    access of the step; ``params.step`` names it), ``ET.output-unknown`` (the
    mapping names a missing element), ``ET.output-type`` (value does not fit;
    ``params.type``) and ``ET.output-not-record`` (a record write that is no
    object). The first offending variable is reported.
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
                f"'{key}' is not a writable output of activity '{node_id}'",
                422,
                code="ET.output-not-writable",
                params={"name": key, "step": node_name(schema, node_id)},
            )
        element = schema.data_elements.get(element_id)
        if element is None:
            raise ExternalTaskError(
                f"unknown data element '{element_id}'",
                422,
                code="ET.output-unknown",
                params={"name": key},
            )
        type_params = {"name": key, "type": element.data_type.value}
        if element.source is DataSourceKind.EXTERNAL:
            if element.write is not None:
                if not value_matches_type(element.data_type, value):
                    raise ExternalTaskError(
                        f"scalar write '{element_id}' is not a "
                        f"{element.data_type.value}",
                        422,
                        code="ET.output-type",
                        params=type_params,
                    )
                scalar_out[element_id] = value
                continue
            if not isinstance(value, Mapping):
                raise ExternalTaskError(
                    f"external write '{element_id}' must be a record (object)",
                    422,
                    code="ET.output-not-record",
                    params={"name": key},
                )
            external_out[element_id] = dict(value)
            continue
        if not value_matches_type(element.data_type, value):
            raise ExternalTaskError(
                f"value for '{element_id}' is not a {element.data_type.value}",
                422,
                code="ET.output-type",
                params=type_params,
            )
        instance_out[element_id] = value
    return instance_out, external_out, scalar_out


def task_not_found(task_id: str) -> ExternalTaskError:
    """The 404 for an unknown external task, the same on every path.

    :param task_id: the id named by the caller.
    :returns: the error (``OP.not-found``, ``params.kind`` ``external_task``,
        ``params.name`` the id) -- to be raised by the caller.
    """

    return ExternalTaskError(
        f"external task '{task_id}' not found",
        404,
        code="OP.not-found",
        params={"kind": "external_task", "name": task_id},
    )


def incident_not_found(incident_id: str) -> ExternalTaskError:
    """The 404 for an unknown -- or, for a limited caller, foreign -- incident.

    One answer for both cases, so a caller cannot tell whether an incident it
    may not see exists.

    :param incident_id: the id named by the caller.
    :returns: the error (``OP.not-found``, ``params.kind`` ``incident``,
        ``params.name`` the id) -- to be raised by the caller.
    """

    return ExternalTaskError(
        f"incident '{incident_id}' not found",
        404,
        code="OP.not-found",
        params={"kind": "incident", "name": incident_id},
    )


class ExternalTaskRuntime:
    """Boundary driver that exposes automatic steps as an external work queue.

    Wires the external-task store to the instance store and the pure engine. The
    ``schema_for`` callable yields the effective (hydrated, possibly ad-hoc)
    schema of an instance, and ``context`` is the engine's execution context so
    a completion can advance composed processes exactly like an interactive one.

    With a ``dal`` (Data Access Layer) the boundary also performs bidirectional
    data exchange: READ accesses on EXTERNAL elements are
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
        on_push: Callable[[ServiceBinding, dict[str, object]], bool | None]
        | None = None,
        on_advance: Callable[
            [ProcessSchema, dict[str, NodeState], ProcessInstance], None
        ]
        | None = None,
        on_complete: Callable[
            [ProcessSchema, ProcessInstance, ProcessInstance, ExternalTask, str | None],
            None,
        ]
        | None = None,
    ) -> None:
        self._tasks = tasks
        self._instances = instances
        self._schema_for = schema_for
        self._context = context
        self._dal = dal
        self._now = now or time.time
        self._on_event = on_event
        self._on_push = on_push
        #: Boundary side effects of an engine advance (activation stamping, mail
        #: notification, push drive, persistence). Fired after a completing task
        #: advances the instance, so a task that becomes ready via the external
        #: path is notified exactly like the human mainline.
        self._on_advance = on_advance
        #: Boundary record of a step completed from outside, called as
        #: ``on_complete(schema, before, after, task, actor)`` right after the
        #: completion is stored and before ``on_advance``: the API writes the
        #: same audit/webhook record as for a human completion (activity
        #: completed, instance completed). ``actor`` is whatever the caller
        #: passed to :meth:`complete` (the reporting identity).
        self._on_complete = on_complete

    def _emit(self, event_type: str, **data: object) -> None:
        """Forward a domain event to the optional sink (event side, E13).

        Best-effort and boundary-only: the sink (the webhook outbox) never feeds
        back into the pure engine, and a sink that is not wired is simply a no-op.
        """

        if self._on_event is not None:
            self._on_event(event_type, dict(data))

    def _emit_task(self, event_type: str, task: ExternalTask, **extra: object) -> None:
        """Emit a ``task.*`` event carrying the task's identifying fields.

        Every task event names the task, its instance, its step and its topic
        (empty for a push task); ``extra`` adds event-specific fields such as
        the incident of ``task.incident``.
        """

        self._emit(
            event_type,
            task_id=task.id,
            instance_id=task.instance_id,
            node_id=task.node_id,
            topic=task.topic,
            **extra,
        )

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

        A throw-away **test instance** gets no task at all: it shares topics
        and push targets with the real process, so a task would let a real
        worker (or tool) act on it and announce ``task.*`` to subscribers. Its
        automatic steps are completed by hand in the test view instead.

        **Concurrent callers create one task per step.** The check "no open
        task yet" below only saves work; it reads a state that a parallel
        call may change at once. The store decides: :meth:`ExternalTaskStore.add`
        refuses a second open task of the same step (in the database a
        unique index, waiting for the other transaction), and only the
        caller whose task was stored announces ``task.ready``.

        **Collective read.** It runs after every start and completion, so it
        must not grow with the stock by a store access per case: the running
        cases come from one store read, and the schema of cases without an
        ad-hoc variant is resolved once per ``schema_id`` (the effective
        schema of such a case depends on nothing else). Before, every start
        loaded every running case and its schema separately.

        :param kind: ``EXTERNAL_TASK`` (pull) or ``HTTP_PUSH`` (push).
        :returns: the tasks this call created (not those another caller
            created in the meantime).
        """

        open_by_step: set[tuple[str, str]] = {
            (t.instance_id, t.node_id)
            for t in self._tasks.list_tasks()
            if t.state in _OPEN_STATES
        }
        created: list[ExternalTask] = []
        base_schemas: dict[str, ProcessSchema] = {}
        for instance in self._instances.list_instances(running_only=True):
            if instance.state is not InstanceState.RUNNING or instance.is_test:
                continue
            if instance.ad_hoc_schema is not None:
                schema = self._schema_for(instance)
            else:
                cached = base_schemas.get(instance.schema_id)
                if cached is None:
                    cached = base_schemas[instance.schema_id] = self._schema_for(instance)
                schema = cached
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
                    retries_left=retry_budget(binding),
                    input_variables=_resolve_inputs(schema, instance, node_id, binding),
                    priority=self._priority_of(schema, node_id),
                )
                open_by_step.add((instance.id, node_id))
                if not self._tasks.add(task):
                    continue  # a parallel call created the step's task first
                created.append(task)
                self._emit_task("task.ready", task)
        return created

    def drive_push(self) -> list[ExternalTask]:
        """Push activated automatic ``HTTP_PUSH`` steps to their tool endpoint.

        The *push* side of the outbound boundary: instead of a
        worker pulling work, ProcWorks proactively calls a server-configured
        endpoint with the step's input package. This is **best-effort and
        idempotent** -- it never advances or mutates the pure engine itself:

        * Materialises a ``CREATED`` push task per newly activated step (the
          idempotency anchor: a step with an open task is not pushed twice --
          the store holds at most one open task per step).
        * For each due ``CREATED`` push task it pre-fetches external READ data,
          then **claims** it -- ``LOCKED`` with a generated callback token and
          a short expiry (:meth:`_push_claim_seconds`), saved only if nobody
          changed the task since it was read. Of two drives running at the
          same time (requests, API processes) only the one whose claim was
          saved delivers; the other skips the task. A claim whose delivery
          was abandoned (the process died) expires and is pushed again.
        * The claimed task goes to the ``on_push`` sink, which delivers it
          once and reports the outcome. On success (``True``, or ``None``
          from a sink that only raises on trouble) the claim loses its expiry:
          the task waits for the tool, which reports via the regular
          ``/v1/external-tasks/{id}/complete`` endpoint (exactly-once). A tool
          that reports before the push call has returned keeps its report.
        * A task whose case has ended or whose step is no longer active is
          not pushed; it becomes ``CANCELLED`` (:meth:`_cancel_stale`).
        * A delivery that **failed** (the sink returns ``False``: the tool
          answered with an error or could not be reached) counts as one failed
          attempt, exactly like a worker's reported failure: the task stays
          ``CREATED`` with a back-off and is pushed again by a later drive; once
          the retry budget (``retry_max``) is used up it becomes an
          ``INCIDENT``. Before, it was locked to a callback that would never
          come -- the case hung without any signal.
        * A push whose configured target can **never** be delivered (the sink
          raises :class:`UndeliverablePush`, e.g. port ``0``) becomes an
          ``INCIDENT`` at once -- retries cannot help, and before it waited
          silently forever. Resolving it re-queues it like any incident.
        * A **pre-fetch** of external data that fails before the delivery
          counts as one failed attempt, as in :meth:`fetch_and_lock` (back-off,
          then an incident); nothing is delivered.
        * A push that cannot even be handed over for another reason (the sink
          raises: endpoint unconfigured, egress locked down) leaves the task
          ``CREATED`` without using its budget, to be retried on a later
          drive.
        * The engine and the instance are never touched.

        Returns the tasks that were pushed in this pass. A no-op when no
        ``on_push`` sink is wired.
        """

        if self._on_push is None:
            return []
        self._materialise(AutomationKind.HTTP_PUSH)
        now = self._now()
        pushed: list[ExternalTask] = []
        for task in self._tasks.list_tasks():
            if task.topic or not self._push_due(task, now):
                continue  # not a push task, or not (again) due
            instance = self._instances.get(task.instance_id)
            if instance is None or not self._step_is_open(task, instance):
                self._cancel_stale(task)
                continue
            if instance.is_test:
                continue  # a test instance (stored by an older version)
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
            except ExternalTaskError as err:
                # Same rule as fetch-and-lock: a failing pre-fetch is a failed
                # attempt (back-off, an incident once the budget is used up).
                # Before, the task stayed due without using its budget and was
                # retried on every drive, forever and without a signal.
                logger.warning(
                    "pre-fetch for push task %s failed, counted as a failed attempt: %s",
                    task.id,
                    err.message,
                )
                self._record_failed_push(task, err.message, now)
                continue
            token = f"push_{uuid.uuid4().hex}"
            # Claim the push before delivering it: of two drives that read the
            # same task (two requests, two API processes) only one delivers.
            # The claim carries the callback token already, so a tool that
            # answers before the push call returns finds its task locked.
            task.state = ExternalTaskState.LOCKED
            task.worker_id = token
            task.lock_expires_at = now + self._push_claim_seconds(binding)
            task.available_at = None
            if not self._tasks.put_if_unchanged(task):
                continue  # another drive claimed it first
            payload: dict[str, object] = {
                "task_id": task.id,
                "instance_id": task.instance_id,
                "node_id": task.node_id,
                "callback_token": token,
                "variables": dict(task.input_variables),
            }
            try:
                delivered = self._on_push(binding, payload)
            except UndeliverablePush as never:
                self._record_failed_push(task, never.message, now, retries=0)
                continue
            except Exception:  # noqa: BLE001 -- a push failure must never corrupt state
                # Not handed over: back to CREATED without using the budget,
                # retried on a later drive.
                task.state = ExternalTaskState.CREATED
                self._release(task)
                self._tasks.put_if_unchanged(task)
                continue
            if delivered is False:
                self._record_failed_push(
                    task, f"push to '{binding.endpoint_ref}' was not delivered", now
                )
                continue
            # Delivered: the task now waits for the tool, without expiry. If
            # the tool has already reported (or failed) it, its report stands.
            task.lock_expires_at = None
            if self._tasks.put_if_unchanged(task):
                pushed.append(task)
            else:
                pushed.append(self._tasks.get(task.id) or task)
        return pushed

    @staticmethod
    def _push_due(task: ExternalTask, now: float) -> bool:
        """Whether :meth:`drive_push` may push ``task`` (a push task) at ``now``.

        Two cases: ``CREATED`` after its back-off, and a claim whose delivery
        was abandoned -- ``LOCKED`` with an expiry that has passed (the
        pushing process died between claim and delivery). A delivered push
        is ``LOCKED`` **without** expiry and never pushed again.

        :param task: a push task (empty topic).
        :param now: the current time.
        :returns: whether to push it now.
        """

        if task.state is ExternalTaskState.CREATED:
            return task.available_at is None or task.available_at <= now
        return (
            task.state is ExternalTaskState.LOCKED
            and task.lock_expires_at is not None
            and task.lock_expires_at <= now
        )

    @staticmethod
    def _push_claim_seconds(binding: ServiceBinding) -> float:
        """How long a push claim holds before it counts as abandoned.

        :param binding: the step's push binding.
        :returns: the step's request timeout (or the dispatcher's default when
            it has none) plus :data:`_PUSH_CLAIM_MARGIN_S` -- longer than any
            living delivery may take.
        """

        timeout_s = request_timeout_seconds(binding)
        if timeout_s <= 0:
            timeout_s = _PUSH_DEFAULT_TIMEOUT_S
        return timeout_s + _PUSH_CLAIM_MARGIN_S

    def _record_failed_push(
        self,
        task: ExternalTask,
        message: str,
        now: float,
        *,
        retries: int | None = None,
    ) -> None:
        """Count a failed push as a failed attempt, unless the task moved on.

        A tool may report (or fail) its task through the callback while the
        push is still running; the stored task then has moved on, the save
        of the failed attempt is refused, and the tool's report stands. The
        same holds for a failed pre-fetch that a parallel drive already
        counted: the second count is refused, so one failure uses one retry.
        Pushes run outside any unit of work, so the refusal ends here.

        :param task: the claimed push task, or the due one whose pre-fetch
            failed (as read by this drive).
        :param message: the reason, kept on an incident.
        :param now: the current time.
        :param retries: as :meth:`_fail_attempt` (``0``: incident at once).
        """

        try:
            self._fail_attempt(task, message, now, retries=retries)
        except ConcurrentModification:
            logger.info(
                "failed push of task %s not recorded: the task was reported meanwhile",
                task.id,
            )

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

        **One broken task never blocks the queue.** A task whose external data
        cannot be pre-fetched is not handed out: it counts as one failed
        attempt (:meth:`_fail_attempt` -- back-off, an incident once its retry
        budget is used up) and the next candidate takes its place, across all
        requested topics. Before, the first such failure aborted the whole call
        with 502: the healthy tasks stayed out of reach and the ones already
        locked in the call hung until their lock expired.

        :param worker_id: the fetching worker.
        :param topics: the topics it serves.
        :param lock_ms: the lock window in milliseconds.
        :param max_tasks: the most tasks to hand out.
        :param use_priority: serve higher priorities first.
        :returns: the locked tasks (possibly fewer than ``max_tasks``).
        **Each task goes to one worker.** A candidate is locked by a
        conditional save (:meth:`ExternalTaskStore.put_if_unchanged`): when a
        parallel call -- another thread, worker or API process -- locked or
        changed it since this call read the queue, the save is refused and
        this call moves on to the next candidate instead of overwriting the
        other lock. On PostgreSQL the second save waits for the first
        transaction and then finds the task's version moved on. The external
        pre-fetch therefore runs only for tasks this call holds.

        **The inputs are current.** Each handed-out task gets its package
        resolved from the case at that moment (:func:`_resolve_inputs`, then
        the external pre-fetch), as a push does at every delivery. A data
        correction after the task was created -- the usual way out of a
        failed attempt -- reaches the worker with the next fetch; before, the
        task kept the old key next to external data read for the new one. A
        task already locked is not handed out again, so the package a worker
        holds never changes under it.

        **Only active steps are served.** A candidate whose case is gone or
        no longer running, or whose step is no longer active (finished another
        way, removed), is not handed out; it is closed as ``CANCELLED``
        (:meth:`_cancel_stale`), so it never comes back.

        :raises ExternalTaskError: only when **nothing** could be handed out
            and at least one pre-fetch failed -- the first failure (502, with
            its ``code``, e.g. ``DAL.many-rows``), so a worker still learns why
            it got no work. The failed attempts are recorded either way.
        """

        self.sync()
        now = self._now()
        wanted = set(topics)
        available = [
            task
            for task in self._tasks.list_tasks()
            if task.topic in wanted and self._is_claimable(task, now)
        ]

        if use_priority:
            available.sort(key=lambda t: PRIORITY_RANK[t.priority], reverse=True)

        locked: list[ExternalTask] = []
        first_failure: ExternalTaskError | None = None
        for task in available:
            if len(locked) >= max(0, max_tasks):
                break
            instance = self._instances.get(task.instance_id)
            if instance is None or not self._step_is_open(task, instance):
                self._cancel_stale(task)
                continue
            if instance.is_test:
                continue  # stored by an older version; never reaches a worker
            # The package is resolved now, from the case as it is -- like a
            # push at each delivery. A value corrected since the task was
            # created (typically after a failed attempt) reaches the worker,
            # and the pre-fetch below reads external data for the same keys.
            schema = self._schema_for(instance)
            task.input_variables = _resolve_inputs(
                schema, instance, task.node_id, schema.service_bindings.get(task.node_id)
            )
            task.state = ExternalTaskState.LOCKED
            task.worker_id = worker_id
            task.lock_expires_at = now + lock_ms / 1000.0
            task.available_at = None
            if not self._tasks.put_if_unchanged(task):
                continue  # a parallel call got it first
            try:
                self._prefetch_external(task)
            except ExternalTaskError as err:
                logger.warning(
                    "pre-fetch for external task %s failed, counted as a failed "
                    "attempt: %s",
                    task.id,
                    err.message,
                )
                self._fail_attempt(task, err.message, now)
                first_failure = first_failure or err
                continue
            if self._dal is not None:
                self._tasks.put(task)  # with the pre-fetched data (own version)
            locked.append(task)
        if not locked and first_failure is not None:
            raise first_failure
        return locked

    @staticmethod
    def _is_claimable(task: ExternalTask, now: float) -> bool:
        """Whether ``fetch_and_lock`` may hand ``task`` to a worker at ``now``.

        Two cases: a ``CREATED`` task whose back-off (``available_at``) is over,
        and a ``LOCKED`` task whose lock has expired -- the worker vanished, so
        the task is reclaimed. A lock without expiry (a pushed task) is never
        reclaimed; every other state is not in the queue.
        """

        if task.state is ExternalTaskState.CREATED:
            return task.available_at is None or task.available_at <= now
        return (
            task.state is ExternalTaskState.LOCKED
            and task.lock_expires_at is not None
            and task.lock_expires_at <= now
        )

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
                # A unique key with several rows says so (DAL.many-rows), like
                # the write-back on that key; other failures keep their text.
                raise ExternalTaskError(
                    f"pre-fetch of '{access.element_id}' failed: {err}",
                    502,
                    code=err.code if isinstance(err, RowCountError) else None,
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
        self._release(task)
        return self._tasks.put(task)

    # -- completion / failure ---------------------------------------------

    def complete(
        self,
        task_id: str,
        worker_id: str,
        variables: dict[str, object],
        *,
        actor: str | None = None,
    ) -> ExternalTask:
        """Apply the worker's outputs and advance the instance (exactly-once).

        Validates the lock, maps the reported variables to data-element writes,
        then calls the pure engine's ``complete_activity``. A duplicate report
        is rejected because the task is no longer ``LOCKED``.

        After the completion is stored the boundary observers run, each
        best-effort (a failure is logged, never undoes the completion):
        ``on_complete`` records the step like a human completion, then
        ``on_advance`` handles what became ready.

        :param task_id: the external task being reported.
        :param worker_id: the reporting worker (for a push task: the callback
            token); must hold the lock.
        :param variables: the reported outputs, validated against the step.
        :param actor: the identity that sent the report (login or token
            subject), handed through to ``on_complete`` for the audit;
            ``None`` when the caller has none to name.
        :returns: the stored, ``COMPLETED`` task.
        :raises ExternalTaskError: 404 unknown task, 409 lock/state conflict or
            engine refusal, 422 invalid outputs (including a missing mandatory
            output, ``D1.write-missing``, or a value outside the step's mask,
            ``U4.*``), 502 failed external write --
            in every such case nothing is recorded and the task stays as it was.
        """

        now = self._now()
        task = self._require_locked(task_id, worker_id, now)
        instance = self._instances.get(task.instance_id)
        if instance is None:
            raise ExternalTaskError(
                f"instance '{task.instance_id}' of task '{task_id}' is gone",
                409,
                code="ET.instance-gone",
                params={},
            )
        schema = self._schema_for(instance)
        before_states = dict(instance.node_states)
        binding = schema.service_bindings.get(task.node_id)
        instance_out, external_out, scalar_out = _resolve_outputs(
            schema, task.node_id, binding, variables
        )
        # Same value rules as a human completion: every mandatory write of the
        # step must be supplied (reported now or stored; once a loop repeats
        # the step not by a value of an earlier round), and
        # the step's mask checks hold. The first finding answers, with its code.
        value_findings = completion_value_findings(
            schema, task.node_id, instance, instance_out
        )
        if value_findings:
            first = value_findings[0]
            raise ExternalTaskError(
                first.message, 422, code=first.code, params=dict(first.params)
            )
        if (external_out or scalar_out) and self._dal is None:
            raise ExternalTaskError(
                "external write requires a configured connector",
                422,
                code="ET.no-connector",
                params={},
            )
        # Post-flush external writes *before* advancing the engine: a connector
        # failure then leaves the task LOCKED and the instance untouched, so the
        # worker can retry without the step having moved on.
        if external_out or scalar_out:
            # The external system is written now and keeps the write even if
            # this request is rolled back: after a conflict the request is
            # answered (409 DB.conflict) instead of repeated -- the worker
            # decides about its retry, as for any refused report.
            unit_of_work.mark_irreversible()
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
                # Exactly one row or an error: a key without a row must not
                # pass as success (the value would be stored nowhere).
                self._dal.write_scalar(
                    schema, instance.data_values, element_id, scalar
                )
            except DataAccessError as err:
                raise ExternalTaskError(
                    f"post-flush of '{element_id}' failed: {err}",
                    502,
                    code=err.code if isinstance(err, RowCountError) else None,
                ) from err
        try:
            advanced = exe.complete_activity(
                instance, schema, task.node_id, instance_out, context=self._context
            )
        except exe.ExecutionError as err:
            # Hand the engine's reason on: the worker (and the web client)
            # get the same code and step name as on the human mainline.
            raise ExternalTaskError(
                err.message, 409, code=err.code, params=dict(err.params)
            ) from err
        self._instances.put(advanced)
        task.state = ExternalTaskState.COMPLETED
        task.lock_expires_at = None
        task.available_at = None
        task.instance_revision_guard += 1
        stored = self._tasks.put(task)
        self._emit_task("task.completed", task)
        if self._on_complete is not None:
            # Audit + instance.completed, exactly like the human mainline. Runs
            # before on_advance so the case's own record precedes what its end
            # triggers in a parent. Never undoes the (exactly-once) completion.
            try:
                self._on_complete(schema, instance, advanced, task, actor)
            except Exception:  # noqa: BLE001 -- observer must not break completion
                logger.exception(
                    "post-completion record hook failed for task %s", task.id
                )
        if self._on_advance is not None:
            # Notify/stamp/push for any task the completion just made ready. This
            # is a best-effort boundary observer; a failure here must never undo
            # the (exactly-once) completion, so it is swallowed (stability > mail).
            try:
                # Baseline with the completed step as COMPLETED: a loop that
                # re-offers it in the same advance counts as a fresh activation.
                self._on_advance(
                    schema, completion_baseline(before_states, task.node_id), advanced
                )
            except Exception:  # noqa: BLE001 -- observer must not break completion
                logger.exception(
                    "post-completion advance hook failed for task %s", task.id
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

        ``retries`` overrides the remaining retry count (absolute value); when
        omitted the count is decremented by one. With retries remaining the task
        is re-queued and becomes fetchable again after a back-off (or after the
        explicit ``retry_timeout_ms``). With none remaining it becomes an
        ``INCIDENT`` for an operator to resolve.
        """

        now = self._now()
        task = self._require_locked(task_id, worker_id, now)
        return self._fail_attempt(
            task,
            error_message,
            now,
            retries=retries,
            retry_timeout_ms=retry_timeout_ms,
        )

    def _fail_attempt(
        self,
        task: ExternalTask,
        error_message: str,
        now: float,
        *,
        retries: int | None = None,
        retry_timeout_ms: int | None = None,
    ) -> ExternalTask:
        """Count one failed attempt: re-queue with back-off, or raise an incident.

        Shared by a worker's reported :meth:`failure` and a push whose delivery
        failed (:meth:`drive_push`), so both follow the same rules.

        :param task: the task whose attempt failed (any owner is released).
        :param error_message: the reason, kept on the incident.
        :param now: the current time.
        :param retries: absolute remaining retries; ``None`` decrements by one.
        :param retry_timeout_ms: explicit back-off; ``None`` uses the step's
            modelled back-off, doubled per failure (:func:`_backoff_ms`).
        :returns: the stored task -- ``CREATED`` with ``available_at`` in the
            future while retries remain, else ``INCIDENT`` (with a stored
            :class:`Incident` and a ``task.incident`` event).
        """

        remaining = retries if retries is not None else task.retries_left - 1
        remaining = max(0, remaining)
        task.worker_id = None
        task.lock_expires_at = None
        if remaining > 0:
            task.retries_left = remaining
            task.state = ExternalTaskState.CREATED
            if retry_timeout_ms is not None:
                backoff = float(retry_timeout_ms)
            else:
                binding = self._binding_of(task)
                backoff = _backoff_ms(
                    binding.retry_backoff_ms if binding else _BACKOFF_BASE_MS,
                    retry_budget(binding) if binding else _RETRY_BUDGET,
                    remaining,
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
        self._emit_task(
            "task.incident", task, incident_id=incident.id, message=error_message
        )
        return task

    def bpmn_error(self, task_id: str, worker_id: str, error_code: str) -> ExternalTask:
        """Record a business (BPMN) error reported by the worker -- as an incident.

        The step stays activated in the pure engine (a modelled error path does
        not exist yet). So that the case does not hang without any signal, the
        report raises an :class:`Incident` (message names the reported code)
        and a ``task.incident`` event, exactly like an exhausted technical
        failure; it shows up wherever incidents are shown. The task keeps the
        state ``BPMN_ERROR`` and the code, is not handed out again, and is
        re-queued by :meth:`resolve_incident` (or the step is finished by a
        supervision completion, which closes the task).

        :param task_id: the reported task.
        :param worker_id: the reporting worker; must hold the lock.
        :param error_code: the business error code the worker names.
        :returns: the stored task (``BPMN_ERROR``, released).
        :raises ExternalTaskError: as :meth:`_require_locked`.
        """

        now = self._now()
        task = self._require_locked(task_id, worker_id, now)
        task.state = ExternalTaskState.BPMN_ERROR
        task.error_code = error_code
        self._release(task)
        stored = self._tasks.put(task)
        message = f"worker reported BPMN error '{error_code}'"
        incident = Incident(
            id=f"inc_{uuid.uuid4().hex}",
            external_task_id=task.id,
            instance_id=task.instance_id,
            node_id=task.node_id,
            message=message,
            created_at=now,
        )
        self._tasks.put_incident(incident)
        self._emit_task(
            "task.incident",
            task,
            incident_id=incident.id,
            message=message,
            error_code=error_code,
        )
        return stored

    def close_step(
        self, instance_id: str, node_id: str, *, announce: bool = True
    ) -> list[ExternalTask]:
        """Close every open task of a step that was completed another way.

        Called after a supervision completion finished an automatic step by
        hand. Its task must not stay open: a lock would run out and hand it to
        a second worker, whose report then fails for good -- while the real
        system may do the work twice. Each open task (``CREATED``, ``LOCKED``,
        ``INCIDENT``, ``BPMN_ERROR``) becomes ``COMPLETED`` and is released;
        its unresolved incidents are resolved. A worker that still reports it
        gets 409 ``ET.not-locked`` (state ``COMPLETED``), and no fetch hands
        it out again.

        Must run **before** the boundary advance materialises new tasks: a
        loop that re-activates the step at once gets a fresh task for the new
        round, which this call must not touch.

        :param instance_id: the instance whose step was completed.
        :param node_id: the completed step.
        :param announce: emit ``task.completed`` with ``closed_by:
            "supervision"`` per closed task (``False`` for a throw-away test
            instance, which announces nothing).
        :returns: the closed tasks (empty when the step had none).
        """

        closed: list[ExternalTask] = []
        for task in self._tasks.list_tasks():
            if (
                task.instance_id != instance_id
                or task.node_id != node_id
                or task.state not in _OPEN_STATES
            ):
                continue
            task.state = ExternalTaskState.COMPLETED
            self._release(task)
            task.instance_revision_guard += 1
            self._tasks.put(task)
            for incident in self._tasks.list_incidents():
                if incident.external_task_id == task.id and not incident.resolved:
                    incident.resolved = True
                    self._tasks.put_incident(incident)
            if announce:
                self._emit_task("task.completed", task, closed_by="supervision")
            closed.append(task)
        return closed

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
        """Resolve an incident and re-queue its task for another attempt.

        A task in ``INCIDENT`` (retries exhausted) or ``BPMN_ERROR`` (business
        error reported) goes back to ``CREATED`` with a fresh retry budget and
        without the reported error code; a task in any other state (e.g.
        completed by a supervision completion meanwhile) is left alone, only
        the incident is closed.

        :param incident_id: the incident to resolve.
        :returns: the stored, resolved incident.
        :raises ExternalTaskError: 404 for an unknown incident.
        """

        incident = self._tasks.get_incident(incident_id)
        if incident is None:
            raise incident_not_found(incident_id)
        task = self._tasks.get(incident.external_task_id)
        if task is not None and task.state in (
            ExternalTaskState.INCIDENT,
            ExternalTaskState.BPMN_ERROR,
        ):
            binding = self._binding_of(task)
            budget = retry_budget(binding) if binding is not None else _RETRY_BUDGET
            task.state = ExternalTaskState.CREATED
            task.retries_left = max(1, budget)
            task.error_code = None
            self._release(task)
            self._tasks.put(task)
        incident.resolved = True
        return self._tasks.put_incident(incident)

    # -- internal ----------------------------------------------------------

    @staticmethod
    def _step_is_open(task: ExternalTask, instance: ProcessInstance) -> bool:
        """Whether ``task``'s step still waits for it in ``instance``.

        :param task: any task.
        :param instance: its (existing) case.
        :returns: ``True`` while the case is running and the step is active
            (``ACTIVATED`` or ``RUNNING`` -- the states the engine completes a
            step from); ``False`` when the case has ended or the step was
            finished another way, skipped by a loop or removed.
        """

        return (
            instance.state is InstanceState.RUNNING
            and instance.node_states.get(task.node_id) in _ACTIVE_NODE_STATES
        )

    def _cancel_stale(self, task: ExternalTask) -> None:
        """Close a queued task that is no longer needed (case or step ended).

        Such a task used to stay in the queue for good: workers fetched it
        again and again, and every report failed with 409. It becomes
        ``CANCELLED`` with ``cancel_reason`` ``"step-ended"``, released from
        any worker. Saved conditionally: if a parallel call changed the task
        meanwhile (e.g. a supervision completion closed it), that change
        stands and nothing happens here.

        A stored cancellation is announced as ``task.cancelled`` (task, case,
        step, topic, ``reason`` ``"step-ended"``): a worker that listed the
        task learns that it will not come. Exactly once -- only the call whose
        conditional save went through emits, and a ``CANCELLED`` task is never
        picked up again (also one closed by the database migration of an
        older version, which is therefore never announced afterwards).

        :param task: the task as this call read it.
        """

        task.state = ExternalTaskState.CANCELLED
        task.cancel_reason = "step-ended"
        self._release(task)
        if self._tasks.put_if_unchanged(task):
            logger.info(
                "external task %s cancelled: step %s of instance %s is no longer active",
                task.id,
                task.node_id,
                task.instance_id,
            )
            self._emit_task("task.cancelled", task, reason=task.cancel_reason)

    def _binding_of(self, task: ExternalTask) -> ServiceBinding | None:
        """The service binding of ``task``'s step, as its instance runs it now.

        :param task: any task.
        :returns: the binding from the instance's effective schema, or
            ``None`` when the instance or the step's binding is gone.
        """

        instance = self._instances.get(task.instance_id)
        if instance is None:
            return None
        return self._schema_for(instance).service_bindings.get(task.node_id)

    @staticmethod
    def _release(task: ExternalTask) -> None:
        """Detach ``task`` from any worker: no owner, no lock, no back-off.

        Used where a task leaves a worker's hands without a timed retry
        (unlock, BPMN error, resolved incident); the caller sets the new state
        and stores the task.
        """

        task.worker_id = None
        task.lock_expires_at = None
        task.available_at = None

    def _require_locked(
        self, task_id: str, worker_id: str, now: float
    ) -> ExternalTask:
        """Load a task the reporting worker holds an unexpired lock on.

        The common precondition of complete, failure, BPMN error, extend-lock
        and unlock. Each refusal names its cause as ``code`` (the worker can
        react without parsing text): 404 ``OP.not-found`` (kind
        ``external_task``), 409 ``ET.not-locked`` (``params.state``: the
        current state), ``ET.locked-by-other`` and ``ET.lock-expired``. The
        messages are unchanged.

        :param task_id: the task named in the request path.
        :param worker_id: the reporting worker (callback token for a push task).
        :param now: the current time, compared with the lock expiry.
        :returns: the locked task.
        :raises ExternalTaskError: as listed above.
        """

        task = self._tasks.get(task_id)
        if task is None:
            raise task_not_found(task_id)
        if task.state is not ExternalTaskState.LOCKED:
            raise ExternalTaskError(
                f"task '{task_id}' is not locked (state {task.state.value})",
                409,
                code="ET.not-locked",
                params={"state": task.state.value},
            )
        if task.worker_id != worker_id:
            raise ExternalTaskError(
                f"task '{task_id}' is locked by another worker",
                409,
                code="ET.locked-by-other",
                params={},
            )
        if task.lock_expires_at is not None and task.lock_expires_at <= now:
            raise ExternalTaskError(
                f"lock for task '{task_id}' has expired",
                409,
                code="ET.lock-expired",
                params={},
            )
        return task
