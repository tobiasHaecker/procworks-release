# SPDX-License-Identifier: BUSL-1.1
"""Headless HTTP API (FastAPI) for the procworks kernel.

This is the single entry point to the domain core (Section 5.4, API-first):
the same operations are available to any client -- GUI, CLI, other systems --
and every mutation goes through the validate-before-commit path. The GUI has
no privileged side door.

This module holds the app, its configuration, the shared state, the
request/response models and the shared helpers (authorization, audit,
instance access). The endpoints themselves are declared per area in flat
modules ``procworks.api_<area>`` and included at the end of this module.

Run locally:
    uvicorn procworks.api:app --reload
Interactive docs at /docs (OpenAPI is generated automatically).
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import threading
import unicodedata
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import unquote_to_bytes

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from procworks import (
    __version__,
    adhoc,
    assignment,
    demo,
    demo_o2c,
    escalation_timer,
    ids,
    mail_runtime,
    migration,
    unit_of_work,
    worklist_priority,
)
from procworks import execution as exe
from procworks import idempotency as idempotency_mod
from procworks import operations as ops
from procworks import (
    templates as builtin_templates_mod,
)
from procworks.assignment import OpenTask
from procworks.audit import (
    EventType,
    create_audit_log,
)
from procworks.auth import (
    INTEGRATION,
    AuthError,
    OpenAuthBackend,
    Principal,
    create_auth_backend,
)
from procworks.auth_password import (
    InitialPassword,
    LoginThrottle,
    PasswordAuthBackend,
    PasswordPolicyError,
    UserView,
    new_initial_password,
    user_view,
)
from procworks.connections import build_connection_registry
from procworks.dal import DataAccessError, UnsafeIdentifierError, check_entity_name
from procworks.execution import ExecutionError, record_data_correction
from procworks.idempotency import IdempotencyStore, create_idempotency_store
from procworks.integration_runtime import (
    ExternalTaskError,
    ExternalTaskRuntime,
    UndeliverablePush,
    request_timeout_seconds,
)
from procworks.licensing import (
    LICENSE_PSEUDO_ID,
    LicenseManager,
    SlotSummary,
    create_license_store,
)
from procworks.model import (
    MAX_LOCK_MS,
    MAX_RETRY_BACKOFF_MS,
    MAX_RETRY_MAX,
    PRIORITY_RANK,
    WRITE_MODES,
    AbsenceEntry,
    AccessMode,
    AggregateKind,
    AutomationKind,
    Cardinality,
    ConnectorKind,
    DataType,
    DecisionRecord,
    EscalationKind,
    EscalationPolicy,
    EscalationStage,
    ExecutorKind,
    ExternalTask,
    FilterOperator,
    FollowUpMode,
    FollowUpTrigger,
    ImpactUrgency,
    InstanceState,
    LoopCell,
    MailBinding,
    MailOutboxEntry,
    MailOutboxState,
    NodeDetailState,
    NodeState,
    NodeType,
    OrgModel,
    ProcessInstance,
    ProcessSchema,
    ProcessTemplate,
    ServiceBinding,
    StaffRule,
    StaffRuleKind,
    TemplateOrigin,
    TemplateParameter,
    TimeConstraint,
    ValueClass,
    WidgetKind,
    WorkItemPriority,
    value_matches_type,
    xor_condition_text,
)
from procworks.outbox import (
    OutboxDispatcher,
    WebhookError,
    build_push_endpoint_registry,
    resolve_target,
)
from procworks.store import (
    InMemoryInstanceStore,
    create_absence_store,
    create_external_task_store,
    create_id_claim_store,
    create_instance_store,
    create_mail_outbox_store,
    create_org_store,
    create_store,
    create_template_store,
    create_webhook_store,
    dehydrate_org,
    hydrate_org,
    make_org_resolver,
    make_resolver,
    remember_version,
    stored_version,
)
from procworks.unit_of_work import CommitOutcomeUnknown, ConcurrentModification
from procworks.validator import (
    CorrectnessError,
    ValidationFinding,
    _possible_agents,
    element_value_findings,
    findings_added,
    node_name,
    validate,
)
from procworks.worklist_priority import TimeContext


def _env_truthy(name: str) -> bool:
    """Return True when the environment variable ``name`` reads as a yes.

    Accepts the common truthy spellings (``1``/``true``/``yes``/``on``, case-
    insensitive). Anything else -- including an unset or empty variable -- is
    False. Used for the additive boot switches that must default to *off*.
    """
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _demo_mode() -> bool:
    """Return True only for a public throw-away demo (``PROCWORKS_DEMO_MODE``).

    Gate for the demo-login conveniences surfaced on ``/auth/config`` (visible
    demo credentials + auto-login). **Default off**, so a regular deployment
    never exposes the shared demo password. The demo *image* sets this alongside
    ``PROCWORKS_LOAD_DEMO``; seeding data and advertising its logins stay two
    separate, independently-off decisions.
    """
    return _env_truthy("PROCWORKS_DEMO_MODE")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: optionally seed a demo data set at boot.

    When ``PROCWORKS_LOAD_DEMO`` is truthy, the built-in demo cosmos is loaded
    once into the (still empty) module singletons, so a throw-away cloud demo
    container comes up *ready* -- no manual ``POST /admin/reset`` needed (D0a).
    ``PROCWORKS_LOAD_O2C`` does the same for
    the large Order-to-Cash data set; both are independent and may be combined.
    This is a pure boundary convenience and touches no correctness rule; the
    seeds go through the same ``load_demo``/``load_o2c`` path as the admin reset.

    Before anything else it lifts the id sequences past the persisted ids
    (:func:`_reserve_stored_ids`, so a new id never replaces a stored one) -- that part is not
    optional and runs on every start, with or without a seed switch.

    Idempotent by design: it only seeds when no schema exists yet, so a
    re-entrant lifespan (test client, ``--reload``) or an already-populated
    store is left untouched. Off by default -- without the env var nothing runs.

    Then it starts the **background escalation sweep**
    (:class:`procworks.escalation_timer.EscalationTimer`, interval from
    ``PROCWORKS_ESCALATION_SWEEP_SECONDS``, default 60 s, ``0`` = off): due
    escalation stages fire in its rhythm, no longer on worklist reads. An
    invalid interval aborts the start with the variable's name.

    On shutdown it stops the timer (a running pass is finished first) and
    closes the idle connections of the shared database pools
    (:func:`procworks.db.dispose_shared_engines`).
    """
    # Zuerst die ID-Zaehler hinter den gespeicherten Bestand setzen -- sonst
    # ersetzt der erste neue Vorgang nach einem Neustart ``instance_1``.
    _reserve_stored_ids()
    _warn_duplicate_login_bindings()
    # Die Leer-Pruefung faellt *einmal*, vor dem ersten Seed: sonst saehe der
    # zweite Schalter den vom ersten gefuellten Store und liefe nie an.
    was_empty = not _store.list_ids()
    load_demo = was_empty and _env_truthy("PROCWORKS_LOAD_DEMO")
    load_o2c = was_empty and _env_truthy("PROCWORKS_LOAD_O2C")
    password = _example_password()
    if load_demo:
        _seed_demo(password)
    if load_o2c:
        _seed_o2c(password)
    if (load_demo or load_o2c) and not _demo_mode() and _password_login_active():
        # Wie das Start-Passwort des Admins: einmal ins Log, sonst nirgends.
        logging.getLogger("procworks.api").warning(
            "Example accounts created (password=%r). Change or delete them "
            "before real use.",
            password,
        )
    # Eskalationsabgleich im Hintergrund statt bei jedem Lesen; ein
    # ungueltiges Intervall bricht den Start mit dem Variablennamen ab.
    interval = escalation_timer.sweep_interval()
    timer = (
        escalation_timer.EscalationTimer(interval, _background_escalation_pass)
        if interval > 0
        else None
    )
    if timer is not None:
        timer.start()
    # Abgelaufene Idempotency-Keys regelmaessig entfernen, auch ohne neue
    # Schluessel (vorher nur beim Reservieren eines neuen).
    housekeeping = escalation_timer.EscalationTimer(
        IDEMPOTENCY_HOUSEKEEPING_SECONDS,
        _idempotency_housekeeping_pass,
        name="procworks-idempotency-housekeeping",
    )
    housekeeping.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.stop()
        housekeeping.stop()
    # Beim Herunterfahren die ruhenden Verbindungen des gemeinsamen Pools
    # schliessen, statt sie vom Datenbankserver auslaufen zu lassen. Die
    # Engines bleiben benutzbar (ein erneuter Lifespan, z. B. im Testclient,
    # oeffnet neue Verbindungen). Ohne DATABASE_URL gibt es keine.
    from procworks.db import dispose_shared_engines

    dispose_shared_engines()


app = FastAPI(
    title="Process-Core API",
    version=__version__,
    summary="Headless, block-structured process engine kernel (Correctness by Construction).",
    lifespan=_lifespan,
)


#: HTTP methods whose endpoints change something and therefore run as a unit
#: of work (:class:`TransactionalRoute`).
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Serialises the units of work of this process while the stores are in
#: memory (see :func:`_write_serialization`).
_memory_write_lock = threading.Lock()


def _write_serialization() -> threading.Lock | None:
    """The lock that runs units of work one after another, or ``None``.

    The in-memory stores have no transactions: a unit cannot be rolled back,
    so a repetition after a conflict could repeat writes the first attempt
    already made (an audit event, a spawned case). In-memory there is exactly
    one API process, so its units simply run one after another -- no conflict
    between requests can arise, and every request sees the state the previous
    one left (the version check of the stores still guards every other
    writer). On a database the units run in parallel; the transaction and the
    conditional save keep them apart.

    Decided at call time from the instance store, so tests that move the API
    onto SQL stores get the database behaviour.

    :returns: :data:`_memory_write_lock` for in-memory stores, else ``None``.
    """

    return _memory_write_lock if isinstance(_instances, InMemoryInstanceStore) else None


def manages_own_units[EndpointT: Callable[..., object]](endpoint: EndpointT) -> EndpointT:
    """Mark a mutating endpoint that must **not** run as one unit of work.

    For endpoints whose very job is to talk to the outside (drive the push
    sink, dispatch the mail outbox, check a connector, ping a webhook) or that
    split their work into units of their own (the escalation sweep, one unit
    per case): one transaction around them would hold a database connection
    through every external call, and a repetition would deliver twice. In
    memory a unit holds the write lock of the process
    (:func:`_write_serialization`) -- every other writer, down to a login,
    would wait for the slowest outside system. Their store writes commit one
    by one, as before units existed.

    An endpoint that does slow work first and stores afterwards (the key
    derivation of a login or a new password, the name lookup of a webhook
    target) carries the mark as well and runs only its store part as a unit
    (:func:`_run_as_unit`). That part must re-check what the slow part relied
    on, because other writers may have changed it in between.

    Apply it *below* the route decorator (it must see the plain function)::

        @router.post("/admin/escalations/sweep")
        @api.manages_own_units
        def post_admin_escalation_sweep() -> ...

    Every marked endpoint is listed, with its reason, in the guard
    ``test_every_mutating_endpoint_runs_as_a_unit_of_work``.

    :param endpoint: the endpoint function.
    :returns: the same function, marked.
    """

    endpoint.__dict__["_procworks_own_units"] = True
    return endpoint


def _run_as_unit[T](change: Callable[[], T], *, exclusive: bool = False) -> T:
    """Run ``change`` as one unit of work of a request, as every mutating endpoint does.

    Passes the write gate (:func:`_hold_write_gate`) first, then runs in
    :func:`procworks.unit_of_work.run` with the in-memory serialisation
    (:func:`_write_serialization`) and ``HTTPException`` as a deliberate
    answer (what was stored before it is kept). :func:`_in_unit_of_work`
    wraps whole endpoints with it; an endpoint marked with
    :func:`manages_own_units` calls it for just its store part, after the
    slow work it must not do under the lock.

    Inside a running unit it simply joins that unit (see
    :func:`procworks.unit_of_work.run`).

    :param change: the store part; loads, changes and saves through the stores.
    :param exclusive: pass the gate exclusively (the reset,
        :func:`excludes_all_writers`).
    :returns: what ``change`` returned in the successful attempt.
    :raises ConcurrentModification: the conflict persisted through all
        repetitions.
    """

    def gated() -> T:
        _hold_write_gate(exclusive=exclusive)
        return change()

    return unit_of_work.run(
        gated,
        serialize=_write_serialization(),
        commit_on=(HTTPException,),
    )


def _in_unit_of_work(endpoint: Callable[..., object]) -> Callable[..., object]:
    """Wrap a synchronous endpoint so each call is one unit of work.

    The call runs in :func:`_run_as_unit`: one transaction for everything the
    request stores (case, audit, outbox -- all or nothing), the conditional
    save, and the repetition after a conflict, so the caller gets the answer
    the current state deserves. ``HTTPException`` is a deliberate answer:
    what was stored before it stays (as before units existed); any other
    error rolls back. Deliveries registered with
    :func:`procworks.unit_of_work.after_commit` run after the commit, still
    within the request.

    ``functools.wraps`` keeps the signature FastAPI reads (parameters,
    dependencies, return annotation via ``__wrapped__``).
    """

    exclusive = bool(getattr(endpoint, "_procworks_excludes_writers", False))

    @functools.wraps(endpoint)
    def run_as_unit(*args: object, **kwargs: object) -> object:
        return _run_as_unit(lambda: endpoint(*args, **kwargs), exclusive=exclusive)

    return run_as_unit


def excludes_all_writers[EndpointT: Callable[..., object]](endpoint: EndpointT) -> EndpointT:
    """Mark a mutating endpoint that must run while no other request writes.

    For the reset of all data: it deletes every model, case and event and may
    seed new ones. A request running beside it (a case start) read a model the
    reset then deleted and saved its case afterwards -- a case without a
    model. Marked, the endpoint passes the database's write gate exclusively
    (:func:`_hold_write_gate`): it waits for the requests under way, and
    requests arriving meanwhile wait for it. In memory the units of the one
    process run one after another anyway.

    Apply it *below* the route decorator (it must see the plain function).

    :param endpoint: the endpoint function.
    :returns: the same function, marked.
    """

    endpoint.__dict__["_procworks_excludes_writers"] = True
    return endpoint


def _hold_write_gate(*, exclusive: bool) -> None:
    """Pass the write gate for the running unit (first thing in every request).

    Every mutating request passes it shared, an endpoint marked with
    :func:`excludes_all_writers` exclusively -- see
    :meth:`procworks.db.SqlAlchemyInstanceStore.hold_write_gate`. Taken
    before any other lock of the unit, so it can never close a cycle with
    them. A no-op for stores without a gate (in memory, where units already
    run one after another).

    :param exclusive: the reset's side of the gate.
    """

    hold = getattr(_instances, "hold_write_gate", None)
    if callable(hold):
        hold(exclusive=exclusive)


class TransactionalRoute(APIRoute):
    """Route class of the API: every mutating endpoint runs as a unit of work.

    Set on the application router and on every ``APIRouter`` of the
    ``api_*`` modules (``APIRouter(route_class=api.TransactionalRoute)``,
    guarded by a test). Endpoints for ``POST``/``PUT``/``PATCH``/``DELETE``
    are wrapped by :func:`_in_unit_of_work` -- unless marked with
    :func:`manages_own_units`, or asynchronous (there are none; an
    ``async def`` endpoint would need its own handling).
    """

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        methods = {m.upper() for m in (kwargs.get("methods") or ())}
        if (
            methods & _MUTATING_METHODS
            and not getattr(endpoint, "_procworks_own_units", False)
            and not inspect.iscoroutinefunction(endpoint)
        ):
            endpoint = _in_unit_of_work(endpoint)
        super().__init__(path, endpoint, **kwargs)


app.router.route_class = TransactionalRoute

# The browser-based UI (Section 8) is a thin web client that may be served from
# a different origin (file:// or a static dev server). It holds no correctness
# logic, so a permissive CORS policy is safe for this local kernel: every
# request still passes the same validate-before-commit path. In production,
# ``PROCWORKS_CORS_ORIGINS`` (comma-separated) pins the allowed origins.
def _cors_origins() -> list[str]:
    raw = os.environ.get("PROCWORKS_CORS_ORIGINS", "").strip()
    if not raw:
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


#: A JSON string escape for U+0000 that is not itself escaped: an even number
#: of backslashes (escaped backslashes, possibly none) followed by ``\u0000``.
#: Outside strings JSON has no backslashes, so this finds exactly the NUL
#: characters of a JSON document without parsing it.
_JSON_NUL_ESCAPE = re.compile(rb"(?<!\\)(?:\\\\)*\\u0000")

#: A JSON ``\\uXXXX`` escape that is not itself escaped (same backslash rule
#: as :data:`_JSON_NUL_ESCAPE`); group 1 is the hex code.
_JSON_UNICODE_ESCAPE = re.compile(rb"(?<!\\)(?:\\\\)*\\u([0-9a-fA-F]{4})")


#: The 422 code for half a UTF-16 surrogate pair in a JSON body.
_LONE_SURROGATE = "REQ.lone-surrogate"

#: The 422 code for a JSON body that is not valid UTF-8 (:func:`_is_valid_utf8`).
_INVALID_ENCODING = "REQ.invalid-encoding"


def _is_valid_utf8(body: bytes) -> bool:
    """Whether a request body is valid UTF-8, decoded strictly.

    JSON travels as UTF-8, but the parser behind the routes decodes a byte
    body leniently (``surrogatepass``): the UTF-8 *encoding* of a surrogate
    (``ED A0 80`` for U+D800, or a pair encoded half by half, "CESU-8") comes
    out as a lone surrogate -- a string neither the response encoder nor
    PostgreSQL can write, so the request ended in 500. Python's strict UTF-8
    decoder refuses exactly what UTF-8 does not allow: encoded surrogates,
    overlong forms, code points above U+10FFFF, bytes that never occur
    (``C0``, ``C1``, ``F5``-``FF``), stray continuation bytes and sequences
    cut off anywhere, including at the very end of the body.

    :param body: the raw request body.
    :returns: ``True`` for valid UTF-8 (also the empty body, four-byte
        characters such as emoji and a leading byte order mark).
    """

    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _has_lone_surrogate(body: bytes) -> bool:
    """Whether a JSON text contains a UTF-16 surrogate escape without its partner.

    JSON spells a character outside the Basic Multilingual Plane as a pair of
    escapes, high (``\\ud800``-``\\udbff``) immediately followed by low
    (``\\udc00``-``\\udfff``). Python's parser also accepts either half on
    its own and yields a string no UTF-8 encoder can write -- neither the
    response encoder nor PostgreSQL. Scanning the escapes without parsing the
    document finds exactly these halves.

    :param body: the raw JSON body.
    :returns: ``True`` when some high escape is not directly followed by a low
        one, or some low escape is not directly preceded by a high one.

    Edge cases: an escaped backslash (``\\\\ud800``) is text, not an escape;
    raw (unescaped) surrogates cannot occur in valid UTF-8 -- the middleware
    refuses those bytes before this scan (:func:`_is_valid_utf8`), because
    the JSON parser would let them through.
    """

    pending_high_end: int | None = None
    for match in _JSON_UNICODE_ESCAPE.finditer(body):
        code = int(match.group(1), 16)
        escape_start = match.end() - 6
        if pending_high_end is not None:
            if 0xDC00 <= code <= 0xDFFF and escape_start == pending_high_end:
                pending_high_end = None
                continue
            return True  # the high half before this escape stays alone
        if 0xD800 <= code <= 0xDBFF:
            pending_high_end = match.end()
        elif 0xDC00 <= code <= 0xDFFF:
            return True
    return pending_high_end is not None


def _is_json_body(scope: Scope) -> bool:
    """Whether the request body is parsed as JSON (absent or JSON media type).

    FastAPI reads a body without ``Content-Type`` as JSON as well, so both are
    scanned; any other type (an upload, plain text) passes untouched -- a NUL
    byte may be legitimate there and never reaches a text column as such.
    """

    for name, value in scope.get("headers") or []:
        if name == b"content-type":
            media = bytes(value).split(b";", 1)[0].strip().lower()
            return media == b"application/json" or media.endswith(b"+json")
    return True


class _RejectNulCharacters:
    """ASGI middleware: refuse a request that carries U+0000 (NUL), with 422.

    PostgreSQL cannot store NUL in a text or JSONB value. A name, label or
    data value with NUL therefore failed only when the store wrote it -- as a
    500, after the request had passed every check, and the broken upstream
    connection then cost the *next* request behind the reverse proxy a 502.
    No ProcWorks input needs the character, so it is refused at the one place
    every request passes, before routing and before any store access:

    * in the path (``/schemas/%00``) and the query string (percent-decoded);
    * in a JSON body, as the escape ``\\u0000`` or as a raw byte.

    A JSON body that is not valid UTF-8 is refused the same way
    (``REQ.invalid-encoding``, :func:`_is_valid_utf8`): the parser would turn
    an encoded surrogate into a string nothing can write (500), and any other
    invalid byte into a 400 without a code. A body in UTF-16/32 contains NUL
    bytes and keeps getting ``REQ.nul-character`` (that check runs first).

    The answer is 422 with ``detail.code`` ``REQ.nul-character`` and
    ``params.where`` (``path``, ``query`` or ``body``); nothing is executed, so
    the state stays unchanged. The same holds for half a UTF-16 surrogate pair
    in a JSON body (``REQ.lone-surrogate``, :func:`_has_lone_surrogate`): the
    parser accepts it, but the string cannot be written as UTF-8, so the
    request used to be stored and then answered with 500. Every other character -- umlauts, emoji,
    right-to-left text -- passes unchanged, and so does the *text* ``\\u0000``
    (an escaped backslash followed by ``u0000``). Non-JSON bodies are not
    inspected (:func:`_is_json_body`). JSON bodies are buffered once and
    replayed to the application unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``.

        :param app: the inner ASGI application.
        """

        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check one request; pass it on unchanged or answer 422 directly."""

        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if "\x00" in scope.get("path", ""):
            await self._refuse("path", scope, receive, send)
            return
        if b"\x00" in unquote_to_bytes(scope.get("query_string", b"")):
            await self._refuse("query", scope, receive, send)
            return
        if not _is_json_body(scope):
            await self._app(scope, receive, send)
            return

        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                # Client went away while sending: let the app see it as usual.
                await self._app(scope, _replay([message]), send)
                return
            chunks.append(message.get("body", b""))
            more = bool(message.get("more_body", False))
        body = b"".join(chunks)
        if b"\x00" in body or _JSON_NUL_ESCAPE.search(body):
            await self._refuse("body", scope, receive, send)
            return
        if not _is_valid_utf8(body):
            await self._refuse("body", scope, receive, send, _INVALID_ENCODING)
            return
        if _has_lone_surrogate(body):
            # Boundary code like ``REQ.nul-character``, not a modelling rule.
            await self._refuse("body", scope, receive, send, _LONE_SURROGATE)
            return
        await self._app(
            scope, _replay([{"type": "http.request", "body": body, "more_body": False}], receive),
            send,
        )

    @staticmethod
    async def _refuse(
        where: str,
        scope: Scope,
        receive: Receive,
        send: Send,
        code: str = "REQ.nul-character",
    ) -> None:
        """Answer the request with the 422 described in the class docstring.

        :param where: ``path``, ``query`` or ``body`` (``params.where``).
        :param code: ``REQ.nul-character`` (default), ``REQ.lone-surrogate`` or
            ``REQ.invalid-encoding``; it picks the message as well.
        """

        if code == _INVALID_ENCODING:
            message = f"the request {where} is not valid UTF-8"
        elif code == _LONE_SURROGATE:
            message = (
                f"the request {where} contains a lone UTF-16 surrogate "
                "(\\ud800-\\udfff without its partner)"
            )
        else:
            message = f"the request {where} contains a NUL character (U+0000)"
        response = JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "message": message,
                    "code": code,
                    "params": {"where": where},
                }
            },
        )
        await response(scope, receive, send)


def _replay(messages: list[Message], then: Receive | None = None) -> Receive:
    """A ``receive`` callable that first returns ``messages``, then defers.

    :param messages: the already consumed ASGI messages, returned in order.
    :param then: the original ``receive`` for everything afterwards (e.g. the
        ``http.disconnect`` a streaming response waits for); ``None`` when the
        consumed messages already ended the conversation.
    :returns: the replacement ``receive`` for the inner application.
    """

    pending = list(messages)

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        if then is not None:
            return await then()
        return {"type": "http.disconnect"}

    return receive


def _json_safe(value: object) -> object:
    """Copy ``value`` with every non-finite float spelled as a string.

    Strict JSON has no ``NaN``/``Infinity``, and the response encoder refuses
    them. The API *accepts* these literals in a request (Python's parser does),
    and a validation error echoes the offending input -- so without this the
    422 for ``"lock_ms": NaN`` became a 500 while it was being written.

    :param value: JSON-like data (dicts, lists, scalars), e.g. the
        ``jsonable_encoder`` output of a validation error list.
    :returns: the same structure; ``nan`` becomes ``"NaN"``, ``inf``
        ``"Infinity"``, ``-inf`` ``"-Infinity"`` (the literals the client sent).
        Everything else is returned unchanged.
    """

    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class InstanceNotFound(HTTPException):
    """The 404 for an unknown instance or one a limited caller may not read.

    A plain :class:`HTTPException` everywhere outside ``/v1`` (``detail``
    stays the string the web client and existing callers know). Under
    ``/v1`` -- the integration API, where every refusal names its reason --
    :func:`_instance_not_found` answers with ``{"message", "code",
    "params"}``: ``OP.not-found`` with ``kind`` ``instance`` and the id the
    caller named. Raised only by :func:`_get_instance_or_404` and
    :func:`_readable_instance_or_404`.
    """

    def __init__(self, instance_id: str) -> None:
        """Create the error.

        There is deliberately **one** text for both causes ("no such
        instance" and "not yours to see"): ``instance '<id>' not found``. A
        second text for the foreign case would tell a limited caller which
        ids exist. The id is the one the caller named, so echoing it reveals
        nothing.

        :param instance_id: the id the caller named (echoed in the text and
            as ``params.name``).
        """

        super().__init__(status_code=404, detail=f"instance '{instance_id}' not found")
        self.instance_id = instance_id


@app.exception_handler(InstanceNotFound)
async def _instance_not_found(request: Request, exc: InstanceNotFound) -> JSONResponse:
    """Answer an :class:`InstanceNotFound` -- with a code under ``/v1`` only.

    The path is the one after the ``/api`` prefix was stripped, so
    ``/api/v1/...`` counts as ``/v1`` too.

    :param request: the refused request (its path decides the shape).
    :param exc: the raised error.
    :returns: the 404 response.
    """

    detail: object = exc.detail
    if str(request.scope.get("path", "")).startswith("/v1/"):
        detail = {
            "message": exc.detail,
            "code": "OP.not-found",
            "params": {"kind": "instance", "name": exc.instance_id},
        }
    return JSONResponse(status_code=404, content={"detail": detail}, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's 422 for an invalid request, as valid JSON in every case.

    Same shape as FastAPI's own handler (``{"detail": [<pydantic errors>]}``,
    each with ``type``, ``loc``, ``msg``, ``input``), so no client or test sees
    a difference -- except that a non-finite number in the echoed ``input``
    is spelled as a string (:func:`_json_safe`) instead of breaking the
    response with a 500, and so is a body that is not valid UTF-8: a route
    expecting JSON that gets another content type echoes the raw bytes, which
    are decoded with replacement characters instead of strictly (strictly,
    one invalid byte turned the 422 into a 500).

    :param request: the refused request (unused; required by the signature).
    :param exc: the validation error raised before the endpoint ran.
    :returns: the 422 response.
    """

    return JSONResponse(
        status_code=422,
        content={
            "detail": _json_safe(
                jsonable_encoder(
                    exc.errors(),
                    custom_encoder={bytes: lambda raw: raw.decode("utf-8", "replace")},
                )
            )
        },
    )


#: Seconds a client should wait before retrying after a database outage.
DB_RETRY_AFTER_SECONDS = 5


def _database_unavailable() -> JSONResponse:
    """The 503 for "the database cannot be reached" -- one shape everywhere.

    :returns: ``503`` with ``detail = {message, code: "DB.unavailable",
        params: {}}`` and ``Retry-After``. The message is fixed on purpose: the
        driver's text names host, port and user of the database.
    """

    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "message": "the database is not reachable at the moment; please retry shortly",
                "code": "DB.unavailable",
                "params": {},
            }
        },
        headers={"Retry-After": str(DB_RETRY_AFTER_SECONDS)},
    )


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
@app.exception_handler(PoolTimeoutError)
async def _database_outage(request: Request, exc: Exception) -> JSONResponse:
    """Answer a request that failed because the database is out of reach.

    ``OperationalError`` (connection refused or lost, server shut down, also
    a transaction the server aborted and asks to retry), ``InterfaceError``
    (a connection that was already closed) and a pool checkout timeout mean
    the request did not run to its end -- trying again later is the right
    reaction, so the answer is 503 (:func:`_database_unavailable`) instead of
    500. Before, reads answered 500 and writes broke the upstream connection
    (502 behind the reverse proxy). Other database errors (a constraint
    violation, invalid data) are defects and stay 500.

    :param request: the failed request (method and path go to the log).
    :param exc: the raised error; only its class reaches the log.
    :returns: the 503 response.
    """

    logging.getLogger("procworks.api").warning(
        "database not reachable during %s %s (%s)",
        request.method,
        request.url.path,
        type(getattr(exc, "orig", None) or exc).__name__,
    )
    return _database_unavailable()


@app.exception_handler(CommitOutcomeUnknown)
async def _commit_outcome_unknown(request: Request, exc: CommitOutcomeUnknown) -> JSONResponse:
    """Answer a request whose commit broke off: 503 ``DB.outcome-unknown``.

    The connection was lost while ``COMMIT`` was under way (typically the
    stall watchdog closed it on a frozen database). The server may have
    executed the commit after all, so the outage answer "nothing was done,
    please retry" would be wrong: a client that followed it created the
    object twice. This answer therefore carries **no** ``Retry-After`` and
    asks to check the result first -- a repetition with the same
    ``Idempotency-Key`` is safe, because key and answer are committed
    together with the change. Breaks before the commit stay ``503
    DB.unavailable`` (:func:`_database_outage`).

    :param request: the failed request (method and path go to the log).
    :param exc: the raised error; the driver error is its ``__cause__``.
    :returns: ``503`` with ``detail = {message, code: "DB.outcome-unknown",
        params: {}}``.
    """

    logging.getLogger("procworks.api").error(
        "database connection lost during the commit of %s %s (%s); the change "
        "may or may not be saved",
        request.method,
        request.url.path,
        type(exc.__cause__).__name__ if exc.__cause__ is not None else "unknown",
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "message": (
                    "the database connection broke off while the change was being "
                    "saved; it may or may not have been saved -- check the result "
                    "before repeating it (the same Idempotency-Key makes a "
                    "repetition safe)"
                ),
                "code": "DB.outcome-unknown",
                "params": {},
            }
        },
    )


@app.exception_handler(SQLAlchemyError)
async def _database_error(request: Request, exc: Exception) -> JSONResponse:
    """Answer an unexpected database error: 500 with the code ``DB.unexpected``.

    A database error that is no outage (those are 503, :func:`_database_outage`
    -- the more specific handler wins) is a defect: a violated constraint, a
    trigger, data the database refuses. The request is rolled back as a whole
    (its unit of work commits only without an error), so nothing of it was
    saved. Before, the answer was a bare ``500 Internal Server Error``
    without a code: a client could neither tell it from other failures nor
    word it. The message is fixed -- the driver's text names tables,
    constraints and values -- and the log gets the error class and the full
    traceback for the operator.

    :param request: the failed request (method and path go to the log).
    :param exc: the database error.
    :returns: ``500`` with ``detail = {message, code: "DB.unexpected",
        params: {}}``.
    """

    logging.getLogger("procworks.api").error(
        "unexpected database error during %s %s (%s)",
        request.method,
        request.url.path,
        type(getattr(exc, "orig", None) or exc).__name__,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "message": "unexpected database error; nothing of the request was saved",
                "code": "DB.unexpected",
                "params": {},
            }
        },
    )


@app.exception_handler(ConcurrentModification)
async def _concurrent_modification(request: Request, exc: ConcurrentModification) -> JSONResponse:
    """Answer a change that kept colliding with other changes: 409 ``DB.conflict``.

    A conflicting save is normally invisible: the request is repeated on the
    new state (:func:`procworks.unit_of_work.run`) and answers from there.
    This answer comes only when the conflict persisted through every
    repetition, or when the request had already done something outside that
    must not happen twice (an external write) -- in both cases nothing of
    the request was stored. The caller reloads and decides again; an
    integration retries the request.

    :param request: the failed request (method and path go to the log).
    :param exc: the conflict, naming what collided.
    :returns: ``409`` with ``detail = {message, code: "DB.conflict",
        params: {kind, id}}`` (``kind`` is ``instance``, ``schema``,
        ``external_task`` or ``org``).
    """

    logging.getLogger("procworks.api").info(
        "concurrent change of %s %s during %s %s",
        exc.kind,
        exc.key,
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "message": str(exc),
                "code": "DB.conflict",
                "params": {"kind": exc.kind, "id": exc.key},
            }
        },
    )


app.add_middleware(_RejectNulCharacters)
# Added after the NUL check, so CORS is the outer layer and the 422 above still
# carries the CORS headers a cross-origin client needs to read it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

_store = create_store()
_instances = create_instance_store()
_org_store = create_org_store()
_external_tasks = create_external_task_store()
_connections = build_connection_registry()
_outbox = OutboxDispatcher(create_webhook_store())
_push_endpoints = build_push_endpoint_registry()
#: Process-wide mail sender for modelled e-mail notifications (rule group N).
#: An SMTP sender when configured via the environment, else a no-op that keeps
#: the feature fully modellable/validated without a mail server.
_mail_sender = mail_runtime.create_mail_sender()
#: Durable, retrying transactional outbox for those notifications: a task-ready
#: mail is queued (surviving a crash) and delivered with a back-off/dead-letter.
#: The store is kept alongside so the admin view can read it and ``/admin/reset``
#: can wipe it.
_mail_outbox_store = create_mail_outbox_store()
_mail_outbox = mail_runtime.MailOutboxDispatcher(_mail_outbox_store)
#: Operational store of recorded agent absences (deputy substitution windows).
#: Read at the boundary to resolve who is absent *now*; that set gates deputy
#: substitution in worklists, task completion and mail. Cleared by ``/admin/reset``.
_absence_store = create_absence_store()
#: Store of *user-created* process templates. Built-in templates are provided by
#: :mod:`procworks.templates` (always available, never persisted); this store
#: holds only the templates a modeller saves. Cleared by ``/admin/reset``.
_template_store = create_template_store()
_resolver = make_resolver(_store)
_org_resolver = make_org_resolver(_org_store)
# ``on_spawn`` is resolved at call time (lambda), so the audit of engine-started
# instances follows a replaced ``_audit``/``_instances`` exactly like every
# other boundary helper does.
_context = exe.ExecutionContext(
    _resolver,
    _instances,
    on_spawn=lambda spawned, origin: _record_spawned_instance(spawned, origin),
)
_audit = create_audit_log()

#: Atomare Reservierung neuer Kennungen, geteilt von allen API-Prozessen an
#: derselben Datenbank (Tabelle ``id_claim``). ``/admin/reset`` laesst sie
#: bewusst stehen: Ein anderer Prozess kann eine reservierte Kennung noch
#: speichern wollen.
_id_claims = create_id_claim_store()

# Kein neu vergebener Schluessel darf einen gespeicherten ersetzen: Die
# Stores speichern per Upsert, eine doppelte ID ueberschriebe also still einen
# Vorgang, ein Schema, eine Vorlage oder ein Organisationsmodell. Die Waechter
# fragen vor jeder Vergabe den Store; ``_reserve_stored_ids`` hebt die Zaehler
# zusaetzlich beim Start ueber den Bestand (siehe procworks/ids.py). Die
# Reservierung (``claim``) schliesst den Fall mehrerer API-Prozesse: Zwei
# Prozesse koennen dieselbe Nummer im selben Augenblick im Store frei finden,
# reservieren kann sie aber nur einer -- der andere nimmt die naechste.
ids.INSTANCE_IDS.guard(
    "instance", lambda key: _instances.get(key) is not None, _id_claims.claim
)
ids.MODEL_IDS.guard("schema", lambda key: _store.get(key) is not None, _id_claims.claim)
ids.MODEL_IDS.guard("tpl", lambda key: _template_store.get(key) is not None, _id_claims.claim)
ids.ORG_IDS.guard("org", lambda key: _org_store.get(key) is not None, _id_claims.claim)


def _reserve_stored_ids() -> int:
    """Lift all id sequences past the ids already persisted.

    Called once at start-up (:func:`_lifespan`). Without it a restarted API
    process counted from 1 again; with the guards alone it would still work,
    but would probe the store once per already-used number. Scans every stored
    schema, org model and user template (all ids inside them count, e.g. node
    and element ids) plus the instance ids -- instances themselves carry no
    generated ids of their own besides ``instance_<n>`` and ``adhoc_<n>``, the
    latter numbered per instance -- and every id reserved in ``id_claim``.

    :returns: the highest number found (0 for an empty system), for logging.
    """

    documents: list[object] = []
    documents.extend(_store.get(key) for key in _store.list_ids())
    documents.extend(_org_store.get(key) for key in _org_store.list_ids())
    documents.extend(_template_store.get(key) for key in _template_store.list_ids())
    # Reserved ids count as used even if their object was never saved
    # (failed validation, aborted request) -- otherwise a restarted process
    # would step through them one failed reservation at a time.
    used = [*_instances.list_ids(), *_id_claims.list_ids()]
    return ids.reserve_existing_ids(instance_ids=used, documents=documents)

#: Licensing / agent metering (dormant by default). Without a configured
#: ``PROCWORKS_LICENSE_PUBKEY`` the manager is *not* enforced: every guard is a
#: no-op and no bindings are written, so the whole layer is inert until a
#: licensor key is set. Activation later is only setting that env var. The
#: offline time ratchet is fed by the append-only, hash-chained audit log's
#: newest timestamp; a trusted anchor is embedded back into that same log.
_license_store = create_license_store()


def _example_password() -> str:
    """Password for the logins a data set seeds.

    Only the public throw-away demo (:func:`_demo_mode`) uses the published
    ``demo-procworks`` -- the website prints it, and ``/auth/config`` offers
    it. Everywhere else a fresh random password: "Beispieldaten laden" on a
    customer installation creates 13 logins -- a modeller among them -- and a
    publicly known password must never open them. The caller reports the random one exactly once
    (reset response, or the server log for the boot seed).
    """

    if _demo_mode():
        return demo.DEMO_PASSWORD
    return secrets.token_urlsafe(9)


def _password_login_active() -> bool:
    """True when logins with passwords exist at all (password auth backend)."""

    return isinstance(_auth_backend, PasswordAuthBackend)


def _warn_duplicate_login_bindings() -> None:
    """Name the persons that more than one login is bound to (once, at start).

    Versions before the unique person index could store a second login for
    a person when two creations ran at the same moment. Such bindings are
    kept (deleting a login is the administrator's decision); this warning
    lists them -- person id and number of logins, no login names -- so they
    can be cleaned up in the user administration. Silent without password
    login or without duplicates; a failing read only logs.
    """

    if not isinstance(_auth_backend, PasswordAuthBackend):
        return
    try:
        duplicates = _auth_backend.store.duplicate_bindings()
    except Exception:  # noqa: BLE001 -- a report must never stop the start
        logging.getLogger("procworks.api").warning(
            "could not check the logins for persons with more than one login",
            exc_info=True,
        )
        return
    if duplicates:
        logging.getLogger("procworks.api").warning(
            "%d person(s) have more than one login: %s. One login per person is the "
            "rule; delete the extra logins in the user administration.",
            len(duplicates),
            ", ".join(f"{agent} ({len(logins)} logins)" for agent, logins in duplicates.items()),
        )


def _seed_demo(password: str) -> None:
    """Load the built-in demo world into the current (empty) stores.

    Shared by ``POST /admin/reset {load_demo:true}`` and the boot seed
    (``PROCWORKS_LOAD_DEMO``, see :func:`_lifespan`), so both paths produce the
    exact same demo cosmos. Password logins are seeded only when password auth
    is active -- the open dev backend already grants every role and needs none.
    Assumes the stores were cleared beforehand (``demo.load_demo`` expects an
    empty system); callers guard that (reset wipes first, the boot seed only
    runs on an empty store).
    """
    backend = _auth_backend if isinstance(_auth_backend, PasswordAuthBackend) else None
    demo.load_demo(
        schema_store=_store,
        instance_store=_instances,
        org_store=_org_store,
        audit_log=_audit,
        password_backend=backend,
        absence_store=_absence_store,
        password=password,
    )


def _seed_o2c(password: str) -> None:
    """Load the large Order-to-Cash data set into the current stores.

    Shared by ``POST /admin/reset {load_o2c:true}`` and the boot seed
    (``PROCWORKS_LOAD_O2C``), exactly like :func:`_seed_demo`. The two data sets
    are independent -- own org model, own schema ids, own logins -- so they may
    be loaded separately or together.
    """
    backend = _auth_backend if isinstance(_auth_backend, PasswordAuthBackend) else None
    demo_o2c.load_o2c(
        schema_store=_store,
        instance_store=_instances,
        org_store=_org_store,
        audit_log=_audit,
        password_backend=backend,
        absence_store=_absence_store,
        password=password,
    )


def _write_time_anchor(ts: float, trusted: bool) -> str:
    """Embed a licensing time-ratchet checkpoint into the hash-chained log.

    Returns the new head hash so the anchor can record the chain position that
    witnessed it (tamper evidence). Chained at once (``immediate``): inside a
    unit of work the database log would otherwise chain the event only at the
    commit, and its hash would not be known yet.
    """

    event = _audit.append(
        EventType.TIME_ANCHOR,
        LICENSE_PSEUDO_ID,
        LICENSE_PSEUDO_ID,
        detail={"hwm": f"{ts:.3f}", "trusted": "true" if trusted else "false"},
        immediate=True,
    )
    return event.entry_hash


_license = LicenseManager(
    _license_store,
    pubkey_pem=os.environ.get("PROCWORKS_LICENSE_PUBKEY"),
    grace_days=int(os.environ.get("PROCWORKS_LICENSE_GRACE_DAYS", "0")),
    claim_ttl_seconds=int(os.environ.get("PROCWORKS_LICENSE_CLAIM_TTL", "1800")),
    time_sources=[_audit.max_event_time],
    anchor_writer=_write_time_anchor,
)


def _claim_fetcher(poll_url: str) -> str | None:
    """Fetch an issued license token for one open auto-pull claim (best-effort).

    Contacts the *separate* licensor claim endpoint (never a customer instance)
    and returns the signed token once the paid order has been fulfilled, or
    ``None`` while it is still pending or on any transient failure -- the poller
    simply retries on the next pass. Deliberately tolerant: a malformed response
    or network error must never raise into a process step (stability first). The
    licensor answers ``200`` with JSON ``{"status": "issued", "token": "…"}``
    once ready, and ``202`` (or ``{"status": "pending"}``) while still waiting.
    """

    from urllib import error as urllib_error
    from urllib import request as urllib_request

    req = urllib_request.Request(poll_url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=8) as resp:  # noqa: S310
            if resp.status == 202:
                return None
            body = resp.read()
    except urllib_error.HTTPError:
        return None  # 4xx/5xx from the licensor -> treat as "not yet"
    except Exception:  # noqa: BLE001 - network/DNS/timeout -> retry next pass
        return None
    try:
        import json as _json

        data = _json.loads(body.decode())
    except Exception:  # noqa: BLE001 - non-JSON body -> nothing to activate
        return None
    if isinstance(data, dict) and data.get("status") == "issued":
        token = data.get("token")
        return token if isinstance(token, str) and token else None
    return None


def _all_agent_ids() -> set[str]:
    """The universe of agent ids across all shared org models (metering base)."""

    ids: set[str] = set()
    for org_id in _org_store.list_ids():
        org = _org_store.get(org_id)
        if org is not None:
            ids.update(org.agents)
    return ids


def _required_agent_ids(schema: ProcessSchema) -> set[str]:
    """Design-time over-approximation of agents a schema's staff rules may need.

    Reuses the validator's bounded ``_possible_agents`` (the same over-approx as
    the N3 mail check): an unbounded rule (a runtime-resolved performer) yields
    no concrete ids and is ignored -- the guard only pins agents it can name.
    """

    org = schema.org_model
    if org is None:
        return set()
    required: set[str] = set()
    for rule in schema.staff_rules.values():
        bound = _possible_agents(org, rule)
        if bound:
            required.update(bound)
    return required

# Auth is a coarse boundary layer. The backend is
# swapped via ``PROCWORKS_AUTH``; the default open backend grants every role and
# leaves ``agent_id`` unbound, so existing clients/tests keep working unchanged.
_auth_backend = create_auth_backend()


def get_principal_pending_password_change(request: Request) -> Principal:
    """FastAPI dependency: the verified identity, even with a pending password change.

    Only the endpoints a login needs to *leave* its initial password use this
    dependency directly: ``/auth/me`` (the client learns that a change is due)
    and ``/auth/change-password``. ``/auth/logout`` needs no principal at all.
    Every other endpoint goes through :func:`get_principal`, which refuses such
    a principal -- so the set of admitted endpoints is exactly the set that
    names this dependency, and a new endpoint is locked by default.

    :param request: the incoming request (its ``Authorization`` header).
    :returns: the principal of the active auth backend; ``must_change`` may be set.
    :raises HTTPException: 401 when the header is missing or invalid.
    """

    try:
        return _auth_backend.authenticate(request.headers.get("Authorization"))
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_principal(
    principal: Principal = Depends(get_principal_pending_password_change),
) -> Principal:
    """FastAPI dependency: the verified identity behind the request (401/403).

    A password login that still uses its one-off initial password
    (``principal.must_change``) is refused with 403 and the code
    ``PW.change-required``: whoever knows an initial password must not be able
    to work with the account -- with all its roles -- without ever replacing
    it. The refusal comes before every role or scope gate, because
    :func:`require_role` and :func:`require_scope` build on this dependency.
    Token, JWT and machine principals never carry the flag and pass unchanged.

    :param principal: the authenticated principal (401 already handled).
    :returns: ``principal`` unchanged when no password change is pending.
    :raises HTTPException: 403 ``PW.change-required`` while the change is due.
    """

    if principal.must_change:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "password change required before using this account",
                "code": "PW.change-required",
                "params": {},
            },
        )
    return principal


def require_role(*allowed: str) -> Callable[[Principal], Principal]:
    """Build a dependency that admits only principals holding one of ``allowed``.

    This is the *coarse* gate at the boundary; the fine-grained BZR eligibility
    in the core is unaffected and still decides who may actually work a node.
    """

    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.roles.intersection(allowed):
            raise HTTPException(status_code=403, detail="forbidden")
        return principal

    return _dep


# Reusable role gates. ``viewer`` is the read floor that
# every authenticated role clears; writes need modeler/operator/admin. The
# ``modeler`` is also a runtime actor: they may work tasks and drive execution
# (including testing their own draft schemas), so they share the ``_run`` gate.
_read = Depends(require_role("viewer", "operator", "modeler", "admin"))
_model = Depends(require_role("modeler", "admin"))
_run = Depends(require_role("operator", "modeler", "admin"))
_admin = Depends(require_role("admin"))


def require_scope(scope: str, *human_roles: str) -> Callable[[Principal], Principal]:
    """Build a dependency for a versioned ``/v1`` integration endpoint.

    Two identities may pass, mirroring the two ways the boundary is used:

    * a **human / open** principal that holds one of ``human_roles`` -- this is
      the existing role-based RBAC, so the open dev mode and logged-in users
      reach ``/v1`` exactly as they reach the legacy endpoints; and
    * an **integration service token** (role :data:`INTEGRATION`) that carries
      the required ``scope`` (or the ``"*"`` wildcard) -- a service is confined
      to the scopes its token was minted with (least privilege).

    A service token is therefore *not* admitted by role alone, and a human is
    never asked for scopes; the two paths never weaken one another.
    """

    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.roles.intersection(human_roles):
            return principal
        if INTEGRATION in principal.roles and (
            scope in principal.scopes or "*" in principal.scopes
        ):
            return principal
        raise HTTPException(status_code=403, detail="forbidden")

    return _dep


#: Reserved ``Idempotency-Key`` values and their answers: in the database
#: with ``DATABASE_URL`` (effective across API processes), else in memory
#: (see :mod:`procworks.idempotency`). Replaced by tests like the stores.
_idempotency: IdempotencyStore = create_idempotency_store()


def _request_fingerprint(request: dict[str, object]) -> str:
    """Stable digest of one request: endpoint, path parameters and body.

    :param request: JSON-serialisable description of the request (the caller
        names the endpoint and passes path parameters and the body as plain
        data, e.g. ``model_dump(mode="json")``).
    :returns: hex SHA-256 over the canonical JSON (sorted keys), so two
        requests that differ only in key order are the same request.
    """

    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotent[T](
    principal: Principal,
    key: str | None,
    request: dict[str, object],
    produce: Callable[[], T],
    *,
    status: int,
    identify: Callable[[T], str],
    reload: Callable[[str], T],
) -> T:
    """Run ``produce`` once per ``(identity, Idempotency-Key)``; replay after.

    The key is bound to the request it was first used with -- endpoint, path
    parameters and body (:func:`_request_fingerprint`). Only the *same* request
    replays. The same key with another schema, other data or on another
    endpoint is refused with **409** ``IDEM.key-reused``: replaying would
    confirm something that never happened (no instance for the second
    schema, data not written).

    **A replay reads anew.** The store keeps only the id of the object the
    first answer showed and its status (``identify``/``status``), never the
    answer itself -- it held the whole case with its data for a day. A
    replay therefore answers with the object's *current* state
    (``reload``), under the same status (the route's own). Was the object
    removed in the meantime, ``reload`` answers as a read would (404).

    :param principal: the caller; keys are per identity.
    :param key: the ``Idempotency-Key`` header, ``None``/empty for none.
    :param request: what identifies the request (endpoint name, path
        parameters, body as JSON data).
    :param produce: performs the operation; called at most once per key.
    :param status: the HTTP status of the route's answer (kept with the
        reference).
    :param identify: the id of the object in ``produce``'s result.
    :param reload: reads that object anew for a replay, with the caller's
        read rights.
    :returns: the operation's result, or the reloaded object for a replay.
    :raises HTTPException: 409 ``IDEM.key-reused`` for a reused key; whatever
        ``produce`` or ``reload`` raises (a failure is not stored, the key
        stays free).

    Two requests with the same new key at the same time run one after the
    other, also in two API processes: the key is reserved
    (:meth:`procworks.idempotency.IdempotencyStore.reserve`) inside the unit
    of work, and the reference is kept only if that unit commits -- a
    rolled-back or repeated attempt never leaves a reference behind for
    something that did not happen. Must run inside the request's unit of
    work (every mutating route is one).
    """

    if not key:
        return produce()
    fingerprint = _request_fingerprint(request)
    reservation = _idempotency.reserve(principal.subject, key, fingerprint)
    unit_of_work.on_finish(reservation.close)
    if reservation.previous is not None:
        if not reservation.previous.same_request:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "Idempotency-Key was already used for a different request "
                        "(other endpoint, parameters or body)"
                    ),
                    "code": "IDEM.key-reused",
                    "params": {},
                },
            )
        return reload(reservation.previous.object_id)
    try:
        result = produce()
    except BaseException:
        reservation.abandon()
        raise
    reservation.remember(identify(result), status)
    return result


def _auth_mode() -> str:
    """Report the active auth backend kind for the client's login UI."""

    if isinstance(_auth_backend, PasswordAuthBackend):
        return "password"
    if isinstance(_auth_backend, OpenAuthBackend):
        return "open"
    from procworks.auth_jwt import JwtAuthBackend

    if isinstance(_auth_backend, JwtAuthBackend):
        return "jwt"
    return "token"


def _password_backend() -> PasswordAuthBackend:
    """Return the active password backend or 404 when password login is off."""

    if not isinstance(_auth_backend, PasswordAuthBackend):
        raise HTTPException(status_code=404, detail="password login is not enabled")
    return _auth_backend


def _find_agent_name(agent_id: str) -> str | None:
    """Best-effort lookup of an agent's display name across all known models.

    Scans the shared org registry and every (hydrated) schema's org model, so a
    user can be provisioned from an existing agent regardless of where that
    agent is modelled.
    """

    for org_id in _org_store.list_ids():
        org = _org_store.get(org_id)
        if org is not None and agent_id in org.agents:
            return org.agents[agent_id].name
    for schema_id in _store.list_ids():
        schema = _get_or_404(schema_id)
        agents = (schema.org_model or OrgModel()).agents
        if agent_id in agents:
            return agents[agent_id].name
    return None



def _may_act_for_others(principal: Principal, instance: ProcessInstance | None) -> bool:
    """May an *unbound* caller name the agent it acts for (delegation)?

    Naming an agent means "this person did it": the core then checks the
    staff rule against that agent and records them as performer. That is a
    legitimate capability of a **machine** identity -- an integration that has
    authenticated its own user and completes the task for them (documented in
    the Integrations-Leitfaden) -- and of the open dev mode, which has no
    identity at all. It is not one of a **personal** login: a modeller or
    administrator without an agent binding could otherwise work any step in
    anybody's name, past the four-eyes principle, with the audit showing the
    named person instead of the login (found 2026-09-24; the web client even
    offered a person picker for it in "Meine Aufgaben").

    Allowed, therefore:

    * the open dev mode (no identity exists);
    * the static-token mode (tokens are the documented integration path);
    * any principal holding the ``integration`` role (e.g. a JWT service
      account);
    * throw-away test instances (no audit, no productive work -- the draft
      test view deliberately switches between the people of a model).

    Everything else -- password logins and personal JWT logins -- acts only as
    itself; without an agent binding the supervision path with a mandatory
    reason remains (:func:`_require_supervision_reason`).
    """

    if _auth_mode() in ("open", "token") or INTEGRATION in principal.roles:
        return True
    return instance is not None and instance.is_test


def _resolve_acting_agent(
    principal: Principal,
    requested: str | None,
    instance: ProcessInstance | None = None,
) -> str | None:
    """Pick the acting agent id, never trusting the request body over identity.

    A *bound* principal acts only as itself: a divergent ``req.agent_id`` is
    rejected (403). An *unbound* principal may name an agent only where
    :func:`_may_act_for_others` allows delegation (403 otherwise); the core BZR
    check then still rejects an ineligible agent with 409. Without a named
    agent the result is ``None`` -- the supervision path for completions.

    ``instance`` is the instance the call acts on; it only matters for the
    test-instance exemption.
    """

    if principal.is_bound:
        if requested is not None and requested != principal.agent_id:
            raise HTTPException(
                status_code=403, detail="cannot act on behalf of another agent"
            )
        return principal.agent_id
    if requested is not None and not _may_act_for_others(principal, instance):
        raise HTTPException(
            status_code=403,
            detail=(
                "Dieser Login ist keinem Bearbeiter zugeordnet und kann nicht im "
                "Namen einer Person handeln. Einen Schritt kann er nur als "
                "Aufsichtseingriff mit Begründung abschließen."
            ),
        )
    return requested


def _delegation_detail(
    principal: Principal, acting: str | None
) -> dict[str, str] | None:
    """Audit detail for an act done *in the name of* an agent by another caller.

    When an unbound machine identity names the agent (see
    :func:`_may_act_for_others`), the event's ``agent_id`` is the person, and
    ``detail.actor`` records who actually sent the request -- so the audit
    shows both and stays complete. ``None`` for a bound caller (it *is* the
    agent), for no named agent, and in the open dev mode (no identity to
    record). ``detail`` is part of the hash chain already, so this is additive.
    """

    if acting is None or principal.is_bound or _auth_mode() == "open":
        return None
    return {"actor": principal.subject}


#: Staff-rule kinds whose ``ref`` names a prior step whose *performer* they use.
_PERFORMER_KINDS = frozenset(
    {StaffRuleKind.NODE_PERFORMING_AGENT, StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR}
)


def _performer_uses(
    rule: StaffRule, node_id: str, *, excluded: bool = False
) -> tuple[bool, bool]:
    """How ``rule`` (at any depth of AND/OR/EXCEPT) uses the performer of ``node_id``.

    A ``NODE_PERFORMING_AGENT``/``NODE_PERFORMING_AGENT_SUPERVISOR`` leaf on
    ``node_id`` either **selects** people (the rule needs the performer to
    find anybody) or sits in the right-hand side of an ``EXCEPT`` and
    **excludes** them -- the four-eyes rule "anyone of the role, but not who
    did step X". An exclusion inside an exclusion selects again.

    :param rule: a staff rule.
    :param node_id: the step whose performer is asked for.
    :param excluded: whether ``rule`` itself sits on an excluding side
        (recursion only).
    :returns: ``(selects, excludes)`` -- whether some leaf on ``node_id``
        selects, whether some leaf excludes; ``(False, False)`` when the rule
        does not refer to that step's performer at all.
    """

    if rule.kind in _PERFORMER_KINDS and rule.ref == node_id:
        return (not excluded, excluded)
    selects = excludes = False
    for index, operand in enumerate(rule.operands):
        flip = rule.kind is StaffRuleKind.EXCEPT and index >= 1
        s, e = _performer_uses(operand, node_id, excluded=excluded != flip)
        selects, excludes = selects or s, excludes or e
    return selects, excludes


def _steps_using_performer_of(
    schema: ProcessSchema, node_id: str
) -> tuple[list[str], list[str]]:
    """The other steps whose staff rule uses the performer of ``node_id``.

    :param schema: the effective schema of the instance (ad-hoc changes
        included).
    :param node_id: the step about to be completed.
    :returns: ``(needing, four_eyes)`` -- names (:func:`node_name`), sorted,
        of the steps whose rule selects by that performer, and of those whose
        rule only excludes that performer (four-eyes rule). A step that does
        both counts as needing only.
    """

    needing: list[str] = []
    four_eyes: list[str] = []
    for other, rule in schema.staff_rules.items():
        if other == node_id:
            continue
        selects, excludes = _performer_uses(rule, node_id)
        if selects:
            needing.append(node_name(schema, other))
        elif excludes:
            four_eyes.append(node_name(schema, other))
    return sorted(needing), sorted(four_eyes)


def _refuse_without_performer(schema: ProcessSchema, node_id: str) -> None:
    """Refuse finishing ``node_id`` without a performer when a later step needs one.

    Shared by both supervision paths -- an interactive step finished by an
    unbound login (:func:`_require_supervision_reason`) and an automatic step
    finished by hand without a person behind the login
    (:func:`_require_automatic_supervision`). Either way nobody is recorded
    as the step's performer afterwards.

    :param schema: the effective schema of the case.
    :param node_id: the step about to be finished.
    :raises HTTPException: 422 ``SUP.performer-needed`` when another step's
        rule selects by that performer (it would find nobody), else 422
        ``SUP.four-eyes`` when another step's rule only excludes that
        performer (the separation of duties could not be shown); ``params``
        ``step`` and ``dependants`` (names, comma-separated).
    """

    dependants, four_eyes = _steps_using_performer_of(schema, node_id)
    if not dependants and four_eyes:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"step '{node_id}' cannot be completed by supervision without an "
                    "agent: a later step must not be done by whoever completes it "
                    "(four-eyes rule), which needs a recorded performer"
                ),
                "code": "SUP.four-eyes",
                "params": {
                    "step": node_name(schema, node_id),
                    "dependants": ", ".join(four_eyes),
                },
            },
        )
    if dependants:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"step '{node_id}' cannot be completed by supervision without an "
                    "agent: later steps determine their performer from whoever "
                    "completes it"
                ),
                "code": "SUP.performer-needed",
                "params": {
                    "step": node_name(schema, node_id),
                    "dependants": ", ".join(dependants),
                },
            },
        )


def _require_supervision_reason(
    principal: Principal,
    acting_agent: str | None,
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    reason: str | None,
) -> str | None:
    """Gate a completion that no agent stands behind (Aufsichtseingriff).

    A login without an agent binding (modeler/admin without a person behind it)
    completes a step **outside** the staff-rule check: the core only verifies
    eligibility when an ``agent_id`` is present. That is legitimate as an
    exceptional *supervision* action, but it must never become a routine work
    path, because it would sidestep two guarantees at once -- the BZR (four-eyes
    principle) and the licensing, which counts *agents* who work.

    Rules (all boundary-only, the core stays untouched):

    * Exempt: throw-away test instances (no audit, no productive work), steps
      without a staff rule (nobody to bypass), an agent-backed caller, and the
      open dev mode (no identity exists, the quickstart keeps working).
    * With **licensing enforced** such a completion is refused (403): otherwise
      an unbound login would be unlimited unlicensed labour.
    * When another step's staff rule uses the **performer** of this one
      ("the person who did step X", "their supervisor"), the supervision is
      refused (422 ``SUP.performer-needed``, ``params.step``/``dependants``):
      a supervision without an agent leaves no performer, and that later
      step would find nobody -- the case could then only go on by another
      supervision. Checked before the reason, so the caller is not asked for
      a reason first. A *bound* supervisor is the performer and passes
      (exempt above).
    * When another step's rule only **excludes** that performer -- the
      four-eyes rule ("anyone of the role except whoever did step X") -- the
      supervision is refused as well (422 ``SUP.four-eyes``, same
      ``params``): without a recorded performer the separation of duties
      cannot be shown, and it is a compliance rule, so no supervision
      overrides it. Its own code, because the later step *would* find people;
      the reason is the rule, not an empty worklist.
    * Otherwise a visible ``reason`` is mandatory (422) -- the visible price
      of the exception.

    Returns the trimmed reason when this *is* a supervision completion, else
    ``None``.
    """

    if (
        acting_agent is not None
        or instance.is_test
        or node_id not in schema.staff_rules
        or _auth_mode() == "open"
    ):
        return None
    if _license.enforced:
        raise HTTPException(
            status_code=403,
            detail=(
                "Abschluss ohne Agentenbindung ist bei aktiver Lizenzierung nicht "
                "zulässig: Der Login muss an einen lizenzierten Bearbeiter gebunden sein."
            ),
        )
    _refuse_without_performer(schema, node_id)
    cleaned = _visible_reason(reason)
    if cleaned is None:
        raise _reason_required("supervision", SUPERVISION_REASON_REQUIRED)
    return cleaned


#: The ``message`` of the 422 when an unbound login completes a staffed step
#: without a reason (``REQ.reason-required``, ``what`` ``supervision``).
SUPERVISION_REASON_REQUIRED = (
    "Aufsichtseingriff: Dieser Login ist keinem Bearbeiter zugeordnet. "
    "Bitte eine Begründung angeben, warum der Schritt an der "
    "Bearbeiterregel vorbei abgeschlossen wird."
)

#: The ``message`` of the 422 when an automatic step is completed by hand
#: without a reason (``REQ.reason-required``, ``what`` ``automatic``). The web
#: client recognises the supervision refusal by the code, not by this text.
AUTOMATIC_SUPERVISION_REQUIRED = (
    "Aufsichtseingriff: Dieser Schritt ist automatisch und wird sonst von einem "
    "angebundenen System erledigt. Bitte eine Begründung angeben, warum er von "
    "Hand abgeschlossen wird."
)


def _is_externally_driven(schema: ProcessSchema, node_id: str) -> bool:
    """Is ``node_id`` an automatic step an outside system works (task or push)?

    :param schema: the effective schema of the instance.
    :param node_id: the step.
    :returns: ``True`` for an automatic binding with ``EXTERNAL_TASK`` or
        ``HTTP_PUSH``; ``False`` otherwise -- including an automatic step
        without automation, which an integration completes through the
        regular completion endpoint (its documented path).
    """

    binding = schema.service_bindings.get(node_id)
    return (
        binding is not None
        and binding.automatic
        and binding.automation in (AutomationKind.EXTERNAL_TASK, AutomationKind.HTTP_PUSH)
    )


def _require_automatic_supervision(
    principal: Principal,
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    reason: str | None,
    acting_agent: str | None = None,
) -> str | None:
    """Gate the completion of an externally driven step by hand.

    Such a step belongs to an outside system (:func:`_is_externally_driven`);
    finishing it by hand is a **supervision act** -- the only way on when the
    system cannot (an incident, a reported business error) -- never routine:

    * only ``modeler``/``admin`` may do it; anybody else gets 409 with the
      engine code ``EX.automatic`` (``params.step``), the same answer as for
      claiming such a step;
    * without a person behind the login (``acting_agent`` ``None``) nobody is
      recorded as the step's performer: when a later step takes its staff
      from that performer, the act is refused like an interactive
      supervision (422 ``SUP.performer-needed``/``SUP.four-eyes``,
      :func:`_refuse_without_performer`), before the reason is asked for;
    * a visible reason is mandatory (422 ``REQ.reason-required`` with ``what``
      ``automatic`` and :data:`AUTOMATIC_SUPERVISION_REQUIRED` as message, so
      the web client asks for it), and it is recorded as
      ``ACTIVITY_SUPERVISED``.

    Exempt: other steps (``None``), a throw-away test instance (its automatic
    steps are simulated by hand in the draft test view) and the open dev mode
    (no identity exists).

    :returns: the trimmed reason when this is such a supervision completion,
        else ``None``.
    :raises HTTPException: 409 or 422 as above; checked before the engine
        runs, so a refusal changes nothing.
    """

    if (
        not _is_externally_driven(schema, node_id)
        or instance.is_test
        or _auth_mode() == "open"
    ):
        return None
    if not principal.roles & {"modeler", "admin"}:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"activity '{node_id}' is automatic and is completed by its "
                    "external system; only a modeller or administrator may "
                    "complete it by hand, with a reason"
                ),
                "code": "EX.automatic",
                "params": {"step": node_name(schema, node_id)},
            },
        )
    if acting_agent is None:
        _refuse_without_performer(schema, node_id)
    cleaned = _visible_reason(reason)
    if cleaned is None:
        raise _reason_required("automatic", AUTOMATIC_SUPERVISION_REQUIRED)
    return cleaned


def _completion_detail(
    before: ProcessInstance,
    node_id: str,
    principal: Principal,
    acting_agent: str | None,
) -> dict[str, str] | None:
    """Detail of an ``ACTIVITY_COMPLETED`` event.

    * ``actor`` -- the login, when no agent stands behind the completion, or
      the sender, when a machine identity completed in an agent's name
      (:func:`_delegation_detail`).
    * ``ready_at`` -- when the step became ready (the activation stamp the
      boundary already keeps for the worklist clock). It lets the KPI report
      measure a step's lead time even when it was completed without being
      claimed -- the audit deliberately has no separate activation event.
    """

    detail: dict[str, str] = {}
    if acting_agent is None:
        detail["actor"] = principal.subject
    else:
        detail.update(_delegation_detail(principal, acting_agent) or {})
    ready = before.node_activated_at.get(node_id)
    if ready is not None:
        detail["ready_at"] = ready.isoformat()
    return detail or None


def _label_of(schema: ProcessSchema, node_id: str) -> str | None:
    """Return the human-readable label of a node, if it exists."""

    node = schema.nodes.get(node_id)
    return node.label if node is not None else None


def _instance_event_payload(instance: ProcessInstance) -> dict[str, object]:
    """Build the webhook payload for an instance lifecycle event."""

    return {
        "instance_id": instance.id,
        "schema_id": instance.schema_id,
        "schema_version": instance.schema_version,
        "state": instance.state.value,
    }


def _starter_detail(principal: Principal) -> dict[str, str] | None:
    """Audit detail naming who started an instance (``detail.actor``).

    The login or token subject is recorded on **every** start path -- bound
    personal login, unbound login (modeller/admin without a person), machine
    token -- so the history never shows "System" for a start a real identity
    made. The bound agent travels separately as the event's ``agent_id``.

    :param principal: the verified identity of the starting request.
    :returns: ``{"actor": subject}``, or ``None`` in the open dev mode, where
        no identity exists (its placeholder subject would only pretend one).
    """

    if _auth_mode() == "open":
        return None
    return {"actor": principal.subject}


def _record_instance_start(
    schema: ProcessSchema, instance: ProcessInstance, principal: Principal
) -> None:
    """Audit and announce a freshly started instance -- shared by every start path.

    Writes ``INSTANCE_CREATED`` with the starter (bound agent as ``agent_id``,
    login/token subject as ``detail.actor``, see :func:`_starter_detail`), emits
    ``instance.started``, records an instance that already finished during the
    start (``INSTANCE_COMPLETED`` + ``instance.completed``), then runs the
    boundary side effects of the first advance (:func:`_after_advance`).

    The starting agent is what makes a limited operator *involved* in a case it
    started (:func:`_involved_instance_ids` looks for its agent in the audit), so
    the legacy endpoint and ``/v1`` must record it identically.

    :param schema: the schema the instance was started from.
    :param instance: the new instance, as returned by the engine.
    :param principal: the identity that started it.
    :returns: nothing; a throw-away test instance is the caller's business
        (it records no audit and is never passed here).
    """

    _audit.append(
        EventType.INSTANCE_CREATED,
        instance.id,
        instance.schema_id,
        schema_version=instance.schema_version,
        agent_id=principal.agent_id,
        detail=_starter_detail(principal),
    )
    _emit_event("instance.started", _instance_event_payload(instance))
    if instance.state is InstanceState.COMPLETED:
        _audit.append(
            EventType.INSTANCE_COMPLETED,
            instance.id,
            instance.schema_id,
            schema_version=instance.schema_version,
        )
        _emit_event("instance.completed", _instance_event_payload(instance))
    _after_advance(schema, None, instance)


def _record_spawned_instance(spawned: ProcessInstance, origin: ProcessInstance) -> None:
    """Record and start an instance the engine started on its own (``on_spawn``).

    Sub-process children and follow-up instances never pass through a start
    request, so :func:`_record_instance_start` never ran for them: they had no
    ``INSTANCE_CREATED``, and the KPIs (counted from the audit) disagreed with
    the instance list. This writes exactly one ``INSTANCE_CREATED`` per spawned
    instance, with ``detail.origin_instance`` (the triggering instance) and
    ``detail.trigger`` (``subprocess`` or ``follow_up``), plus an
    ``INSTANCE_COMPLETED`` when the instance already finished while it was
    started (e.g. a sub-process without an interactive step) -- nobody else
    would record that completion. A completion later on goes through the
    ordinary completion path (:func:`_record_completion`).

    It also gives the spawned instance the **start** a direct start gets
    (:func:`_start_spawned_instance`): start and activation times, the mail
    of each step that is ready at once, and ``instance.started`` (plus
    ``instance.completed`` when it already finished). Without them a child's
    steps had no due date, no escalation and no waiting-time figure, and
    their modelled mail was never sent.

    Skipped for a test instance: the engine passes ``is_test`` on to every
    instance a test instance spawns (sub-process child, SYNC and ASYNC
    follow-up, and so on down the chain), so the spawned instance's own flag
    decides -- the same flag every later path (completion, worklists, mail,
    webhooks, KPIs) consults. An ASYNC follow-up has no back-reference, so a
    walk up the parent chain could not have recognised it.

    It runs inside the engine, so it never raises: an audit write or a side
    effect that fails is logged instead of aborting an advance that has
    already stored instances.

    :param spawned: the new instance, instantiated and advanced (mutated:
        activation stamps, then stored again).
    :param origin: the instance whose progress started it.
    """

    if spawned.is_test:
        if spawned.unaudited_decisions:
            spawned.unaudited_decisions = []
            _instances.put(spawned)
        return
    detail = {
        "origin_instance": origin.id,
        "trigger": "subprocess" if spawned.parent_node_id else "follow_up",
    }
    try:
        _audit.append(
            EventType.INSTANCE_CREATED,
            spawned.id,
            spawned.schema_id,
            schema_version=spawned.schema_version,
            detail=detail,
        )
        if spawned.unaudited_decisions:
            # Decisions its first advance took, after its creation event.
            _record_branch_decisions(spawned, _effective_schema_for(spawned))
            _instances.put(spawned)
        if spawned.state is InstanceState.COMPLETED:
            _audit.append(
                EventType.INSTANCE_COMPLETED,
                spawned.id,
                spawned.schema_id,
                schema_version=spawned.schema_version,
            )
    except Exception:  # noqa: BLE001 -- observer inside the engine must not raise
        logging.getLogger("procworks.api").exception(
            "could not audit the start of instance %s", spawned.id
        )
    try:
        _start_spawned_instance(spawned)
    except Exception:  # noqa: BLE001 -- observer inside the engine must not raise
        logging.getLogger("procworks.api").exception(
            "could not run the start side effects of instance %s", spawned.id
        )


def _start_spawned_instance(spawned: ProcessInstance) -> None:
    """The start side effects of a real instance the engine spawned.

    The same a direct start runs after its audit (:func:`_record_instance_start`
    -> :func:`_after_advance`), minus what is not per instance:

    * **activation clock** -- ``started_at`` and ``node_activated_at`` of every
      human step that is ready at once (:func:`_stamp_activations`); due dates,
      escalation and the waiting-time KPI all start from these stamps;
    * **mail** of every such step (:func:`_notify_ready`, which reads the
      stamps, so it runs second);
    * ``instance.started`` and, for an instance that already finished while
      it was spawned, ``instance.completed`` -- in that order, like a direct
      start;
    * the stamped instance is stored again.

    Pushes are not driven here: the push drive scans every instance and runs
    in the advance of the request that spawned this one. Called for real
    instances only (a test instance announces and notifies nothing).

    :param spawned: the spawned instance (mutated, then stored).
    """

    schema = _effective_schema_for(spawned)
    _stamp_activations(schema, None, spawned)
    _notify_ready(schema, None, spawned)
    _instances.put(spawned)
    _emit_event("instance.started", _instance_event_payload(spawned))
    if spawned.state is InstanceState.COMPLETED:
        _emit_event("instance.completed", _instance_event_payload(spawned))


def _record_step_completion(
    before: ProcessInstance,
    after: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    *,
    agent_id: str | None,
    detail: dict[str, str] | None,
    supervision: dict[str, str] | None = None,
) -> None:
    """Audit and announce one completed step -- shared by every completion path.

    The human completion (``/complete``, ``/v1/.../complete``) and the
    completion of an automatic step from outside (external-task worker, push
    callback) end in the same engine call, so they leave the same record:
    ``ACTIVITY_COMPLETED`` for the step, an ``ACTIVITY_SUPERVISED`` with the
    reason for a supervision completion, and -- when the instance just finished
    -- ``INSTANCE_COMPLETED`` plus the ``instance.completed`` webhook
    (:func:`_record_completion`). Before, the automatic paths recorded nothing,
    so KPIs, process map, history and subscribers never learned of the step.

    A throw-away test instance records nothing (its whole lifecycle stays out
    of the audit). The caller runs :func:`_after_advance` afterwards.

    :param before: the instance before the completion (decides the test skip
        and whether the instance just finished).
    :param after: the instance after the completion.
    :param schema: the effective schema, for the step's label.
    :param node_id: the completed step.
    :param agent_id: the agent the completion is attributed to, if any.
    :param detail: the event detail (actor, ready_at, worker ...), may be ``None``.
    :param supervision: detail of a supervision completion (``actor`` and
        ``reason``); when given, an ``ACTIVITY_SUPERVISED`` event carrying it
        follows the completion.
    """

    if before.is_test:
        return
    _audit.append(
        EventType.ACTIVITY_COMPLETED,
        after.id,
        after.schema_id,
        schema_version=after.schema_version,
        node_id=node_id,
        label=_label_of(schema, node_id),
        agent_id=agent_id,
        detail=detail,
    )
    if supervision is not None:
        # Own event (not in KPIs/mining): the reason stays in the hash chain
        # and is visible in the history.
        _audit.append(
            EventType.ACTIVITY_SUPERVISED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
            node_id=node_id,
            label=_label_of(schema, node_id),
            detail=supervision,
        )
    _record_completion(before, after)


def _record_external_completion(
    schema: ProcessSchema,
    before: ProcessInstance,
    after: ProcessInstance,
    task: ExternalTask,
    actor: str | None,
) -> None:
    """Record a step completed by an external-task worker or a push callback.

    The ``on_complete`` observer of :class:`ExternalTaskRuntime`. Builds the
    detail of the step's ``ACTIVITY_COMPLETED`` and hands over to the shared
    :func:`_record_step_completion`:

    * ``actor`` -- the identity that sent the report (token or login subject);
    * ``external_task`` -- the task id, linking the history to the task;
    * ``worker_id`` for a pulled task, or ``channel: push`` for a pushed one --
      a push task's "worker" is its callback token, a credential for exactly
      this task, which has no place in a log anybody with audit rights reads;
    * ``ready_at`` -- when the step became ready, if the boundary stamped it.

    No ``agent_id``: no person stands behind an automatic step.
    """

    detail: dict[str, str] = {"external_task": task.id}
    if actor:
        detail["actor"] = actor
    if task.topic:
        detail["worker_id"] = task.worker_id or ""
    else:
        detail["channel"] = "push"
    ready = before.node_activated_at.get(task.node_id)
    if ready is not None:
        detail["ready_at"] = ready.isoformat()
    _record_step_completion(before, after, schema, task.node_id, agent_id=None, detail=detail)


def _record_completion(before: ProcessInstance, after: ProcessInstance) -> None:
    """Append an INSTANCE_COMPLETED event when an instance has just finished."""

    if (
        before.state is InstanceState.RUNNING
        and after.state is InstanceState.COMPLETED
    ):
        _audit.append(
            EventType.INSTANCE_COMPLETED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
        )
        _emit_event("instance.completed", _instance_event_payload(after))


# --- request models ------------------------------------------------------


class DemoLogin(BaseModel):
    """One advertised demo login (public throw-away demo only)."""

    login: str = Field(..., examples=["mara.modell"])
    name: str = Field(..., examples=["Mara Modell"])
    role: str = Field(..., examples=["modeler"])


class AuthConfig(BaseModel):
    mode: str = Field(..., examples=["password"])
    password_login: bool = False
    # --- Demo-only fields (populated solely in PROCWORKS_DEMO_MODE) ---------
    #: True in a public throw-away demo -> the client may auto-login and show
    #: the credential hint. False/absent everywhere else.
    demo: bool = False
    #: Shared password of the seeded demo logins (already public in demo mode).
    #: null outside demo mode -- never leaks a real deployment's secrets.
    demo_password: str | None = None
    #: Login the client should auto-authenticate a fresh visitor as (the modeler).
    demo_autologin: str | None = None
    #: The other advertised demo logins, for one-click role switching.
    demo_logins: list[DemoLogin] = []
    #: Broker endpoint the SPA POSTs the post-demo survey to (``PROCWORKS_DEMO_FEEDBACK_URL``).
    #: null/absent -> the SPA shows no "end demo & give feedback" flow. Demo-only.
    demo_feedback_url: str | None = None
    # --- OIDC redirect login (jwt mode only, opt-in via configuration) ------
    #: IdP authorization endpoint for the SPA's Authorization-Code+PKCE login.
    #: Populated only in jwt mode when ``PROCWORKS_JWT_AUTHORIZE_URL``,
    #: ``PROCWORKS_JWT_TOKEN_URL`` and ``PROCWORKS_JWT_CLIENT_ID`` are all set;
    #: otherwise the SPA keeps the plain bearer-token field (the redirect
    #: login is opt-in). No secrets: a public SPA client uses PKCE, not a client secret.
    oidc_authorize_url: str | None = None
    #: IdP token endpoint the SPA exchanges the authorization code at.
    oidc_token_url: str | None = None
    #: Public (PKCE) client id registered at the IdP for the SPA.
    oidc_client_id: str | None = None
    #: Scopes the SPA requests (``PROCWORKS_JWT_OIDC_SCOPES``).
    oidc_scopes: str | None = None


class LoginRequest(BaseModel):
    login: str = Field(..., examples=["erika.musterfrau"])
    password: str = Field(..., examples=["geheim"])


class LoginResponse(BaseModel):
    token: str
    principal: Principal
    must_change: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    roles: list[str] = Field(..., examples=[["operator"]])
    agent_id: str | None = Field(default=None, examples=["a1"])
    login: str | None = Field(default=None, examples=["erika.musterfrau"])
    display_name: str | None = Field(default=None, examples=["Erika Musterfrau"])


class CreateUserResponse(BaseModel):
    user: UserView
    login: str
    initial_password: str


class ResetPasswordResponse(BaseModel):
    login: str
    initial_password: str


class ResetRequest(BaseModel):
    load_demo: bool = Field(
        default=False,
        description="When true, reload the built-in demo data after wiping; "
        "otherwise leave an empty system.",
        examples=[True],
    )
    load_o2c: bool = Field(
        default=False,
        description="When true, additionally load the large Order-to-Cash data "
        "set (own org, six schemas, running orders). Independent of load_demo; "
        "both may be combined.",
        examples=[True],
    )


class ResetResponse(BaseModel):
    demo_loaded: bool
    o2c_loaded: bool = False
    #: Passwort der angelegten Beispiel-Logins -- nur hier, genau einmal.
    #: Zufällig außer in der öffentlichen Demo; ``None``, wenn keine Logins
    #: angelegt wurden (kein Datensatz oder kein Passwort-Login).
    example_password: str | None = None
    #: Die Logins, die der geladene Datensatz angelegt hat (Reihenfolge der
    #: Datensatz-Tabelle, Basis-Demo vor Order-to-Cash) -- damit der Dialog
    #: Beispiele aus *diesem* Datensatz nennt. Leer genau dann, wenn
    #: ``example_password`` ``None`` ist.
    example_logins: list[str] = Field(default_factory=list)
    schemas: int
    instances: int
    org_models: int
    users: int


class MonitoringRevision(BaseModel):
    """A cheap, monotonic revision counter of the runtime event history.

    Clients poll ``GET /monitoring/revision`` and refresh their live views (task
    lists, monitoring, the running instance) whenever the value changes, so
    progress made by other users becomes visible without a manual reload.
    """

    revision: int


class CreateSchemaRequest(BaseModel):
    name: str = Field(..., examples=["Urlaubsantrag"])


class SaveTemplateRequest(BaseModel):
    """Capture an existing schema as a reusable template."""

    schema_id: str = Field(..., examples=["urlaubsantrag"])
    name: str = Field(..., examples=["Urlaubsantrag (Standard)"])
    description: str = Field(default="", examples=["Antrag stellen, prüfen, entscheiden"])
    category: str = Field(default="", examples=["Personal"])


class InstantiateTemplateRequest(BaseModel):
    """Create a fresh draft schema from a template (optional new name)."""

    name: str | None = Field(default=None, examples=["Urlaubsantrag 2026"])


class TemplateRoleInfo(BaseModel):
    """A performer of a template and the steps it carries (gallery text)."""

    name: str
    steps: list[str]


class TemplateSummary(BaseModel):
    """Lightweight catalogue entry for the template gallery (no blueprint).

    Listing returns summaries so the (potentially large) embedded blueprint is
    only transferred when a single template is fetched or instantiated.
    """

    id: str
    name: str
    description: str
    category: str
    origin: TemplateOrigin
    #: Number of steps (activities/sub-processes) in the blueprint.
    step_count: int = 0
    #: Who does what, derived from the staff rules (``templates.template_roles``):
    #: one entry per performer with the steps it carries.
    roles: list[TemplateRoleInfo] = Field(default_factory=list)


class SerialInsertRequest(BaseModel):
    label: str = Field(..., examples=["Antrag prüfen"])
    after_node_id: str = Field(..., examples=["start"])
    #: Optional: the target of the edge to splice when the anchor has several
    #: exits (the start of one branch of a split, also an empty XOR branch).
    before_node_id: str | None = Field(default=None, examples=[None])


class ParallelInsertRequest(BaseModel):
    branch_labels: list[str] = Field(..., examples=[["Fachprüfung", "Budgetprüfung"]])
    after_node_id: str = Field(..., examples=["start"])


class Branch(BaseModel):
    """One cell of an XOR partition (K7); fields apply per discriminator kind."""

    label: str = Field(..., examples=["Freigabe Leitung"])
    upper: float | None = Field(default=None, examples=[1000])
    bool_value: bool | None = Field(default=None, examples=[True])
    values: list[str] = Field(default_factory=list, examples=[["A", "B"]])
    is_else: bool = Field(default=False)


class ConditionalInsertRequest(BaseModel):
    discriminator: str = Field(..., examples=["betrag"])
    branches: list[Branch]
    after_node_id: str = Field(..., examples=["start"])


class RenameNodeRequest(BaseModel):
    label: str = Field(..., examples=["Antrag genehmigen"])


class MoveNodeRequest(BaseModel):
    """Target position for a node move: directly after this anchor node."""

    after_node_id: str = Field(..., examples=["start"])


class LoopInsertRequest(BaseModel):
    """A REPEAT-UNTIL loop block (K6): body label + decidable exit condition."""

    label: str = Field(..., examples=["Nacharbeit erledigen"])
    after_node_id: str = Field(..., examples=["start"])
    #: INSTANCE element that decides the loop in every iteration (BOOLEAN for
    #: the shorthand below; any partitionable type when ``cells`` are given).
    discriminator: str = Field(..., examples=["nacharbeit_noetig"])
    #: Boolean shorthand: the body repeats while the discriminator equals this
    #: value. Ignored when ``cells`` are given.
    repeat_value: bool = True
    #: Optional repeat/exit partition (stage S3): cells tile the
    #: discriminator's domain like a K7 partition (THRESHOLD/BOOLEAN/ENUM per
    #: the element's type) and classify each cell into repeat or exit.
    cells: list[LoopCell] | None = None
    #: Hard brake (stage S3): total body runs are capped at this bound (>= 2)
    #: and the T2 critical path charges the body that many times. Defaults to
    #: the same 10 as the web dialog, so no loop runs unbounded by accident; an explicit ``null``
    #: means "unbounded".
    max_iterations: int | None = ops.DEFAULT_MAX_ITERATIONS


class LoopDecisionRequest(BaseModel):
    """Replace the exit condition of an existing loop (K6b, LOOP_END node)."""

    node_id: str = Field(..., examples=["loopend_1"])
    discriminator: str = Field(..., examples=["nacharbeit_noetig"])
    repeat_value: bool = True
    cells: list[LoopCell] | None = None
    max_iterations: int | None = None


class AddDataElementRequest(BaseModel):
    name: str = Field(..., examples=["betrag"])
    data_type: DataType = Field(..., examples=[DataType.FLOAT])
    element_id: str | None = Field(default=None, examples=["betrag"])


class UpdateDataElementRequest(BaseModel):
    name: str | None = Field(default=None, examples=["betrag"])
    data_type: DataType | None = Field(default=None, examples=[DataType.FLOAT])


class ConnectDataRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    element_id: str = Field(..., examples=["betrag"])
    mode: AccessMode = Field(..., examples=[AccessMode.READ])
    mandatory: bool = True
    param_type: DataType | None = None


class FormFieldRequest(BaseModel):
    element_id: str = Field(..., examples=["betrag"])
    widget: WidgetKind = Field(..., examples=[WidgetKind.NUMBER])
    label: str | None = Field(default=None, examples=["Betrag"])
    mode: AccessMode = Field(default=AccessMode.WRITE, examples=[AccessMode.WRITE])
    required: bool = True
    options: list[str] = Field(default_factory=list)
    help_text: str | None = None
    group: str = ""
    #: Optional input checks: bounds for numbers, pattern and maximum
    #: length for text. U2 checks they fit the field; completions enforce them.
    min_value: float | None = None
    max_value: float | None = None
    pattern: str | None = None
    max_length: int | None = None


class SetFormRequest(BaseModel):
    title: str = ""
    fields: list[FormFieldRequest]
    columns: int = Field(default=1, ge=1, le=3)


class RegisterConnectorRequest(BaseModel):
    name: str = Field(..., examples=["ERP-Kunden"])
    kind: ConnectorKind = Field(..., examples=[ConnectorKind.MS_SQL])
    connector_id: str | None = Field(default=None, examples=["erp"])


class BindExternalDataRequest(BaseModel):
    connector_id: str = Field(..., examples=["erp"])
    entity: str = Field(..., examples=["Kunde"])
    key_element_id: str = Field(..., examples=["kunden_nr"])


class QueryFilterRequest(BaseModel):
    column: str = Field(..., examples=["kd_id"])
    column_type: DataType = Field(..., examples=[DataType.INTEGER])
    operator: FilterOperator = Field(..., examples=[FilterOperator.EQ])
    key_element_id: str = Field(..., examples=["kunden_nr"])


class OrderByRequest(BaseModel):
    column: str = Field(..., examples=["created"])
    descending: bool = False


class SqlSelectRequest(BaseModel):
    connector_id: str = Field(..., examples=["erp"])
    entity: str = Field(..., examples=["Kunde"])
    column: str = Field(..., examples=["name"])
    column_type: DataType = Field(..., examples=[DataType.STRING])
    aggregate: AggregateKind = AggregateKind.NONE
    filters: list[QueryFilterRequest] = Field(default_factory=list)
    cardinality: Cardinality = Field(..., examples=[Cardinality.KEY_UNIQUE])
    order_by: list[OrderByRequest] = Field(default_factory=list)
    unique_column: str = ""


class SqlWriteRequest(BaseModel):
    connector_id: str = Field(..., examples=["erp"])
    entity: str = Field(..., examples=["Kunde"])
    column: str = Field(..., examples=["status"])
    column_type: DataType = Field(..., examples=[DataType.STRING])
    filters: list[QueryFilterRequest] = Field(default_factory=list)
    unique_column: str = ""


class ImportBpmnRequest(BaseModel):
    xml: str = Field(..., description="BPMN 2.0 XML document")
    name: str | None = Field(default=None, examples=["Importierter Prozess"])
    schema_id: str | None = Field(default=None, examples=["imported"])


class AddRoleRequest(BaseModel):
    name: str = Field(..., examples=["Sachbearbeiter"])
    role_id: str | None = Field(default=None, examples=["sb"])


class AddOrgUnitRequest(BaseModel):
    name: str = Field(..., examples=["Einkauf"])
    parent_id: str | None = None
    org_unit_id: str | None = Field(default=None, examples=["einkauf"])
    manager_id: str | None = Field(default=None, examples=["a1"])


class AddAgentRequest(BaseModel):
    name: str = Field(..., examples=["Erika Muster"])
    role_ids: list[str] = Field(default_factory=list, examples=[["sb"]])
    org_unit_id: str | None = None
    agent_id: str | None = None
    deputy_id: str | None = Field(default=None, examples=["a2"])
    email: str | None = Field(default=None, examples=["erika@firma.de"])


class UpdateAgentRequest(BaseModel):
    name: str | None = Field(default=None, examples=["Erika Mustermann"])
    role_ids: list[str] | None = Field(default=None, examples=[["sb"]])
    org_unit_id: str | None = Field(default=None, examples=["einkauf"])
    email: str | None = Field(default=None, examples=["erika@firma.de"])


class SetManagerRequest(BaseModel):
    manager_id: str | None = Field(default=None, examples=["a1"])


class SetParentRequest(BaseModel):
    parent_id: str | None = Field(default=None, examples=["unit_1"])


class SetDeputyRequest(BaseModel):
    deputy_id: str | None = Field(default=None, examples=["a2"])


class SetMailboxRequest(BaseModel):
    mailbox: str | None = Field(default=None, examples=["einkauf@firma.de"])


class SetMailBindingRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    #: ``None`` clears the notification of the node; a value sets/replaces it.
    binding: MailBinding | None = Field(default=None)


class CreateOrgModelRequest(BaseModel):
    name: str = Field(..., examples=["Stadtverwaltung"])
    org_model_id: str | None = Field(default=None, examples=["org_city"])


class LinkOrgModelRequest(BaseModel):
    org_model_id: str = Field(..., examples=["org_city"])


class AssignServiceRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    name: str = Field(..., examples=["Antrag erfassen"])
    automatic: bool = False
    template_id: str | None = Field(default=None, examples=["tmpl_erfassen"])
    parameter_mapping: dict[str, str] = Field(default_factory=dict)


class SetAutomationRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    automation: AutomationKind = Field(..., examples=[AutomationKind.EXTERNAL_TASK])
    topic: str | None = Field(default=None, examples=["invoice-check"])
    endpoint_ref: str | None = Field(default=None, examples=["webhook_1"])
    retry_max: int | None = Field(default=None, ge=0, examples=[5])
    retry_backoff_ms: int | None = Field(default=None, ge=0, examples=[2000])
    request_timeout_ms: int | None = Field(default=None, ge=0, examples=[30000])


class AddActivityTemplateRequest(BaseModel):
    name: str = Field(..., examples=["Antrag erfassen"])
    executor: ExecutorKind = Field(..., examples=[ExecutorKind.MANUAL])
    inputs: list[TemplateParameter] = Field(default_factory=list)
    outputs: list[TemplateParameter] = Field(default_factory=list)
    template_id: str | None = Field(default=None, examples=["tmpl_erfassen"])


class AssignStaffRuleRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    rule: StaffRule


class SetValueClassRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    value_class: ValueClass | None = Field(
        default=None, examples=[ValueClass.VALUE_ADDING]
    )


class SetPriorityRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    #: When ``None`` the priority annotation is cleared.
    priority: WorkItemPriority | None = Field(
        default=None,
        examples=[
            WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH)
        ],
    )


class SetTimeConstraintRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    #: When ``None`` the temporal annotation is cleared.
    constraint: TimeConstraint | None = Field(
        default=None, examples=[TimeConstraint(max_duration_seconds=3600)]
    )


class SetDeadlineRequest(BaseModel):
    deadline_seconds: float | None = Field(default=None, examples=[86400])


class InsertSubprocessRequest(BaseModel):
    after_node_id: str = Field(..., examples=["start"])
    target_schema_id: str = Field(..., examples=["schema_2"])
    target_version: int = Field(..., examples=[1])
    label: str = ""
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)


class SubprocessMappingRequest(BaseModel):
    node_id: str = Field(..., examples=["sub_1"])
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)


class ConvertToSubprocessRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    target_schema_id: str = Field(..., examples=["schema_2"])
    target_version: int = Field(..., examples=[1])
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)


class SetSubprocessBindingRequest(BaseModel):
    node_id: str = Field(..., examples=["sub_1"])
    target_schema_id: str = Field(..., examples=["schema_2"])
    target_version: int = Field(..., examples=[1])
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)


class LibraryFlagRequest(BaseModel):
    is_library: bool = Field(..., examples=[True])


class LibraryDataElement(BaseModel):
    id: str
    name: str
    data_type: str


class SubprocessLibraryEntry(BaseModel):
    id: str
    name: str
    version: int
    data_elements: list[LibraryDataElement]


class LinkFollowUpRequest(BaseModel):
    target_schema_id: str = Field(..., examples=["schema_3"])
    target_version: int | None = None
    trigger: FollowUpTrigger = FollowUpTrigger.ON_COMPLETE
    condition: str | None = None
    handover_mapping: dict[str, str] = Field(default_factory=dict)
    mode: FollowUpMode = FollowUpMode.ASYNC


class SyncEdgeRequest(BaseModel):
    """A K4 synchronisation edge between two parallel activities."""

    source_id: str = Field(..., examples=["act_1"])
    target_id: str = Field(..., examples=["act_2"])


class InsertBetweenRequest(BaseModel):
    """ADEPT insertBetweenNodeSets: activity between two node sets (K4)."""

    label: str = Field(..., examples=["Zwischenprüfung"])
    source_ids: list[str] = Field(..., examples=[["act_1"]])
    target_ids: list[str] = Field(..., examples=[["act_2"]])


class SimulateRequest(BaseModel):
    """Seed values for one side-effect-free what-if run (E6)."""

    data: dict[str, object] = Field(default_factory=dict)


class SetEscalationPolicyRequest(BaseModel):
    """Modelled overdue reaction of a node (T3/E9); ``policy: null`` clears."""

    node_id: str = Field(..., examples=["act_1"])
    policy: EscalationPolicy | None = None


class StartActivityRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    #: Acting agent (E1: starting presupposes ownership, W4). A bound login
    #: always acts as itself; open dev mode must name the agent.
    agent_id: str | None = Field(default=None, examples=["agent_erika"])


class ActivityDetailRequest(BaseModel):
    """Suspend/resume/reset a started activity (E2, detail-state overlay)."""

    node_id: str = Field(..., examples=["act_1"])
    agent_id: str | None = Field(default=None, examples=["agent_erika"])


#: Hinweis, wenn „Problem melden“ ohne sichtbare Begründung kommt.
FAIL_REASON_REQUIRED = (
    "Problem melden: Bitte eine Begründung angeben – sie steht im Verlauf des "
    "Vorgangs und sagt der Prozessverantwortung, was zu tun ist."
)


#: Unicode categories whose characters show nothing on their own: control and
#: format characters (zero-width space/joiners, word joiner, byte-order mark,
#: soft hyphen), combining marks without a base, separators -- and code points
#: that carry no agreed character at all: unassigned ones (``Cn``, shown as
#: nothing or an empty box) and private-use ones (``Co``, whose look depends on
#: a font the reader of the history does not have).
_INVISIBLE_CATEGORIES = frozenset(
    {"Cc", "Cf", "Mn", "Me", "Zs", "Zl", "Zp", "Cn", "Co"}
)
#: Letters that render as blank space although Unicode files them as letters
#: or symbols (Hangul fillers, the blank Braille pattern).
_BLANK_LOOKING = frozenset({0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800})


def _visible_reason(reason: str | None) -> str | None:
    """The reason as it will be recorded, or ``None`` if it shows nothing.

    A reason duty exists because the history must say *why* something
    happened; a reason a reader cannot see says nothing. Plain whitespace was
    refused before, but a reason made only of zero-width characters (U+200B
    and friends), a byte-order mark or a Hangul filler passed -- and stood in
    the history as an empty cell. Every reason duty of the API decides here.

    :param reason: the reason from the request (``None`` or blank allowed).
    :returns: the reason with surrounding whitespace removed, unchanged
        otherwise (a zero-width joiner inside an emoji sequence stays), or
        ``None`` when no character of it is visible
        (:data:`_INVISIBLE_CATEGORIES`, :data:`_BLANK_LOOKING`, ``isspace``).
    """

    cleaned = (reason or "").strip()
    for char in cleaned:
        if not (
            char.isspace()
            or unicodedata.category(char) in _INVISIBLE_CATEGORIES
            or ord(char) in _BLANK_LOOKING
        ):
            return cleaned
    return None


def _reason_required(what: str, message: str) -> HTTPException:
    """The 422 for a missing reason -- one code for every reason duty.

    Several actions demand a visible reason because it is what the history
    keeps (a failure report, an ad-hoc change, a supervisory completion or
    data correction). The answer carries the language-neutral code
    ``REQ.reason-required`` with ``params.what`` naming the action, so the
    client words it from its catalogue; ``message`` is the German sentence
    the endpoint answered with before.

    Used by every reason duty: ``fail`` (failure report), ``suspend`` (a
    paused step), ``supervision``
    (an unbound login completes a staffed step), ``automatic`` (an automatic
    step is completed by hand), ``data-correction`` (a correction outside the
    caller's own step) and ``adhoc`` (the occasion of an ad-hoc change). The
    web client recognises the supervisory refusals (``supervision``,
    ``automatic``, ``data-correction``) by the code and asks for the reason;
    nothing may depend on the wording of ``message``.

    :param what: the action, e.g. ``"fail"`` (``params.what``).
    :param message: the sentence for callers without a catalogue.
    :returns: the exception, to be raised by the caller.
    """

    return HTTPException(
        status_code=422,
        detail={"message": message, "code": "REQ.reason-required", "params": {"what": what}},
    )


def _require_fail_reason(reason: str) -> str:
    """Return the reason of a failure report, or refuse a blank one (422).

    A failed step waits for someone to recover it, and the reason is what
    tells them why. A reason of only spaces, tabs or line breaks used to be
    accepted -- the step stood as failed in the history with no cause at all.
    What counts as visible decides :func:`_visible_reason`.
    Checked before the engine runs, so a refused report changes nothing.

    :param reason: the reason from the request (may be blank).
    :returns: the reason without surrounding whitespace.
    :raises HTTPException: 422 ``REQ.reason-required`` (``what`` ``fail``,
        ``message`` :data:`FAIL_REASON_REQUIRED`) when nothing visible remains
        after stripping.
    """

    cleaned = _visible_reason(reason)
    if cleaned is None:
        raise _reason_required("fail", FAIL_REASON_REQUIRED)
    return cleaned


#: Hinweis, wenn „Anhalten“ ohne sichtbaren Grund kommt.
SUSPEND_REASON_REQUIRED = (
    "Anhalten: Bitte einen Grund angeben – er steht im Verlauf des Vorgangs "
    "und sagt allen Beteiligten, warum die Aufgabe ruht."
)


def _require_suspend_reason(reason: str | None) -> str:
    """Return the reason of a pause, or refuse a missing or blank one (422).

    A paused step stays with its owner while its deadline keeps running; the
    history must say why it rests, or nobody who looks at the case can tell a
    justified pause from a forgotten task. The reason is recorded at the
    ``ACTIVITY_SUSPENDED`` event only (the instance keeps no field for it).
    What counts as visible decides :func:`_visible_reason`, as for every
    reason duty. Checked before the engine runs, so a refused pause changes
    nothing.

    :param reason: the reason from the request; ``None`` when a caller does
        not send the field (an integration written before the duty) -- it
        gets the same coded 422, not a schema error.
    :returns: the reason without surrounding whitespace.
    :raises HTTPException: 422 ``REQ.reason-required`` (``what`` ``suspend``,
        ``message`` :data:`SUSPEND_REASON_REQUIRED`) when nothing visible
        remains.
    """

    cleaned = _visible_reason(reason)
    if cleaned is None:
        raise _reason_required("suspend", SUSPEND_REASON_REQUIRED)
    return cleaned


class SuspendActivityRequest(BaseModel):
    """Pause a started activity (E2, V1) -- with a reason for the history.

    ``reason`` is optional in the schema only so that a request without it
    gets the coded refusal of :func:`_require_suspend_reason`; it is required.
    """

    node_id: str = Field(..., examples=["act_1"])
    agent_id: str | None = Field(default=None, examples=["agent_erika"])
    reason: str | None = Field(default=None, examples=["Warte auf Rückruf des Kunden"])


class FailActivityRequest(BaseModel):
    """Report a started activity as failed (E2, V3) -- with a reason."""

    node_id: str = Field(..., examples=["act_1"])
    agent_id: str | None = Field(default=None, examples=["agent_erika"])
    reason: str = Field(..., examples=["Unterlagen unvollständig"])


class ClaimActivityRequest(BaseModel):
    """Take over an offered task (E1): it leaves everyone else's worklist."""

    node_id: str = Field(..., examples=["act_1"])
    agent_id: str | None = Field(default=None, examples=["agent_erika"])


class ReturnActivityRequest(BaseModel):
    """Return a claimed task to the open offer (E1, W3)."""

    node_id: str = Field(..., examples=["act_1"])
    agent_id: str | None = Field(default=None, examples=["agent_erika"])


class CompleteActivityRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    data: dict[str, object] = Field(default_factory=dict)
    agent_id: str | None = Field(default=None, examples=["a1"])
    #: Pflichtbegründung eines **Aufsichtseingriffs**: Abschluss durch einen
    #: Login *ohne* Agentenbindung (siehe :func:`_require_supervision_reason`).
    supervision_reason: str | None = Field(
        default=None, examples=["Bearbeiterin krank, Frist läuft ab"]
    )


class AdhocInsertRequest(BaseModel):
    after_node_id: str = Field(..., examples=["act_1"])
    label: str = Field(..., examples=["Zusatzpruefung"])
    #: Wer den neuen Schritt bearbeitet. Pflicht im Kern (B2 für den neuen
    #: Schritt): ohne Regel stünde er in keiner Arbeitsliste. Optional
    #: im Schema nur, damit der Kern die Ablehnung mit Befund ``B2.no-staff``
    #: formuliert statt eines Pydantic-Fehlers.
    staff_rule: StaffRule | None = None
    #: Anlass der Änderung; Pflicht außer bei Test-Instanzen, steht im
    #: Ereignis ``ADHOC_INSERTED``.
    reason: str | None = None


class AdhocDeleteRequest(BaseModel):
    node_id: str = Field(..., examples=["act_2"])
    #: Anlass der Änderung; Pflicht außer bei Test-Instanzen.
    reason: str | None = None


class AdhocRenameRequest(BaseModel):
    node_id: str = Field(..., examples=["act_2"])
    label: str = Field(..., examples=["Zusatzpruefung (angepasst)"])
    #: Anlass der Änderung; Pflicht außer bei Test-Instanzen.
    reason: str | None = None


class RevisionRequest(BaseModel):
    new_schema_id: str | None = Field(default=None, examples=["schema_v2"])


class MigrateRequest(BaseModel):
    target_schema_id: str = Field(..., examples=["schema_v2"])
    data_mapping: dict[str, object] = Field(default_factory=dict)


class MigrationReport(BaseModel):
    migratable: bool
    findings: list[ValidationFinding]


class WorklistReport(BaseModel):
    state: str
    ready_activities: list[str]
    pending_decisions: list[str]


class ValidationReport(BaseModel):
    correct: bool
    findings: list[ValidationFinding]
    #: Stufe B: whether the schema is not merely *correct* but also
    #: *runnable*. ``correct`` (Stufe A) holds after every operation; this one may
    #: legitimately be false while a draft is still being built. Additive fields --
    #: an older client simply ignores them.
    releasable: bool = True
    #: The Stufe-B findings behind ``releasable`` (empty when it is true). Today
    #: this is rule B2: an interactive step without a staff rule would be
    #: activated at runtime but appear in nobody's worklist.
    release_findings: list[ValidationFinding] = []


# --- helpers -------------------------------------------------------------


def _get_or_404(schema_id: str) -> ProcessSchema:
    schema = _store.get(schema_id)
    if schema is None:
        raise HTTPException(status_code=404, detail=f"schema '{schema_id}' not found")
    return hydrate_org(schema, _org_resolver)


def _persist_schema(schema: ProcessSchema) -> ProcessSchema:
    """Store a schema, clearing hydrated shared-org data first (single source).

    Saves conditionally (the store refuses a schema someone else saved since
    it was read, :class:`ConcurrentModification`). The stored copy may be a
    dehydrated copy of ``schema``; its new version is carried back to
    ``schema``, so the caller may save the same schema again.

    :param schema: the (hydrated) schema to store.
    :returns: ``schema``, carrying the new stored version.
    """

    stored = _store.put(dehydrate_org(schema))
    version = stored_version(stored)
    if version is not None and stored is not schema:
        remember_version(schema, version)
    return schema


def _claim_family(schema: ProcessSchema) -> list[ProcessSchema]:
    """Load all stored schemas and claim ``schema``'s family for this unit of work.

    A revision or release decides by the whole family (one version
    numbering, :func:`procworks.operations.family_root_id`). Saving the
    family's first version again -- content unchanged, conditionally --
    makes two concurrent such requests on any versions of one family
    collide: one goes through, the other is repeated and then sees the
    first one's result. The root is skipped when it is ``schema`` itself
    (the request saves that one anyway) or no longer stored.

    **Claim first, read afterwards.** The root is found by walking
    ``revision_of`` upwards with single loads (a lineage never changes once
    stored), claimed, and only *then* are the stored schemas read. With
    "read committed" transactions (PostgreSQL) each read sees what was
    committed when it ran: read before the claim, a request could see the
    sibling drafts as they were before a concurrent request committed but
    the root as it is after -- its conditional save of the root then
    succeeded and both requests made (or released) the same version number.
    After a successful claim, every request of the family that committed
    earlier is visible to the reads, and every later one collides with the
    claim.

    :param schema: the schema the request acts on.
    :returns: all stored schemas, read after the claim for this unit of work.
    """

    root = _lineage_root(schema)
    if root.id != schema.id:
        _persist_schema(root)
    return [s for sid in _store.list_ids() if (s := _store.get(sid)) is not None]


def _lineage_root(schema: ProcessSchema) -> ProcessSchema:
    """Load the first version of ``schema``'s family by walking ``revision_of``.

    Same walk as :func:`procworks.operations.family_root_id`, but over single
    store loads instead of a list read beforehand -- so the caller can claim
    the root before it reads anything else (see :func:`_claim_family`).

    :param schema: any version of the family.
    :returns: the stored root as loaded now (carrying its stored version), or
        ``schema`` itself when it has no stored predecessor (a first version,
        a schema from before ``revision_of`` existed, or a predecessor that
        is no longer stored). A cycle in the lineage (only through a
        hand-edited store) ends the walk at the last schema before it repeats.
    """

    current = schema
    seen = {schema.id}
    while current.revision_of is not None and current.revision_of not in seen:
        parent = _store.get(current.revision_of)
        if parent is None:
            break
        seen.add(parent.id)
        current = parent
    return current


def _all_templates() -> dict[str, ProcessTemplate]:
    """Merge the built-in library with the stored user templates, keyed by id.

    Built-in templates (code) are listed first; a user template can never
    shadow a built-in because their id namespaces differ (``tpl-*`` vs. minted
    ``tpl_*``), but if one ever did the stored user template wins on lookup.
    """

    merged: dict[str, ProcessTemplate] = {
        t.id: t for t in builtin_templates_mod.builtin_templates()
    }
    for tid in _template_store.list_ids():
        template = _template_store.get(tid)
        if template is not None:
            merged[tid] = template
    return merged


def _get_template_or_404(template_id: str) -> ProcessTemplate:
    """Resolve a template by id (built-in or user), or raise HTTP 404."""

    user = _template_store.get(template_id)
    if user is not None:
        return user
    for template in builtin_templates_mod.builtin_templates():
        if template.id == template_id:
            return template
    raise HTTPException(status_code=404, detail=f"template '{template_id}' not found")


def _findings_422(findings: Iterable[ValidationFinding]) -> HTTPException:
    """Build the HTTP 422 that carries rule findings to the caller.

    Every rejection by a correctness rule (validator, D3/D6 data checks, U4
    mask checks) answers in the same shape ``{"findings": [...]}``, each
    finding serialised with ``model_dump()`` so ``code``/``params`` reach the
    client catalogue (``findingText``). Kept in one place so the shape cannot
    drift between endpoints.

    :param findings: the findings to report (typically non-empty).
    :returns: the exception to ``raise`` (optionally ``from`` the cause) --
        it is returned rather than raised so call sites keep their explicit
        ``raise`` and exception chaining.
    """

    return HTTPException(
        status_code=422,
        detail={"findings": [f.model_dump() for f in findings]},
    )


def _commit_or_422(result_fn: Callable[[], ProcessSchema]) -> ProcessSchema:
    """Execute an operation callable; map CorrectnessError to HTTP 422."""

    try:
        schema = result_fn()
    except CorrectnessError as exc:
        raise _findings_422(exc.findings) from exc
    return _persist_schema(schema)


def _get_org_or_404(org_id: str) -> OrgModel:
    org = _org_store.get(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail=f"org model '{org_id}' not found")
    return org


def _schemas_referencing(org_id: str) -> list[ProcessSchema]:
    """All stored schemas that resolve their staffing against this shared org."""

    result: list[ProcessSchema] = []
    for sid in _store.list_ids():
        schema = _store.get(sid)
        if schema is not None and schema.org_model_id == org_id:
            result.append(schema)
    return result


def _commit_org_or_422(result_fn: Callable[[], OrgModel]) -> OrgModel:
    """Apply a shared-org change, refusing it if it breaks a referencing schema.

    The org op is validated for internal consistency (validate-before-commit);
    additionally each schema that references the org is validated twice --
    hydrated with the *stored* org and with the *candidate* org -- so an org
    edit can never silently break a referencing process's staffing. Only if no
    referencing schema gains a finding is the new org persisted (atomic across
    the org boundary).

    **Only findings the change adds count**
    (:func:`~procworks.validator.findings_added`, the comparison every edit
    of a model uses). Rules
    get stricter over time; a released schema can violate a rule that did not
    exist at its release (a time value above the bound, an input pattern the
    complexity check now refuses), and a released schema cannot be edited to
    remove that (only a revision of it can). Counting such findings against
    every org change locked the
    org for everybody, administrators included. A finding the schema already
    had with the stored org -- even on the very step the change touches --
    does not block; a finding the change causes (or alters) does, and the 422
    names only those.

    :param result_fn: runs the org operation and returns the candidate org;
        a :class:`CorrectnessError` from it becomes 422 with its findings.
    :returns: the persisted org.
    :raises HTTPException: 422 ``org change would break referencing schemas``
        with the added findings of all referencing schemas.

    Edge cases: an org that is not stored yet (no stored version to compare
    with) is compared against the schema as stored -- with its empty embedded
    org, as :func:`hydrate_org` leaves it for a missing shared model.

    Concurrency: the referencing schemas are saved again unchanged in the same
    unit of work, so a schema change made while this check ran conflicts and
    the request is repeated on the new state. The org itself is saved
    conditionally as well (its own ``lock_version``): two concurrent changes
    of the same org conflict even without a referencing schema, and the
    later one is repeated on the new state.
    """

    try:
        org = result_fn()
    except CorrectnessError as exc:
        raise _findings_422(exc.findings) from exc
    if org.id is not None:
        breaking: list[ValidationFinding] = []
        referencing = _schemas_referencing(org.id)
        for schema in referencing:
            before = validate(hydrate_org(schema, _org_resolver), _resolver)
            hydrated = schema.model_copy(update={"org_model": org.model_copy(deep=True)})
            breaking += findings_added(before, validate(hydrated, _resolver))
        if breaking:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "org change would break referencing schemas",
                    "findings": [f.model_dump() for f in breaking],
                },
            )
        # The check above holds for the schemas as read. Save each of them
        # again (content unchanged) in this unit of work: a schema changed in
        # the meantime -- e.g. a staff rule on a role this change empties --
        # makes the save conflict, and the repetition checks the new state.
        # Without it each change was checked against the other's old state
        # and both were stored, leaving a step nobody may do.
        for schema in referencing:
            _store.put(schema)
    return _org_store.put(org)


def _get_instance_or_404(instance_id: str) -> ProcessInstance:
    """Load an instance or answer 404 (:class:`InstanceNotFound`).

    :param instance_id: the id named by the caller.
    :returns: the stored instance.
    :raises InstanceNotFound: no such instance (``detail`` unchanged; under
        ``/v1`` with code ``OP.not-found``).
    """

    instance = _instances.get(instance_id)
    if instance is None:
        raise InstanceNotFound(instance_id)
    return instance


def _run_or_409(result_fn: Callable[[], ProcessInstance]) -> ProcessInstance:
    """Execute a runtime operation callable; map ExecutionError to HTTP 409."""

    try:
        instance = result_fn()
    except ExecutionError as exc:
        raise HTTPException(status_code=409, detail=_execution_error_detail(exc)) from exc
    return _instances.put(instance)


def _execution_error_detail(exc: ExecutionError) -> dict[str, object]:
    """409 body of an engine refusal: the technical message plus ``code``/``params``.

    ``message`` stays unchanged for API users and tests; ``code``/``params`` let
    the web client word the refusal in German, exactly like a rule
    finding (``describeError`` -> ``findingText``).
    """

    return {"message": exc.message, "code": exc.code, "params": exc.params}


def _effective_schema_for(instance: ProcessInstance) -> ProcessSchema:
    """Return the schema an instance currently runs against.

    Ad-hoc changed instances carry their own per-instance variant
    (``ad_hoc_schema``); everything else runs against the released base schema.
    """

    base = _get_or_404(instance.schema_id)
    return hydrate_org(adhoc.effective_schema(instance, base), _org_resolver)


def _schema_memo() -> Callable[[ProcessInstance], ProcessSchema]:
    """:func:`_effective_schema_for` for a pass over many cases, each schema loaded once.

    The overview reads (worklists, monitoring, titles, unstaffed steps, the
    escalation sweep) used to load and hydrate the schema again for every
    case -- with a few hundred cases of a handful of schemas, almost all of
    their time went into reloading and deep-copying the same models. The
    returned function remembers the hydrated base schema per ``schema_id``
    for the lifetime of the function, i.e. one pass.

    Only for **read-only** passes: the cached schema is shared by every case
    of that schema, so no caller may change it. A case with an ad-hoc variant
    gets its own variant (never cached, never shared).

    :returns: ``effective(instance) -> ProcessSchema``, same result as
        :func:`_effective_schema_for` (and the same 404 for a missing schema).
    """

    hydrated: dict[str, ProcessSchema] = {}

    def effective(instance: ProcessInstance) -> ProcessSchema:
        if instance.ad_hoc_schema is not None:
            return _effective_schema_for(instance)
        schema = hydrated.get(instance.schema_id)
        if schema is None:
            schema = hydrated[instance.schema_id] = _effective_schema_for(instance)
        return schema

    return effective


def _emit_event(event_type: str, payload: dict[str, object]) -> None:
    """Enqueue a domain event on the outbox and attempt best-effort delivery.

    The webhook side of the open API (E13): emission is transactional -- the
    entry is stored in the same unit of work as the change it announces, so
    both are committed together or not at all. Delivery runs **after** the
    commit (:func:`procworks.unit_of_work.after_commit`, once per request,
    still before the answer), so no subscriber hears of a change that was
    rolled back, and a repeated unit never delivers twice. Delivery never
    blocks or fails the triggering request -- exhausted retries become
    dead-letters instead.
    """

    _outbox.emit(event_type, payload)
    unit_of_work.after_commit("webhooks", lambda: _outbox.dispatch_pending())


def _push_external(binding: ServiceBinding, payload: dict[str, object]) -> bool:
    """Deliver an ``HTTP_PUSH`` activity package to its tool endpoint (E11/E13).

    Resolves the binding's ``endpoint_ref`` to a server-configured, trusted URL
    (and optional signing secret) and delivers the package once through the
    outbox (durable entry, signature, delivery log, circuit breaker) --
    :meth:`OutboxDispatcher.push_once`. Retries are **not** the outbox's job
    here: the external task owns them (budget, back-off, incident), see
    :meth:`ExternalTaskRuntime.drive_push`.

    The delivery is bounded by the step's modelled ``request_timeout_ms``
    (``0``: the dispatcher's default); a tool that does not answer in time
    counts as a failed delivery.

    :returns: whether the tool accepted the package (2xx). ``False`` -- an error
        status, an unreachable or too slow tool -- counts as a failed attempt
        of the task.
    :raises UndeliverablePush: the configured URL can never be delivered
        (same shape check as at start, :func:`build_push_endpoint_registry`:
        scheme, host, credentials, port); the task becomes an incident naming
        the reference and the reason code, never the URL.
    :raises Exception: an unconfigured or locked-down endpoint; the task then
        stays pending without using its budget and is retried on a later
        drive. The engine is never affected.
    """

    ref = binding.endpoint_ref or ""
    target = _push_endpoints.resolve(ref)
    try:
        resolve_target(target.url, allow_internal=True, pin=False, honour_lockdown=False)
    except WebhookError as exc:
        raise UndeliverablePush(
            f"push endpoint '{ref}' can never be delivered ({exc.code})"
        ) from exc
    delivery = _outbox.push_once(
        target.url,
        target.secret_ref,
        "task.push",
        payload,
        # The step's modelled request timeout bounds the call (capped to the
        # validator's bound for a value stored before it); 0 = default.
        timeout_s=request_timeout_seconds(binding),
    )
    return delivery.ok


def _drive_pushes() -> None:
    """Best-effort push of newly activated ``HTTP_PUSH`` steps after an advance.

    Called after every engine advance (start/complete). The push itself runs
    after the unit of work has committed (:func:`_drive_pushes_now` via
    :func:`procworks.unit_of_work.after_commit`): a tool must never receive a
    step whose activation is then rolled back, and no database transaction
    may stay open while a tool is called. Outside a unit it runs at once.
    """

    unit_of_work.after_commit("push", lambda: _drive_pushes_now())


def _drive_pushes_now() -> None:
    """Drive the push sink once. It never raises: a push problem must not fail
    the triggering request nor corrupt the instance, so failures are swallowed
    and retried on the next advance."""

    try:
        _external_runtime().drive_push()
    except Exception:  # noqa: BLE001 -- the push side must never break a process
        pass


def _mail_label(entry: MailOutboxEntry) -> str | None:
    """Best-effort human label of a queued notification's node (for the audit).

    Resolves the node label from the entry's base schema if it is still present;
    a missing/renamed schema degrades to ``None`` rather than raising.
    """

    schema = _store.get(entry.schema_id)
    return _label_of(schema, entry.node_id) if schema is not None else None


def _dispatch_mail_outbox() -> None:
    """Drive the durable mail outbox once and audit the terminal outcomes (N).

    Attempts every due queued notification through the process-wide sender (read
    dynamically so an operator/test can swap it). A successful send records a
    ``mail.sent`` audit event; an exhausted retry budget records ``mail.failed``
    (:func:`_audit_mail_outcome`). Only **metadata** is logged (recipient count,
    attempts, error), never the address list or the body (data minimisation).
    Transient failures stay silent -- they will be retried on a later drive.

    The sends run outside any unit of work (no transaction is held while the
    mail server answers); each event is recorded afterwards in a unit of its
    own behind the write gate, like every other write. The delivery itself
    never raises; a database error while recording an event does (the
    after-commit step logs it, the admin trigger answers it like any request).
    """

    for result in _mail_outbox.dispatch_pending(_mail_sender):
        if result.delivered or result.dead:
            _run_as_unit(functools.partial(_audit_mail_outcome, result))


def _audit_mail_outcome(result: mail_runtime.MailDispatchResult) -> None:
    """Record the ``mail.sent``/``mail.failed`` event of one send -- if its case still exists.

    The store part of :func:`_dispatch_mail_outbox`, run as a unit of work
    that passes the write gate (:func:`_run_as_unit`). The case is read
    **inside** that unit: a reset of all data (``POST /admin/reset``) that
    ran while the mail was on its way has wiped the case together with the
    audit log, and the event would otherwise start the new log with an entry
    for a case that no longer exists. Behind the gate the order is fixed:
    either the reset waits for this unit (and wipes its event with the rest)
    or the unit runs after it and finds the case gone -- then it records
    nothing (the mail was sent all the same; the outbox entry went with the
    reset).

    :param result: the outcome of one reserved send; only a delivered or a
        dead-lettered one is recorded (others are ignored).
    """

    entry = result.entry
    instance = _instances.get(entry.instance_id)
    if instance is None:
        return
    version = instance.schema_version
    if result.delivered:
        _audit.append(
            EventType.MAIL_SENT,
            entry.instance_id,
            entry.schema_id,
            schema_version=version,
            node_id=entry.node_id,
            label=_mail_label(entry),
            detail={"recipients": str(len(entry.recipients))},
        )
    elif result.dead:
        _audit.append(
            EventType.MAIL_FAILED,
            entry.instance_id,
            entry.schema_id,
            schema_version=version,
            node_id=entry.node_id,
            label=_mail_label(entry),
            detail={
                "recipients": str(len(entry.recipients)),
                "attempts": str(entry.attempts),
                "error": (entry.last_error or "")[:200],
            },
        )


def _notify_ready(
    schema: ProcessSchema,
    before_states: dict[str, NodeState] | None,
    after: ProcessInstance,
) -> None:
    """Durably queue + deliver modelled e-mail notifications for ready tasks (N).

    Boundary side effect, called after an engine advance. ``before_states`` is
    the node marking *before* the advance (``None`` for a freshly instantiated
    instance). Each task that just became ready and carries a mail binding is
    enqueued idempotently into the durable outbox (surviving a crash), then the
    outbox is driven once the change is committed (still within the request),
    so a healthy mail server is notified promptly.
    Test instances never notify (mirrors the audit/webhook suppression); enqueue
    reads the activation stamp, so ``_stamp_activations`` must run first
    (see :func:`_after_advance`). Delivery errors are swallowed by the dispatcher,
    so this can never break the triggering request.
    """

    if after.is_test:
        return
    mail_runtime.enqueue_ready_tasks(
        schema, before_states, after, _mail_outbox, _current_absent_agents()
    )
    # Queued with the change, sent once it is committed (never for a rollback).
    unit_of_work.after_commit("mail", lambda: _dispatch_mail_outbox())


def _after_advance(
    schema: ProcessSchema,
    before_states: dict[str, NodeState] | None,
    after: ProcessInstance,
) -> None:
    """Run all boundary side effects of one engine advance, in the right order.

    Stamps the worklist activation clock **before** the mail notification (the
    durable outbox derives its per-activation idempotency key from that stamp),
    schedules the ``HTTP_PUSH`` drive and the mail dispatch for after the commit
    of the unit of work, then persists the (stamp-mutated) instance.
    Shared by every runtime-advance path -- the human mainline (start/complete),
    the external-task completion, ad-hoc changes and migration -- so a task that
    becomes ready is notified no matter which path activated it.
    """

    _stamp_activations(schema, before_states, after)
    _drive_pushes()
    _notify_ready(schema, before_states, after)
    _record_branch_decisions(after, schema)
    _instances.put(after)
    _record_subprocess_joins(schema, before_states, after)


def _branch_decision_detail(schema: ProcessSchema, record: DecisionRecord) -> dict[str, str]:
    """The ``BRANCH_DECIDED`` detail of one decision: branch, element, value, condition.

    :param schema: the schema the instance ran on when it decided.
    :param record: the engine's note of the decision.
    :returns: ``branch`` (name of the chosen branch's first step, or
        ``"leerer Zweig"`` for an empty branch that leads straight to the
        join), ``element`` (discriminator name), ``value`` (the value as JSON)
        and ``condition`` (the branch condition in words, e.g.
        ``"Betrag >= 1000"``). A decision the schema no longer describes (an
        ad-hoc variant changed since) keeps only ``branch`` and ``value``.
    """

    target = schema.nodes.get(record.target)
    empty = target is not None and target.type is NodeType.XOR_JOIN
    detail = {
        "branch": "leerer Zweig" if empty else node_name(schema, record.target),
        "value": json.dumps(record.value, ensure_ascii=False, default=str),
    }
    decision = schema.xor_decisions.get(record.node_id)
    if decision is None:
        return detail
    element = schema.data_elements.get(decision.discriminator)
    name = element.name if element is not None else decision.discriminator
    detail["element"] = name
    for index, branch in enumerate(decision.branches):
        if branch.target == record.target:
            detail["condition"] = xor_condition_text(name, decision, index)
            break
    return detail


def _record_branch_decisions(instance: ProcessInstance, schema: ProcessSchema) -> None:
    """Audit the XOR decisions the engine handed over, then empty the hand-over.

    Writes one ``BRANCH_DECIDED`` per entry of
    ``instance.unaudited_decisions`` (node = the split, detail see
    :func:`_branch_decision_detail`) and clears the list **in place**; the
    caller stores the instance afterwards. Called after the event that
    triggered the advance has been written (step completion, instance start,
    ad-hoc change, migration), so the history reads in order. A test instance
    only has its list emptied -- it records no audit, as everywhere.

    :param instance: the advanced instance (mutated: list emptied).
    :param schema: the schema it runs on (effective, ad-hoc variant included).
    """

    pending = instance.unaudited_decisions
    if not pending:
        return
    instance.unaudited_decisions = []
    if instance.is_test:
        return
    for record in pending:
        _audit.append(
            EventType.BRANCH_DECIDED,
            instance.id,
            instance.schema_id,
            schema_version=instance.schema_version,
            node_id=record.node_id,
            label=node_name(schema, record.node_id),
            detail=_branch_decision_detail(schema, record),
        )


def _record_subprocess_joins(
    schema: ProcessSchema,
    before_states: dict[str, NodeState] | None,
    after: ProcessInstance,
) -> None:
    """Audit what a finished child instance did to its parent (and beyond).

    A SUBPROCESS node completes **inside the engine**, in a different instance
    than the request advanced: the child finishes, ``_propagate_completion``
    joins it into the parent, marks the parent's node COMPLETED and may finish
    the parent in turn. The boundary never saw any of it, so two entries were
    missing from the parent's history:

    * the **completion of the SUBPROCESS step**. Without it the Soll/Ist map
      called every sub-process "never executed" and reported the transition
      *across* it as a deviation from the model -- in the Order-to-Cash demo
      two of two reported deviations were exactly this false alarm.
    * the **completion of the parent instance**, whenever the sub-process was
      its last step. The parent was COMPLETED but its log did not say so, which
      silently cost the KPIs a finished instance and the webhook subscribers
      their ``instance.completed``.

    Runs from :func:`_after_advance`, so every path is covered (human
    completion, external task, ad-hoc change, migration). It fires only for the
    advance that actually finished the instance -- an instance that was already
    COMPLETED before this advance has nothing to join -- and it stops at the
    first ancestor where the join demonstrably did not happen: a stale child of
    an earlier loop iteration (``child_instances`` no longer points at it) or a
    parent node that is still waiting.

    :param schema: schema of the advanced instance (to find its END node)
    :param before_states: its node states before the advance (``None`` on a
        fresh instance)
    :param after: the advanced instance
    """

    if after.state is not InstanceState.COMPLETED:
        return
    end_id = schema.end_node().id
    if before_states is not None and before_states.get(end_id) is NodeState.COMPLETED:
        return  # was already finished -- this advance joined nothing
    child = after
    while child.parent_instance_id and child.parent_node_id:
        parent = _instances.get(child.parent_instance_id)
        if parent is None:
            return
        node_id = child.parent_node_id
        if parent.child_instances.get(node_id) != child.id:
            return  # stale child of an earlier loop round: no join happened
        if parent.node_states.get(node_id) is not NodeState.COMPLETED:
            return  # the parent step is still open (or waiting on siblings)
        if not parent.is_test:
            parent_schema = _effective_schema_for(parent)
            ready = parent.node_activated_at.get(node_id)
            detail = {"child_instance": child.id}
            if ready is not None:
                detail["ready_at"] = ready.isoformat()
            _audit.append(
                EventType.ACTIVITY_COMPLETED,
                parent.id,
                parent.schema_id,
                schema_version=parent.schema_version,
                node_id=node_id,
                label=_label_of(parent_schema, node_id),
                detail=detail,
            )
            # The parent advanced inside the engine: its decisions behind the
            # sub-process step follow the step's completion.
            _record_branch_decisions(parent, parent_schema)
            if parent.state is InstanceState.COMPLETED:
                _audit.append(
                    EventType.INSTANCE_COMPLETED,
                    parent.id,
                    parent.schema_id,
                    schema_version=parent.schema_version,
                )
                _emit_event("instance.completed", _instance_event_payload(parent))
        else:
            parent.unaudited_decisions = []
        _instances.put(parent)
        if parent.state is not InstanceState.COMPLETED:
            return
        child = parent


def _stamp_activations(
    schema: ProcessSchema,
    before_states: dict[str, NodeState] | None,
    after: ProcessInstance,
) -> None:
    """Stamp the runtime clock of the time-based worklist prioritisation.

    Records, per human-task ACTIVITY that *just* became ready (ACTIVATED in
    ``after`` but not before), the current wall-clock into
    ``after.node_activated_at`` -- the origin from which an open task's reaction
    time is measured (time-based worklist prioritisation). On a fresh
    instance (``before_states is None``) the instance ``started_at`` is set too
    (origin of the process-deadline slack).

    This lives at the API boundary and mirrors ``mail_runtime``'s ready-node diff
    exactly, so the execution engine and the validator stay untouched (leitplanke
    L2). A node re-activated by a loop overwrites its stamp, so its clock
    restarts -- matching the loop-aware mail notification. The mutation is
    persisted by the caller. Test instances are stamped as well (harmless: they
    are not shown in operational worklists), keeping the helper branch-free.
    """

    now = datetime.now(UTC)
    if before_states is None and after.started_at is None:
        after.started_at = now
    for node_id, state in after.node_states.items():
        if state is not NodeState.ACTIVATED:
            continue
        if before_states is not None and before_states.get(node_id) is NodeState.ACTIVATED:
            continue
        node = schema.nodes.get(node_id)
        if node is None or node.type is not NodeType.ACTIVITY:
            continue
        if node_id not in schema.staff_rules:
            continue
        after.node_activated_at[node_id] = now


def _time_context(instance: ProcessInstance, schema: ProcessSchema) -> TimeContext:
    """Build the read-time clock inputs for prioritising an instance's worklist.

    Reads the current wall-clock, the per-node activation stamps and the process
    start/deadline; the derived prioritisation logic lives in
    ``worklist_priority``. Kept trivial and side-effect-free.
    """

    return TimeContext(
        now=datetime.now(UTC),
        activated_at=dict(instance.node_activated_at),
        started_at=instance.started_at,
        deadline_seconds=schema.deadline_seconds,
        claimed_at=dict(instance.node_claimed_at),
        paused_seconds=dict(instance.node_paused_seconds),
        suspended_at=dict(instance.node_suspended_at),
    )


def _current_absent_agents() -> frozenset[str]:
    """Resolve which agents are absent right now (boundary read of the store).

    Reads the absence store against the current wall-clock and returns the set of
    absent agent ids. Handed to the runtime eligibility resolution
    (:func:`procworks.assignment.eligible_agents`) so that, during an agent's
    absence window, their deputy receives the tasks *in parallel*. Because
    absence only *adds* the deputy and never removes the absent agent, an absence
    without a registered deputy can never leave a task unassigned (safety
    invariant) -- the task simply stays with the absent agent.
    """

    return assignment.absent_agent_ids(_absence_store.list_entries(), datetime.now(UTC))


def _external_runtime() -> ExternalTaskRuntime:
    """Build the external-task boundary driver over the module singletons.

    Stateless wiring around the shared stores and engine context, so a fresh
    instance per request is fine and keeps the runtime free of global state.
    """

    return ExternalTaskRuntime(
        _external_tasks,
        _instances,
        _effective_schema_for,
        _context,
        dal=_connections.data_access_layer(),
        on_event=_emit_event,
        on_push=_push_external,
        on_advance=_after_advance,
        on_complete=_record_external_completion,
    )


def _run_external(action: Callable[[], object]) -> object:
    """Execute an external-task action, mapping ExternalTaskError to HTTP.

    The answer carries ``message`` and, when the runtime names one, the reason
    ``code`` (e.g. ``DAL.no-row``, or the engine's ``EX.*`` code) plus its
    ``params``, so a worker can tell a missing target row from a connector
    outage without parsing text, and the web client words it from its
    catalogue. An error without code keeps the plain ``{"message": ...}``
    answer existing integrations rely on.
    """

    try:
        return action()
    except ExternalTaskError as err:
        raise _external_http(err) from err


def _external_http(err: ExternalTaskError) -> HTTPException:
    """The HTTP answer for an :class:`ExternalTaskError` (see :func:`_run_external`).

    :param err: the runtime's refusal.
    :returns: an :class:`HTTPException` with ``err.status`` and ``detail``
        ``{"message"}``, plus ``code`` (and ``params``, if any) when the
        runtime names a reason -- to be raised by the caller.
    """

    detail: dict[str, object] = {"message": err.message}
    if err.code is not None:
        detail["code"] = err.code
        if err.params is not None:
            detail["params"] = err.params
    return HTTPException(status_code=err.status, detail=detail)



def _commit_instance_or_422(result_fn: Callable[[], ProcessInstance]) -> ProcessInstance:
    """Execute an instance change op; map CorrectnessError to HTTP 422."""

    try:
        instance = result_fn()
    except CorrectnessError as exc:
        raise _findings_422(exc.findings) from exc
    return _instances.put(instance)



# --- endpoints -----------------------------------------------------------
# (health, auth and users stay here; the others: procworks/api_admin.py, procworks/api_schemas.py)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process answers. Deliberately without a database check --
    a container orchestrator restarts a container whose liveness fails, and a
    restart does not bring a database back (readiness is :func:`ready`)."""

    return {"status": "ok", "version": __version__}


def _database_engines() -> list[Engine]:
    """Every distinct database engine the API currently uses (one per URL).

    The stores are module singletons (``_store``, ``_instances``, the auth
    backend's credential store, the outbox's store …); each SQLAlchemy store
    keeps its engine in ``_engine``. Looked up at call time, so a store a
    test swaps in is seen. In-memory stores have none.

    :returns: one engine per database URL, in no particular order.
    """

    engines: dict[str, Engine] = {}
    for value in list(globals().values()):
        for holder in (value, getattr(value, "store", None), getattr(value, "_store", None)):
            engine = getattr(holder, "_engine", None)
            if isinstance(engine, Engine):
                engines.setdefault(str(engine.url), engine)
    return list(engines.values())


#: Seconds ``/ready`` waits for the database ping before it answers 503. A
#: frozen database server (or a network that drops packets) answers neither
#: the ping nor a cancel -- no server-side limit ends that wait, so the
#: readiness probe would hang with it instead of reporting the outage.
READY_TIMEOUT_SECONDS = 5.0

#: The ping thread of a ``/ready`` that has not returned yet (``None`` when
#: none is pending). While it is still running, further probes answer 503 at
#: once instead of starting another thread that would hang the same way.
#: A ping that ends on its own within the deadline (a new connection gives up
#: after the connect timeout of 5 s) leaves no thread behind -- the next
#: probe pings again and takes up to :data:`READY_TIMEOUT_SECONDS` itself.
_ready_probe: threading.Thread | None = None
_ready_probe_lock = threading.Lock()


def _ping_databases(outcome: list[BaseException | None]) -> None:
    """Ping every database once (the work of one ``/ready``, in its thread).

    :param outcome: receives ``None`` on success or the error raised.
    """

    try:
        for engine in _database_engines():
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        outcome.append(None)
    except BaseException as exc:  # noqa: BLE001 -- handed to the request thread
        outcome.append(exc)


@app.get("/ready", response_model=None)
def ready() -> dict[str, str] | JSONResponse:
    """Readiness: the API can serve requests, its database included.

    Pings every database (:func:`_database_engines`) with ``SELECT 1``, in a
    thread of its own, and waits at most :data:`READY_TIMEOUT_SECONDS`. Meant
    for a load balancer or an orchestrator's readiness probe: while it fails,
    no traffic should be sent, but the process must not be restarted (that is
    :func:`health`). No login needed, like ``/health``; the answer names
    nothing about the database.

    A ping that does not return in time (frozen server, lost network) is
    answered with 503 and left to finish in the background; while it still
    runs, every further probe answers 503 at once, so hanging pings never
    pile up. A ping that ends by itself -- a new connection gives up after
    the connect timeout, a pooled one after the stall limit of the database
    layer -- is not pending any more: the next probe pings anew and answers
    within :data:`READY_TIMEOUT_SECONDS` as well. Either way no probe takes
    longer than that.

    :returns: ``{"status": "ready"}`` (also without any database, in-memory
        mode), or the 503 ``DB.unavailable`` of :func:`_database_unavailable`.
    """

    global _ready_probe
    log = logging.getLogger("procworks.api")
    with _ready_probe_lock:
        if _ready_probe is not None and _ready_probe.is_alive():
            log.warning("readiness: the previous database ping has not returned yet")
            return _database_unavailable()
        outcome: list[BaseException | None] = []
        probe = threading.Thread(
            target=_ping_databases, args=(outcome,), name="ready-probe", daemon=True
        )
        _ready_probe = probe
        probe.start()
    probe.join(READY_TIMEOUT_SECONDS)
    if probe.is_alive():
        log.warning("readiness: no answer from the database within %s s", READY_TIMEOUT_SECONDS)
        return _database_unavailable()
    error = outcome[0] if outcome else None
    if isinstance(error, (OperationalError, InterfaceError, PoolTimeoutError)):
        log.warning(
            "readiness: database not reachable (%s)",
            type(getattr(error, "orig", None) or error).__name__,
        )
        return _database_unavailable()
    if error is not None:
        raise error
    return {"status": "ready"}


@app.get("/auth/me", response_model=Principal)
def get_me(
    principal: Principal = Depends(get_principal_pending_password_change),
) -> Principal:
    """Return the verified identity of the caller (for the client's login UI).

    Reachable with a pending password change: ``must_change`` in the answer
    tells the client to show the forced change dialog instead of the app.
    """

    return principal


def _business_role_label(
    org: OrgModel | None, agent_id: str | None, fallback: str
) -> str:
    """Human label for a demo login's *business* role (not its RBAC role).

    The RBAC role of every Order-to-Cash login is ``operator``, which would make
    the demo's role-switch box read "Bearbeiter" eight times over and tell a
    visitor nothing. What they actually want to pick is the **role in the
    process** -- Debitorenbuchhaltung, Lager/Versand, Kreditmanagement -- so the
    label is resolved from the seeded organisation instead.

    An agent carrying more than two roles is the deliberate "walk the whole
    process under one login" persona (Sina Springer holds every operational
    role); listing six names would overflow the box, so it collapses to a short
    hint. Falls back to ``fallback`` (the RBAC role) whenever the organisation
    is not resolvable -- the label is a convenience, never a correctness input.
    """

    if org is None or agent_id is None:
        return fallback
    agent = org.agents.get(agent_id)
    if agent is None or not agent.role_ids:
        return fallback
    if len(agent.role_ids) > 2:
        return "alle Rollen"
    names = [org.roles[rid].name for rid in agent.role_ids if rid in org.roles]
    return "/".join(names) if names else fallback


def _demo_logins() -> list[DemoLogin]:
    """The demo logins advertised on ``/auth/config`` (demo mode only).

    Always the base demo cosmos; the Order-to-Cash logins are added **only when
    that data set is part of this deployment** (``PROCWORKS_LOAD_O2C``, the same
    boot switch :func:`_lifespan` seeds it with). Advertising a login that was
    never seeded would offer a visitor credentials that cannot work, so the two
    are deliberately tied to one switch.
    """

    from procworks.demo import DEMO_USERS
    from procworks.demo import ORG_ID as DEMO_ORG_ID

    # Same reasoning as for the Order-to-Cash logins below: three of the base
    # logins are RBAC ``operator``, so the box would read "Bearbeiter" three
    # times and hide what actually distinguishes them (Sachbearbeiter,
    # Teamleitung, Einkauf). Logins without an agent (modeller, viewer) keep
    # their RBAC role as the fallback.
    base_org = _org_store.get(DEMO_ORG_ID)
    logins = [
        DemoLogin(
            login=login,
            name=name,
            role=_business_role_label(base_org, agent, next(iter(roles), "viewer")),
        )
        for login, name, roles, agent in DEMO_USERS
    ]
    if not _env_truthy("PROCWORKS_LOAD_O2C"):
        return logins

    from procworks.demo_o2c import O2C_USERS
    from procworks.demo_o2c import ORG_ID as O2C_ORG_ID

    org = _org_store.get(O2C_ORG_ID)
    logins += [
        DemoLogin(
            login=login,
            name=name,
            role=_business_role_label(org, agent_id, next(iter(roles), "viewer")),
        )
        for login, name, roles, agent_id in O2C_USERS
    ]
    return logins


@app.get("/auth/config", response_model=AuthConfig)
def get_auth_config() -> AuthConfig:
    """Public: tell the client which login UI to render (open/token/password).

    In a public demo (``PROCWORKS_DEMO_MODE`` **and** password login) this also
    advertises the seeded demo logins + their shared password and names the
    login to auto-authenticate, so a fresh visitor lands in the editor without
    guessing credentials. Outside demo mode all demo fields stay empty -- a
    regular deployment never exposes any password here.
    """
    mode = _auth_mode()
    cfg = AuthConfig(mode=mode, password_login=mode == "password")
    if mode == "jwt":
        # OIDC redirect login (opt-in): only when all
        # three endpoints/ids are configured does the SPA offer the
        # "Über Firmenkonto anmelden" flow; otherwise the token field stays
        # the (documented) default. Values are public client metadata.
        authorize = os.environ.get("PROCWORKS_JWT_AUTHORIZE_URL", "").strip()
        token_url = os.environ.get("PROCWORKS_JWT_TOKEN_URL", "").strip()
        client_id = os.environ.get("PROCWORKS_JWT_CLIENT_ID", "").strip()
        if authorize and token_url and client_id:
            cfg.oidc_authorize_url = authorize
            cfg.oidc_token_url = token_url
            cfg.oidc_client_id = client_id
            cfg.oidc_scopes = (
                os.environ.get("PROCWORKS_JWT_OIDC_SCOPES", "").strip()
                or "openid profile email"
            )
    if mode == "password" and _demo_mode():
        from procworks.demo import DEMO_AUTOLOGIN, DEMO_PASSWORD

        cfg.demo = True
        cfg.demo_password = DEMO_PASSWORD
        cfg.demo_autologin = DEMO_AUTOLOGIN
        cfg.demo_logins = _demo_logins()
        # Broker survey endpoint for the "end demo & give feedback" flow (unset ->
        # the SPA simply shows no survey). Not a secret; the broker gates + relays.
        cfg.demo_feedback_url = os.environ.get("PROCWORKS_DEMO_FEEDBACK_URL", "").strip() or None
    return cfg


#: Brute-force brake for ``POST /auth/login``; see :class:`LoginThrottle`.
_login_throttle = LoginThrottle()


class TrustedProxies:
    """The reverse proxies whose ``X-Forwarded-For`` header the API believes.

    Configured with ``PROCWORKS_TRUSTED_PROXIES`` (read once at start,
    :meth:`from_env`): comma-separated IP addresses and networks
    (``172.18.0.5``, ``172.16.0.0/12``, ``fd00::/8``), the word ``private``
    for loopback and private networks, or ``none`` (believe nobody, the TCP
    peer is the client). **Default (unset or empty): ``private``** -- the
    bundled Caddy talks to the API over the internal Compose/Kubernetes
    network, and the API port is not published, so only containers of the
    stack reach it. Where the API port is reachable from an office network
    without the proxy, name the proxy's address instead: otherwise any host
    there could claim another address and slip past the per-address lock of
    the login throttle (the per-login lock holds regardless).
    """

    def __init__(self, networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
                 *, private: bool) -> None:
        """Create the set.

        :param networks: the networks (a single address is a /32 or /128).
        :param private: whether loopback and private addresses count as well.
        """

        self._networks = tuple(networks)
        self._private = private

    @classmethod
    def parse(cls, raw: str | None) -> TrustedProxies:
        """Read the setting.

        :param raw: the value of ``PROCWORKS_TRUSTED_PROXIES`` (``None`` or
            blank = the default ``private``).
        :returns: the set.
        :raises ValueError: an entry is neither an address, a network,
            ``private`` nor ``none``, an entry is empty, or ``none`` is combined
            with others -- the message names the variable and the entry, so a
            typo stops the start instead of silently trusting nobody (or
            everybody).
        """

        text = (raw or "").strip()
        if not text:
            return cls((), private=True)
        entries = [entry.strip() for entry in text.split(",")]
        if entries == ["none"]:
            return cls((), private=False)
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        private = False
        for entry in entries:
            if entry == "private":
                private = True
                continue
            try:
                if not entry or entry == "none":
                    raise ValueError(entry)
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                raise ValueError(
                    f"PROCWORKS_TRUSTED_PROXIES: {entry!r} is no IP address or network "
                    "(allowed: addresses, networks such as 172.16.0.0/12, 'private', "
                    "or 'none' alone)"
                ) from None
        return cls(networks, private=private)

    @classmethod
    def from_env(cls) -> TrustedProxies:
        """The set configured in ``PROCWORKS_TRUSTED_PROXIES`` (see :meth:`parse`)."""

        return cls.parse(os.environ.get("PROCWORKS_TRUSTED_PROXIES"))

    def trusts(self, address: str) -> bool:
        """Whether ``address`` is one of the trusted proxies.

        :param address: an IP address as text; anything else is not trusted.
        :returns: ``True`` for a configured address/network, or -- with
            ``private`` -- a loopback or private address.
        """

        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if self._private and (ip.is_private or ip.is_loopback):
            return True
        return any(ip in network for network in self._networks)


#: Read once at start; a wrong value stops the start (``ValueError``).
_TRUSTED_PROXIES = TrustedProxies.from_env()


def _client_address(request: Request) -> str | None:
    """The client address for the login throttle.

    Behind the bundled Caddy the TCP peer is the proxy, so its address would
    lump every user together. Caddy *replaces* ``X-Forwarded-For`` with the
    address it saw (it ignores a client-sent value unless its own
    ``trusted_proxies`` is configured). The header is therefore believed
    **only** when the peer is a trusted proxy (:data:`_TRUSTED_PROXIES`).
    Then the entries are read from the right -- each one was appended by the
    proxy in front of it -- skipping those that are trusted proxies
    themselves; the first other one is the client. If every entry is trusted
    (a client inside the trusted network), the left-most is the client.

    Limit: the API learns no more than the first proxy saw. Under Docker
    Desktop the host's port forwarding already replaces the client's address
    with the network gateway, so every client arrives as that address.

    :param request: the request (``client.host`` and the header are read).
    :returns: the client address; the peer when it is not a trusted proxy,
        the header is missing, or the deciding entry is no IP address;
        ``None`` without a peer.
    """

    peer = request.client.host if request.client else None
    if peer is None or not _TRUSTED_PROXIES.trusts(peer):
        return peer
    hops = [hop.strip() for hop in request.headers.get("X-Forwarded-For", "").split(",")]
    hops = [hop for hop in hops if hop]
    if not hops:
        return peer
    for hop in reversed(hops):
        if _TRUSTED_PROXIES.trusts(hop):
            continue
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer  # garbled entry: not taken as an address
        return hop
    return hops[0]


@app.post("/auth/login", response_model=LoginResponse)
@manages_own_units  # the key derivation runs before the unit (_run_as_unit)
def post_login(req: LoginRequest, request: Request) -> LoginResponse:
    """Exchange username + password for a session bearer token (password mode).

    Guarded by :data:`_login_throttle`: after repeated failures per
    login or per client address the endpoint answers 429 with ``Retry-After``
    -- *before* the password is even checked, so a locked key gives no oracle.

    The password check (one key derivation, the slow part) runs outside any
    unit of work, so simultaneous logins verify in parallel and no other
    writer waits for them; only opening the session is a unit, and it
    refuses a password reset or changed in between
    (:meth:`~procworks.auth_password.PasswordAuthBackend.open_session`).
    """

    backend = _password_backend()
    address = _client_address(request)
    wait = _login_throttle.retry_after(req.login, address)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=(
                "Zu viele fehlgeschlagene Anmeldeversuche. Bitte in "
                f"{wait} Sekunden erneut versuchen."
            ),
            headers={"Retry-After": str(wait)},
        )
    try:
        verified = backend.verify_login(req.login, req.password)
        result = _run_as_unit(lambda: backend.open_session(verified))
    except AuthError as exc:
        _login_throttle.failure(req.login, address)
        raise HTTPException(
            status_code=401, detail=exc.message, headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    _login_throttle.success(req.login)
    return LoginResponse(
        token=result.token,
        principal=result.principal,
        must_change=result.must_change,
    )


@app.post("/auth/logout", status_code=204)
def post_logout(request: Request) -> Response:
    """Invalidate the caller's current session token (password mode)."""

    backend = _password_backend()
    backend.logout(request.headers.get("Authorization"))
    return Response(status_code=204)


@app.post("/auth/change-password", status_code=204)
@manages_own_units  # the key derivations run before the unit (_run_as_unit)
def post_change_password(
    req: ChangePasswordRequest,
    principal: Principal = Depends(get_principal_pending_password_change),
) -> Response:
    """Self-service password change; clears the forced-change flag.

    Reachable with a pending password change (it is the way out of it); a
    change refused by the policy keeps the flag and thus the lock.

    Two different refusals share the status 401 here and must stay apart:

    * the session token no longer counts (expired, logged out, reset) -- the
      dependency refuses before this body runs, with the plain ``detail``
      every other endpoint gives (``invalid session`` ...);
    * the session is valid but the *current password* is wrong -- an input
      error, answered with the coded detail ``AUTH.wrong-current-password``
      (``message`` keeps the technical base ``invalid credentials``).

    The web client tells them apart by that code and never by English text:
    with the code it marks the field, without it it shows the login mask. The
    code reveals nothing about other logins -- the caller is already
    authenticated as the login whose password it names; the login endpoint
    keeps its single uniform refusal.

    Checking the current password and hashing the new one (two key
    derivations) run outside any unit of work; only storing the change is a
    unit, and it refuses a password that was reset or changed in between
    (:meth:`~procworks.auth_password.PasswordAuthBackend.apply_password_change`,
    then the same 401 as a wrong current password).

    :param req: current and new password.
    :param principal: the authenticated caller (may have a pending change).
    :returns: 204 on success.
    :raises HTTPException: 401 ``AUTH.wrong-current-password``; 400 with the
        policy code (``PW.too-short``, ``PW.unchanged``).
    """

    backend = _password_backend()
    try:
        change = backend.prepare_password_change(
            principal.subject, req.current_password, req.new_password
        )
        _run_as_unit(lambda: backend.apply_password_change(change))
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "message": exc.message,
                "code": "AUTH.wrong-current-password",
                "params": {},
            },
        ) from exc
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=_policy_detail(exc)) from exc
    return Response(status_code=204)


def _policy_detail(exc: PasswordPolicyError) -> dict[str, object]:
    """400 body of a password/login policy refusal: message, code, params.

    Same shape as the other coded boundary refusals (``USERS.delete-self`` ...),
    so the web client words it through its one message catalogue and names the
    concrete reason (too short, unchanged, login taken, unknown role).
    """

    return {"message": str(exc), "code": exc.code, "params": exc.params}


@app.get("/users", response_model=list[UserView], dependencies=[_admin])
def list_users() -> list[UserView]:
    """List login users (admin only); never exposes password hashes."""

    backend = _password_backend()
    return [user_view(u) for u in backend.store.list_users()]


@app.post("/users", response_model=CreateUserResponse, status_code=201, dependencies=[_admin])
@manages_own_units  # the key derivation runs before the unit (_run_as_unit)
def create_user(req: CreateUserRequest) -> CreateUserResponse:
    """Provision a login from an agent; returns the initial password once (admin).

    Refused with 400 and a code, nothing stored: no role (``USERS.no-role``),
    unknown role, an ``agent_id`` no organisation knows
    (``USERS.unknown-agent``), a person who already has a login
    (``USERS.agent-taken``, one login per person), a taken login name. A
    login without ``agent_id`` (administrator, modeller) stays possible.

    The checks run first (:func:`_checked_new_user`), so a request refused
    anyway costs no key derivation; then the initial password and its hash
    are made, outside any unit of work (the key derivation must not hold up
    the other writers); then the checks run again and the login is stored
    as one unit (:func:`_create_user_unit`) -- the name or the person may
    have been taken in between.
    """

    _password_backend()  # 404 before any key derivation when password login is off
    _checked_new_user(req)
    initial = new_initial_password()
    return _run_as_unit(lambda: _create_user_unit(req, initial))


class _NewUserNames(NamedTuple):
    """What :func:`_checked_new_user` resolved for ``POST /users``.

    :ivar subject: the stable identity of the new login.
    :ivar display_name: the name shown in the client (the request's, or the
        person's name), or ``None``.
    :ivar agent_name: the person's name as the organisation knows it, or
        ``None`` without ``agent_id``.
    """

    subject: str
    display_name: str | None
    agent_name: str | None


def _checked_new_user(req: CreateUserRequest) -> _NewUserNames:
    """Every refusal of ``POST /users`` that needs no key derivation.

    The person must be known to an organisation (``USERS.unknown-agent``), a
    subject must be derivable, and the login rules of the backend must hold
    (:meth:`~procworks.auth_password.PasswordAuthBackend.check_new_user`:
    role, one login per person, free name). Reads only, stores nothing.
    Called before the key derivation (a cheap early answer) and again in
    the store part (:func:`_create_user_unit`), where it decides.

    :param req: the request as received.
    :returns: subject, display name and the person's name.
    :raises HTTPException: 400 with the codes listed at :func:`create_user`
        (``USERS.agent-taken`` names the person by name, as the admin knows
        it).
    """

    backend = _password_backend()
    agent_name: str | None = None
    if req.agent_id is not None:
        # The person must exist: a login bound to nobody known would work
        # with an empty worklist and no hint why.
        agent_name = _find_agent_name(req.agent_id)
        if agent_name is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"no organisation knows the person '{req.agent_id}'",
                    "code": "USERS.unknown-agent",
                    "params": {"agent": req.agent_id},
                },
            )
    display_name = req.display_name
    if display_name is None:
        display_name = agent_name
    subject = req.login or display_name or req.agent_id
    if not subject:
        raise HTTPException(
            status_code=400, detail="need login, display_name or agent_id"
        )
    try:
        backend.check_new_user(
            subject=subject,
            roles=req.roles,
            agent_id=req.agent_id,
            login=req.login,
            display_name=display_name,
        )
    except PasswordPolicyError as exc:
        raise _user_policy_refusal(exc, agent_name) from exc
    return _NewUserNames(subject, display_name, agent_name)


def _user_policy_refusal(exc: PasswordPolicyError, agent_name: str | None) -> HTTPException:
    """The 400 of a login rule refused for ``POST /users``.

    :param exc: the backend's refusal.
    :param agent_name: the person's name, or ``None``; a ``USERS.agent-taken``
        then names the person by it instead of the internal id.
    :returns: the exception to raise.
    """

    if exc.code == "USERS.agent-taken" and agent_name:
        exc.params["agent"] = agent_name
    return HTTPException(status_code=400, detail=_policy_detail(exc))


def _create_user_unit(req: CreateUserRequest, initial: InitialPassword) -> CreateUserResponse:
    """The store part of ``POST /users``: check the request again, store the login.

    :param req: the request as received.
    :param initial: the initial password with its hash, made beforehand.
    :returns: the created login and its initial password (shown once).
    :raises HTTPException: 400 with the codes listed at :func:`create_user`
        -- also for a name or person taken since the early check.
    """

    backend = _password_backend()
    subject, display_name, agent_name = _checked_new_user(req)
    try:
        user, initial_password = backend.create_user(
            subject=subject,
            roles=req.roles,
            agent_id=req.agent_id,
            login=req.login,
            display_name=display_name,
            initial=initial,
        )
    except PasswordPolicyError as exc:
        raise _user_policy_refusal(exc, agent_name) from exc
    return CreateUserResponse(
        user=user_view(user),
        login=user.login,
        initial_password=initial_password,
    )


@app.post(
    "/users/{login}/reset-password",
    response_model=ResetPasswordResponse,
    dependencies=[_admin],
)
@manages_own_units  # the key derivation runs before the unit (_run_as_unit)
def reset_user_password(login: str) -> ResetPasswordResponse:
    """Set a fresh initial password (forces change); returns it once (admin).

    An unknown login is refused first (404, no key derivation). Then the new
    password and its hash are made, outside any unit of work; storing it
    (and ending the login's sessions) is one unit, which reads the login
    again under its lock. A login deleted in between stays deleted: 404.

    :param login: the login to reset.
    :returns: the login and its new initial password (shown once).
    :raises HTTPException: 404 for an unknown login.
    """

    backend = _password_backend()
    if backend.store.get_user(login) is None:
        raise HTTPException(status_code=404, detail="user not found")
    initial = new_initial_password()
    try:
        initial_password = _run_as_unit(lambda: backend.reset_password(login, initial=initial))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
    return ResetPasswordResponse(login=login, initial_password=initial_password)


# Serialises "is this the last admin?" with the deletion itself. Without it two
# admins deleting each other at the same moment would both see the other one
# still present and both succeed, leaving no admin. Two locks, taken in this
# order (always the same, so they never deadlock):
#
# 1. this process lock, around check and deletion -- enough in memory, where
#    one process runs its units one after another;
# 2. the store's admin-set lock (CredentialStore.lock_admin_changes): on a
#    database it is held by the database until the request's unit of work
#    commits. The deletion only becomes visible with that commit, so a lock
#    that ended with the deletion let a second request of the same or another
#    API process read the deleted admin as still present.
_user_delete_lock = threading.Lock()


@app.delete("/users/{login}", status_code=204, dependencies=[_admin])
def delete_user(login: str, principal: Principal = Depends(get_principal)) -> Response:
    """Remove a login user (admin only) and end its sessions.

    Two deletions are refused with 409, because afterwards nobody could repair
    the result through the application:

    * ``USERS.delete-self`` -- the caller's own login (the admin would lock
      themselves out mid-session);
    * ``USERS.last-admin`` -- the last login holding the ``admin`` role (no
      one could provision or reset logins any more).

    An unknown ``login`` stays a silent 204 (idempotent delete, unchanged API).

    :param login: login to remove.
    :param principal: the calling admin.
    :raises HTTPException: 409 with ``code``/``params`` in ``detail``.
    """

    backend = _password_backend()
    with _user_delete_lock:
        backend.store.lock_admin_changes()
        _delete_user_checked(backend, login, principal)
    backend.revoke_sessions(login)  # sessions persist across restarts -- end them
    return Response(status_code=204)


def _delete_user_checked(
    backend: PasswordAuthBackend, login: str, principal: Principal
) -> None:
    """Refuse self/last-admin deletion, otherwise delete.

    The caller holds both admin locks (see :data:`_user_delete_lock`), so no
    other deletion of an admin can commit between the check and this one's
    commit.

    The login itself is read under its own lock
    (:meth:`~procworks.auth_password.CredentialStore.lock_user`, after the
    admin-set lock -- always in this order): a login of the same account
    running at the same moment either committed its session before (the
    caller ends it with ``revoke_sessions`` in the same unit) or waits and
    then finds the login gone.

    :raises HTTPException: 409 ``USERS.delete-self`` / ``USERS.last-admin``.
    """

    target = backend.store.lock_user(login)
    if target is not None and login == principal.subject:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "cannot delete your own login",
                "code": "USERS.delete-self",
                "params": {"login": login},
            },
        )
    if target is not None and "admin" in target.roles:
        other_admins = [
            u for u in backend.store.list_users()
            if u.login != login and "admin" in u.roles
        ]
        if not other_admins:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "cannot delete the last admin login",
                    "code": "USERS.last-admin",
                    "params": {"login": login},
                },
            )
    backend.store.delete_user(login)


def _wipe_users(keep_logins: set[str]) -> None:
    """Delete every login except the ones in ``keep_logins`` (password mode).

    The acting admin (and any explicitly kept admin) survive a reset so the
    operator stays logged in and the system remains administrable. If none of
    the kept logins is an admin (the acting admin was deleted concurrently, or
    acts through an identity without a stored login), every admin login is
    kept as well -- a reset never leaves the system without an administrator.
    In open/token mode there is no credential store, so there is nothing to
    wipe.

    :param keep_logins: logins that must survive.
    """

    if not isinstance(_auth_backend, PasswordAuthBackend):
        return
    # Same lock as DELETE /users: a concurrent deletion of the acting admin
    # must not interleave with the wipe and leave no admin behind.
    with _user_delete_lock:
        # Held by the database until the reset commits (see _user_delete_lock).
        _auth_backend.store.lock_admin_changes()
        users = _auth_backend.store.list_users()
        # Re-checked under the lock: if no kept login is (still) an admin --
        # e.g. the acting admin was deleted meanwhile -- admin logins survive
        # the wipe, so the system never ends up without an administrator.
        kept_admin = any(u.login in keep_logins and "admin" in u.roles for u in users)
        for user in users:
            if user.login in keep_logins or (not kept_admin and "admin" in user.roles):
                continue
            _auth_backend.store.delete_user(user.login)
            _auth_backend.revoke_sessions(user.login)


def _user_count() -> int:
    if isinstance(_auth_backend, PasswordAuthBackend):
        return len(_auth_backend.store.list_users())
    return 0


class RunNowResponse(BaseModel):
    """Result of asking the backup scheduler to run now."""

    requested: bool = Field(description="True when the .run-now marker was written")


class MailOutboxEntryView(BaseModel):
    """Read-only, data-minimised projection of a queued notification (admin).

    Deliberately omits the recipient address list and the rendered body (data
    minimisation): the ops view needs the delivery *state*, not
    the personal content. The recipient *count* and the modeller-authored subject
    are kept because they identify the notification without leaking a distribution
    list. ``node_label`` is a convenience lookup and may be ``None``.
    """

    id: str
    instance_id: str
    node_id: str
    node_label: str | None = None
    schema_id: str
    state: MailOutboxState
    attempts: int
    max_attempts: int
    recipient_count: int
    subject: str
    last_error: str | None = None
    created_at: float
    next_attempt_at: float


class MailOutboxStatus(BaseModel):
    """The durable mail outbox at a glance: per-state counts plus the entries."""

    configured: bool = Field(
        description="True when a real SMTP sender is active (else a no-op sender)."
    )
    total: int
    pending: int
    failed: int
    dead: int
    sent: int
    #: Dropped because no mail server is configured (final, never sent).
    dropped: int = 0
    entries: list[MailOutboxEntryView]


def _mail_outbox_view(entry: MailOutboxEntry) -> MailOutboxEntryView:
    """Project a stored outbox entry to its data-minimised admin view."""

    return MailOutboxEntryView(
        id=entry.id,
        instance_id=entry.instance_id,
        node_id=entry.node_id,
        node_label=_mail_label(entry),
        schema_id=entry.schema_id,
        state=entry.state,
        attempts=entry.attempts,
        max_attempts=entry.max_attempts,
        recipient_count=len(entry.recipients),
        subject=entry.subject,
        last_error=entry.last_error,
        created_at=entry.created_at,
        next_attempt_at=entry.next_attempt_at,
    )


def _mail_outbox_status() -> MailOutboxStatus:
    """Assemble the current mail-outbox status (newest entry first)."""

    entries = sorted(
        _mail_outbox_store.list_entries(), key=lambda e: e.created_at, reverse=True
    )
    counts: dict[MailOutboxState, int] = {state: 0 for state in MailOutboxState}
    for entry in entries:
        counts[entry.state] += 1
    return MailOutboxStatus(
        configured=isinstance(_mail_sender, mail_runtime.SmtpMailSender),
        total=len(entries),
        pending=counts[MailOutboxState.PENDING],
        failed=counts[MailOutboxState.FAILED],
        dead=counts[MailOutboxState.DEAD],
        sent=counts[MailOutboxState.SENT],
        dropped=counts[MailOutboxState.DROPPED],
        entries=[_mail_outbox_view(entry) for entry in entries],
    )


# --- shared, cross-schema organisation models ---------------------------
# (endpoints: procworks/api_org.py)


class DirectoryAgent(BaseModel):
    """One agent of the cross-model people directory (display data only).

    Carries where the entry comes from so a caller can tell a shared org model
    (``org_model_id``) from a model-local one (``schema_id``). No correctness
    rule reads this -- staffing is always resolved against the org model of the
    *schema in question*.
    """

    agent_id: str
    name: str
    email: str | None = None
    deputy_id: str | None = None
    org_model_id: str | None = None
    schema_id: str | None = None


# --- Licensing / agent metering (dormant unless enforced) ------------------
#
# All of these endpoints exist unconditionally. While licensing is off
# (``_license.enforced == False``) they still answer, reporting the free
# contingent, so the web client can render the agent page uniformly. Only when a
# licensor public key is configured do the mutating actions actually gate work.
#
# (endpoints: procworks/api_licensing.py, procworks/api_modelling.py)


class ActivateLicenseRequest(BaseModel):
    """A signed license token (base64-of-JSON or raw JSON) to install."""

    token: str


class CheckoutRequest(BaseModel):
    """Requested pack size / duration for a purchase (with sensible defaults)."""

    slots: int = 5
    months: int = 12


class CheckoutResponse(BaseModel):
    """Where to complete the (online) purchase, plus the install id to bind to.

    ``claim_token``/``poll_url`` are only present when online auto-pull is
    configured (``PROCWORKS_LICENSE_CLAIM_URL`` set) and licensing is enforced;
    the web client then polls until the pack is issued and activates it
    automatically. Absent -> the operator uses the manual copy-&-paste flow.
    """

    checkout_url: str | None
    install_id: str
    message: str
    claim_token: str | None = None
    poll_url: str | None = None


class ClaimPollResult(BaseModel):
    """Outcome of one auto-pull poll pass over the open claims."""

    activated: int  # packs activated in this pass
    pending: int  # claims still awaiting fulfilment
    summary: SlotSummary


class BindLicenseRequest(BaseModel):
    """Re-home an agent onto another license contingent."""

    license_id: str


class RefreshTimeRequest(BaseModel):
    """Optional trusted timestamp (epoch seconds) from a signed time source.

    Must be a finite number: ``NaN``/``Infinity`` would be anchored into the
    hash-chained audit and the time ratchet as a "trusted" lower bound, where
    no comparison with it ever holds. Refused by the model itself (422,
    ``finite_number``), before the licensing layer sees it.
    """

    trusted_now: float | None = Field(default=None, allow_inf_nan=False)


# --- execution endpoints -------------------------------------------------
# (endpoints: procworks/api_execution.py)


def _reads_only_own_instances(principal: Principal) -> bool:
    """Is this caller limited to the instances it is involved in?

    An operator reads only the instances it takes part in -- data values and
    audit included; a colleague's leave request is none of its business.
    Limited is a **personal** login whose only role is ``operator``.
    Not limited: ``viewer`` (the read-only role, i.e. management/revision),
    ``modeler``/``admin``, and machine identities -- open dev mode, static
    tokens, the ``integration`` role -- which keep the documented integration
    path.
    """

    if _auth_mode() in ("open", "token") or INTEGRATION in principal.roles:
        return False
    return not principal.roles & {"viewer", "modeler", "admin"}


def _involved_instance_ids(principal: Principal) -> set[str]:
    """Instance ids a limited caller may read (see :func:`_reads_only_own_instances`).

    Involved is the bound agent when it
    * appears as the acting agent of any audit event of the instance (started
      it, claimed, completed, returned ... a step), or
    * may work -- or has claimed -- an open step right now (eligible agents
      exactly as the worklist resolves them: deputies of absent colleagues and
      fired escalation stages included).
    An unbound personal operator is involved in nothing.

    Collective reads only: the cases the agent acted in come from one audit
    query (:meth:`~procworks.audit.AuditLog.instance_ids_for_agent`), the
    running cases from one store read with each schema loaded once
    (:func:`_schema_memo`). It used to build the whole history and load every
    running case separately -- on every list request of such a login.
    """

    agent = principal.agent_id
    if agent is None:
        return set()
    ids = _audit.instance_ids_for_agent(agent)
    absent = _current_absent_agents()
    effective = _schema_memo()
    for instance in _instances.list_instances(running_only=True):
        if instance.id in ids or instance.is_test:  # a test case: never for him
            continue
        tasks = assignment.open_tasks(effective(instance), instance, absent_agents=absent)
        if any(t.claimed_by == agent or agent in t.eligible_agents for t in tasks):
            ids.add(instance.id)
    return ids


def _is_involved(principal: Principal, instance: ProcessInstance) -> bool:
    """Is the bound agent involved in this one instance? (involvement rule)

    The single-instance form of :func:`_involved_instance_ids` -- same
    definition (acting agent of any event of the instance, or eligible for /
    owner of an open step right now) -- without scanning every instance. Used
    on every read and on every action path, so it must stay cheap.
    """

    agent = principal.agent_id
    if agent is None:
        return False
    if any(e.agent_id == agent for e in _audit.for_instance(instance.id)):
        return True
    if instance.state is not InstanceState.RUNNING:
        return False
    tasks = assignment.open_tasks(
        _effective_schema_for(instance), instance, absent_agents=_current_absent_agents()
    )
    return any(t.claimed_by == agent or agent in t.eligible_agents for t in tasks)


def _readable_instance_or_404(instance_id: str, principal: Principal) -> ProcessInstance:
    """Load an instance for a read or an action, hiding foreign ones from limited callers.

    A limited caller gets **404** for an instance it is not involved in --
    exactly the same answer as for a missing one (status, text, code and
    ``params``; :class:`InstanceNotFound` has only one text), so the endpoint
    does not reveal which instance ids exist. The action endpoints (claim, return, suspend, resume,
    fail, reset, start, complete, data) load through here too: a 409 or a D3
    finding for a foreign instance would reveal its state or its data.
    """

    instance = _get_instance_or_404(instance_id)
    if instance.is_test and not _may_see_test_instances(principal):
        raise InstanceNotFound(instance_id)
    if _reads_only_own_instances(principal) and not _is_involved(principal, instance):
        raise InstanceNotFound(instance_id)
    return instance


def _may_see_test_instances(principal: Principal) -> bool:
    """May this caller read and act on throw-away test instances?

    A test instance is a modeller's rehearsal of a draft: only modellers and
    administrators start one, so only they (the starter included) read it,
    work its steps or see it listed. Anybody else -- an operator responsible
    for one of its steps, a viewer, an integration identity -- gets the 404
    of a missing instance. The open dev mode carries every role.

    :param principal: the caller.
    :returns: ``True`` for a ``modeler`` or ``admin`` role.
    """

    return bool(principal.roles & {"modeler", "admin"})


class DisplayFieldsRequest(BaseModel):
    element_ids: list[str] = Field(default_factory=list, examples=[["bestellnr", "lieferant"]])


def _read_gate(principal: Principal) -> None:
    """The read floor (viewer and up) for endpoints that need the principal."""

    if not principal.roles & {"viewer", "operator", "modeler", "admin"}:
        raise HTTPException(status_code=403, detail="forbidden")


#: Most cases one background pass escalates (each in its own unit of work).
#: A pass holds one database connection per escalated case and writes an
#: event and a mail per stage; bounded, a large backlog (after a long
#: outage, or a mass deadline) is worked off in steps of this size, one
#: second apart (:data:`procworks.escalation_timer.CATCH_UP_PAUSE_SECONDS`),
#: instead of in one long burst next to the users' requests.
ESCALATION_SWEEP_BATCH = 200


def _escalation_sweep(now: datetime | None = None, *, limit: int | None = None) -> int:
    """Fire due escalation stages across the running cases (T3/E9).

    Called by the background timer (:func:`_background_escalation_pass`,
    ``PROCWORKS_ESCALATION_SWEEP_SECONDS``) and by ``POST
    /admin/escalations/sweep`` -- **never by a read**. It used to run before
    every worklist read over every case with its schema and organisation, so
    the worklists, and the monitoring (one task read per running case) even
    quadratically, grew with the stock. A stage now fires in the rhythm of the
    timer (at most one interval late).

    Only cases of schemas **with escalation rules** are looked at
    (:func:`_schemas_with_escalation_rules` decides on the schemas first;
    without any rule no case is loaded), and those in one collective read.
    Idempotent through the persisted per-activation stage counter; the mail
    outbox additionally dedups per ``esc|instance|node|stage|activation``.
    A step whose due instant or stage instant is not representable (time
    value stored before the 100-year bound of T1/T3) is skipped and logged,
    never raised -- the other cases are swept unchanged.

    **One unit of work per escalated case** (:func:`_escalate_instance`:
    reload, fire, conditional save, audit and mail queue together). If the
    case changed under it -- a completion, or the timer of another API
    process escalating the same case -- the save is refused, the unit rolled
    back (no escalation event, no mail) and the case **skipped**, not
    repeated: the next pass looks again on the fresh state. So of two
    processes sweeping at once, exactly one stores a stage. **Any other
    error of one case** (deadlock, a damaged stored record) is logged with
    the case id and that case skipped as well; the pass goes on. **A
    database outage** (lost connection, statement timeout, exhausted pool)
    is no error of one case: it ends the pass and is raised
    (:func:`_sweep_escalations`).

    :param now: the sweep's clock (default: now).
    :param limit: at most this many due cases are escalated in this call
        (the rest waits for the next pass); ``None``: no bound (the admin
        endpoint).
    :returns: the number of stages fired.
    :raises ValueError: ``limit`` below 1.
    :raises sqlalchemy.exc.OperationalError: the database is out of reach
        (see :func:`_sweep_escalations` for the whole class).
    """

    return _sweep_escalations(now or datetime.now(UTC), limit).fired


#: Interval of the idempotency housekeeping timer (seconds).
IDEMPOTENCY_HOUSEKEEPING_SECONDS = idempotency_mod.CLEANUP_INTERVAL_SECONDS


def _idempotency_housekeeping_pass() -> bool:
    """One pass of the idempotency housekeeping: remove expired keys.

    Runs in the API's housekeeping timer (every
    :data:`IDEMPOTENCY_HOUSEKEEPING_SECONDS`, also when no new key arrives;
    before, expired rows went only when a new key was reserved). Outside any
    unit of work; on PostgreSQL several processes skip each other's rows.

    :returns: ``False`` -- one bounded pass per interval is enough (a backlog
        larger than one batch goes in the following passes).
    """

    removed = _idempotency.remove_expired()
    if removed:
        logging.getLogger("procworks.api").info("removed %d expired idempotency keys", removed)
    return False


def _background_escalation_pass() -> bool:
    """One pass of the background timer: bounded by :data:`ESCALATION_SWEEP_BATCH`.

    :returns: ``True`` when the pass stopped at its bound with more due cases
        left (the timer then continues after a short pause).
    """

    return _sweep_escalations(datetime.now(UTC), ESCALATION_SWEEP_BATCH).more_due


def _schemas_with_escalation_rules() -> set[str]:
    """The schema ids of running cases whose schema carries escalation rules.

    Decided on the schemas before any case is loaded: the distinct schema
    ids of the running cases come from one query, each of their schemas is
    read once. Ad-hoc variants need no look of their own: an ad-hoc change
    can only remove a rule (deleting its step), never add one, and a
    released schema's rules cannot change -- so a case can only escalate if
    its base schema has rules.

    :returns: the ids; empty when no running case runs on a schema with rules.
    """

    with_rules: set[str] = set()
    for schema_id in _instances.running_schema_ids():
        schema = _store.get(schema_id)
        if schema is not None and schema.escalation_policies:
            with_rules.add(schema_id)
    return with_rules


class _SweepOutcome(NamedTuple):
    """Result of one escalation pass (:func:`_sweep_escalations`).

    :ivar fired: stages fired in this pass.
    :ivar more_due: the pass stopped at its bound with due cases left.
    :ivar skipped: due cases not escalated in this pass -- changed meanwhile
        (conflict) or failed with an error of their own (logged with the case
        id); the next pass tries them again.
    """

    fired: int
    more_due: bool
    skipped: int


def _is_database_outage(exc: BaseException) -> bool:
    """Whether ``exc`` means the database is out of reach -- not a fault of one case.

    The same class of errors the API answers with 503 (:func:`_database_outage`,
    :func:`_commit_outcome_unknown`): a lost or refused connection, a
    statement the server cancelled (statement timeout), a closed connection,
    an exhausted pool, a commit that broke off. **Not** an outage: a
    collision with another transaction (deadlock ``40P01``, serialisation
    failure ``40001``, :func:`procworks.unit_of_work.is_transient_collision`)
    -- that concerns the one unit that lost, and repeating it later helps.

    :param exc: any exception.
    :returns: ``True`` for an outage.
    """

    if isinstance(exc, CommitOutcomeUnknown | PoolTimeoutError):
        return True
    if isinstance(exc, OperationalError | InterfaceError):
        return not unit_of_work.is_transient_collision(exc)
    return False


def _sweep_escalations(moment: datetime, limit: int | None) -> _SweepOutcome:
    """The pass behind :func:`_escalation_sweep` (see there).

    **Each case is isolated** -- from the faults of the others, not from the
    database. Its due computation and its unit of work are guarded one by
    one: an error of one case (a conflict, a deadlock or serialisation
    failure, a stored record that no longer loads, a bug) rolls back that
    case's unit, is logged with the case id and counted as skipped -- and
    the pass goes on with the next case. The cases are read in a stable
    order, so before this a permanently broken case stopped every pass at
    the same place and no case behind it ever escalated.

    **A database outage ends the pass** (:func:`_is_database_outage`: lost
    connection, statement timeout, exhausted pool, broken-off commit): it is
    raised unchanged, so the admin trigger answers 503 like every other
    request and the background timer logs the failed pass and tries again
    at its next tick. Skipping it per case turned an outage into a ``200``
    with every due case "skipped" -- one failing transaction per case, and
    nothing showed that the database was gone. Non-``Exception`` signals
    (``KeyboardInterrupt``, ``SystemExit``) end the pass as well.

    :param moment: the sweep's clock.
    :param limit: most cases to escalate, or ``None`` for no bound.
    :returns: stages fired, whether due cases are left beyond the bound, and
        the number of skipped cases.
    :raises ValueError: ``limit`` below 1.
    :raises sqlalchemy.exc.OperationalError: (and the other outage errors of
        :func:`_is_database_outage`) the database is out of reach; what the
        pass stored before stays stored (one unit per case).
    """

    if limit is not None and limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    schema_ids = _schemas_with_escalation_rules()
    if not schema_ids:
        return _SweepOutcome(0, False, 0)
    effective = _schema_memo()
    absent = _current_absent_agents()
    log = logging.getLogger("procworks.api")
    fired_total = 0
    attempted = 0
    skipped = 0
    for instance in _instances.list_instances(running_only=True, schema_ids=schema_ids):
        try:
            if not _due_escalations(instance, moment, effective(instance)):
                continue
        except Exception as exc:  # noqa: BLE001 -- one broken case must not stop the pass
            if _is_database_outage(exc):
                raise  # the database is gone, not this case (see docstring)
            skipped += 1
            log.warning(
                "escalation of %s skipped: its due stages cannot be determined (%s: %s); "
                "next sweep retries",
                instance.id, type(exc).__name__, exc,
            )
            continue
        if limit is not None and attempted >= limit:
            return _SweepOutcome(fired_total, True, skipped)
        attempted += 1
        try:
            fired_total += unit_of_work.run(
                functools.partial(_escalate_instance, instance.id, moment, absent),
                retries=0,
                serialize=_write_serialization(),
            )
        except ConcurrentModification:
            skipped += 1
            log.info(
                "escalation of %s skipped: the case changed meanwhile, next sweep retries",
                instance.id,
            )
        except Exception as exc:  # noqa: BLE001 -- isolate each case (see docstring)
            if _is_database_outage(exc):
                raise  # the database is gone, not this case (see docstring)
            # The case's unit was rolled back (no event, no mail, no stage).
            # A deadlock or serialisation failure, a stored record that no
            # longer loads: the case waits for the next pass, the others are
            # escalated now.
            skipped += 1
            log.warning(
                "escalation of %s skipped: %s: %s; next sweep retries",
                instance.id, type(exc).__name__, exc,
            )
    return _SweepOutcome(fired_total, False, skipped)


def _due_escalations(
    instance: ProcessInstance,
    moment: datetime,
    schema: ProcessSchema | None = None,
) -> list[tuple[str, int, EscalationStage]]:
    """The escalation stages of ``instance`` that are due at ``moment``, in order.

    Read-only. Shared due computation with the worklist bands (rule B): once
    claimed, a modelled processing duration counts from the claim; the pause
    credit (net time, opt-in) shifts the due instant the same way it shifts
    the bands.

    :param instance: the case (only RUNNING cases escalate).
    :param moment: the sweep's clock.
    :param schema: the case's effective schema, if the caller has it already
        (the sweep, from its per-pass cache); loaded otherwise.
    :returns: ``(node_id, stage_index, stage)`` for every stage not fired yet
        whose instant has passed; empty for a case without policies.
    """

    if instance.state is not InstanceState.RUNNING:
        return []
    if schema is None:
        schema = _effective_schema_for(instance)
    if not schema.escalation_policies:
        return []
    due_stages: list[tuple[str, int, EscalationStage]] = []
    for node_id, policy in schema.escalation_policies.items():
        if instance.node_states.get(node_id) not in (
            NodeState.ACTIVATED,
            NodeState.RUNNING,
        ):
            continue
        if instance.node_details.get(node_id) is NodeDetailState.FAILED:
            # E2: a failed step waits for its recovery -- the failure is
            # already loud, escalating further would only add noise.
            continue
        constraint = schema.time_constraints.get(node_id)
        target, origin = worklist_priority.effective_clock(
            constraint,
            instance.node_activated_at.get(node_id),
            instance.node_claimed_at.get(node_id),
            worklist_priority.pause_credit_seconds(
                constraint,
                instance.node_paused_seconds.get(node_id),
                instance.node_suspended_at.get(node_id),
                moment,
            ),
        )
        if origin is None or target is None:
            continue  # no stamped clock (legacy instance) -> no due instant
        # Guarded shift: a schema stored before the T1/T3 upper bound can
        # carry a time value beyond the year 9999. Such a step has no due
        # instant (logged) instead of failing the sweep -- and with it the
        # worklists of every agent, since the sweep runs before each read.
        due = worklist_priority.shifted(origin, target, f"{instance.id}/{node_id}")
        if due is None:
            continue
        already = instance.escalated_stages.get(node_id, 0)
        for index in range(already, len(policy.stages)):
            stage = policy.stages[index]
            fire_at = worklist_priority.shifted(
                due, stage.after_seconds, f"{instance.id}/{node_id} stage {index + 1}"
            )
            if fire_at is None or moment < fire_at:
                break  # stages are strictly ascending (T3b)
            due_stages.append((node_id, index, stage))
    return due_stages


def _escalate_instance(instance_id: str, moment: datetime, absent: frozenset[str]) -> int:
    """Fire the due stages of one case and save it -- one unit of work.

    Reloads the case (the sweep's first read may be stale by now), fires
    every due stage (audit event, mail queued), raises the stage counters and
    saves conditionally.

    :param instance_id: the case.
    :param moment: the sweep's clock.
    :param absent: agents absent right now (for the recipients).
    :returns: the number of stages fired (0 if nothing is due any more).
    :raises ConcurrentModification: the case changed since this unit read it.
    """

    # Like every request: never beside a reset (its mail entry would survive
    # the wipe of a case it escalated).
    _hold_write_gate(exclusive=False)
    instance = _instances.get(instance_id)
    if instance is None:
        return 0
    due_stages = _due_escalations(instance, moment)
    if not due_stages:
        return 0
    schema = _effective_schema_for(instance)
    for node_id, index, stage in due_stages:
        _fire_escalation_stage(instance, schema, node_id, index, stage, absent)
        instance.escalated_stages[node_id] = index + 1
    _instances.put(instance)
    return len(due_stages)


def _fire_escalation_stage(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    index: int,
    stage: EscalationStage,
    absent: frozenset[str],
) -> None:
    """Boundary side effects of one fired stage: audit + best-effort mail.

    FUNCTIONAL: the added performers learn about the task (the broadened
    offer itself materialises through ``eligible_agents``). HIERARCHICAL: the
    leadership set is informed. Recipients without an address are skipped --
    delivery is best-effort and never blocks a process step (the audit event
    is the guaranteed trace). Test instances stay silent like everywhere.
    """

    if instance.is_test:
        return
    label = _label_of(schema, node_id) or node_id
    _audit.append(
        EventType.TASK_ESCALATED,
        instance.id,
        instance.schema_id,
        schema_version=instance.schema_version,
        node_id=node_id,
        label=label,
        detail={"stage": str(index + 1), "kind": stage.kind.value},
    )
    recipients = assignment.resolve_rule_agents(
        schema, stage.rule, instance, absent_agents=absent
    )
    addresses = sorted(
        (agent.email or "").strip()
        for agent_id in recipients
        if (agent := schema.org_model.agents.get(agent_id)) is not None
        and (agent.email or "").strip()
    )
    if not addresses:
        return
    if stage.kind is EscalationKind.FUNCTIONAL:
        subject = f'Eskalation (Stufe {index + 1}): "{label}" jetzt auch bei Ihnen'
        body = (
            f'Die Aufgabe "{label}" (Vorgang {instance.id}) hat ihre Soll-Zeit '
            f"überschritten und wurde Ihrer Bearbeitermenge zusätzlich "
            f"angeboten (funktionale Eskalation, Stufe {index + 1})."
        )
    else:
        subject = f'Eskalation (Stufe {index + 1}): "{label}" ist überfällig'
        body = (
            f'Die Aufgabe "{label}" (Vorgang {instance.id}) hat ihre Soll-Zeit '
            f"überschritten (hierarchische Eskalation, Stufe {index + 1}). "
            f"Bitte prüfen Sie den Vorgang."
        )
    marker = instance.node_activated_at.get(node_id)
    dedup = f"esc|{instance.id}|{node_id}|{index}|{marker.isoformat() if marker else ''}"
    _mail_outbox.enqueue(
        mail_runtime.MailMessage(
            to=addresses,
            subject=subject,
            body=body,
            instance_id=instance.id,
            node_id=node_id,
            schema_id=instance.schema_id,
            message_id=dedup,
        ),
        dedup,
    )
    # Queued with the escalation, sent once it is committed.
    unit_of_work.after_commit("mail", lambda: _dispatch_mail_outbox())


def _tasks_for_agent(agent_id: str) -> list[OpenTask]:
    """Collect the open tasks an agent is currently eligible for (incl. deputy).

    Each instance's tasks are prioritised with its own time context (time-based
    worklist prioritisation); the cross-instance list is then re-sorted so the
    agent sees one coherent, most-urgent-first todo list across all instances.

    Withdrawn view (E1): a task someone *else* has claimed leaves this personal
    list until it is returned or completed; the agent's own claimed tasks stay
    (marked via ``claimed_by``). The instance-wide list stays complete.

    Test instances never appear here (nor for deputies, who see the same
    list): they are a modeller's dry run, played through in the test-run view
    via ``/instances/{id}/tasks``. In a real worklist they looked exactly like
    real tasks and could be claimed and completed unnoticed -- mails and
    escalations of test instances are silent for the same reason.

    A pure read: due escalation stages are fired by the background timer
    (:func:`_escalation_sweep`), not here. The running cases come from one
    collective store read, each schema is loaded once per call
    (:func:`_open_tasks_of_running_cases`).
    """

    tasks = [
        task
        for task in _open_tasks_of_running_cases(None)
        # withdrawn: a task someone else took over leaves this list
        if (task.claimed_by is None or task.claimed_by == agent_id)
        and agent_id in task.eligible_agents
    ]
    tasks.sort(key=_worklist_order)
    return tasks


def _worklist_order(task: OpenTask) -> tuple[object, ...]:
    """Sort key of a cross-case worklist: most urgent first (band, priority, due date)."""

    return worklist_priority.sort_key(
        task.time_criticality,
        PRIORITY_RANK[task.priority],
        task.due_at,
        task.label,
        task.node_id,
    )


def _open_tasks_of_running_cases(only: set[str] | None) -> list[OpenTask]:
    """The open tasks of every running real case, each prioritised in its own time.

    The shared core of the personal worklists and the monitoring's task
    overview. Test instances are left out (a modeller's dry run, never part
    of a worklist or the monitoring). One collective store read for the
    cases, each schema loaded once (:func:`_schema_memo`), the absences read
    once -- the work per call does not grow by a store access per case.

    :param only: restrict to these case ids (the cases a limited operator may
        read); ``None`` for all.
    :returns: the open tasks in case order, unsorted across cases.
    """

    effective = _schema_memo()
    absent = _current_absent_agents()
    tasks: list[OpenTask] = []
    for instance in _instances.list_instances(running_only=True):
        if instance.is_test or (only is not None and instance.id not in only):
            continue
        schema = effective(instance)
        ctx = _time_context(instance, schema)
        tasks += assignment.open_tasks(schema, instance, ctx, absent_agents=absent)
    return tasks


def _readable_instances(principal: Principal) -> list[ProcessInstance]:
    """Every case ``principal`` may read, in one collective store read.

    The same reading right as ``GET /instances`` and
    :func:`_readable_instance_or_404`: a limited operator gets the cases it
    is involved in, test instances only modellers/administrators. For the
    monitoring list and the titles, which used to load case after case.

    :param principal: the caller (read floor already passed).
    :returns: the readable cases (detached copies).
    """

    instances = _instances.list_instances()
    if not _may_see_test_instances(principal):
        instances = [i for i in instances if not i.is_test]
    if _reads_only_own_instances(principal):
        own = _involved_instance_ids(principal)
        instances = [i for i in instances if i.id in own]
    return instances


# --- absence / deputy substitution ---------------------------------------
# (endpoints: procworks/api_execution.py)


def _require_agent_self_or_supervisor(principal: Principal, agent_id: str) -> None:
    """Guard: only the person themselves or a supervisor touches a person's record.

    Shared by the worklist (``/agents/{id}/tasks``), the absence endpoints and
    the override of someone else's claim (:func:`_force_foreign_claim`), so the
    access rule is defined once. Admitted are:

    * the principal bound to ``agent_id`` (self-service);
    * ``admin`` and ``modeler`` (supervision), bound or not;
    * an **unbound machine identity** that has already passed the endpoint's
      role gate (:func:`_may_act_for_others` without an instance): the open
      dev mode and an unbound token of the static-token mode, the documented
      integration path, e.g. an HR bridge recording absences.

    The callers' role gate comes first and is ``operator``/``modeler``/
    ``admin`` for all of them (worklist, absences, return, reset). A principal
    holding only the role ``integration`` (e.g. a JWT service account, or a
    machine token, which carries exactly that role) is therefore refused there
    with 403 and never reaches this guard; the ``integration`` branch of
    :func:`_may_act_for_others` only matters for a principal that also holds
    one of those roles.

    Everything else gets 403 -- a bound operator on a colleague, and a
    **personal login without an agent binding** (password or JWT, roles only
    ``operator``/``viewer``). That login is neither the person nor a
    supervisor; letting it through would hand it every worklist with its task
    context and the absences that steer who receives work.

    :param principal: the caller (role gate already passed).
    :param agent_id: the agent whose record is read or changed.
    :raises HTTPException: 403 ``forbidden`` when none of the cases applies.

    Edge case: an unknown ``agent_id`` is decided like any other foreign agent
    (403 for a personal operator), so the guard tells nobody which ids exist.
    """

    if principal.agent_id == agent_id and principal.is_bound:
        return
    if principal.roles.intersection({"admin", "modeler"}):
        return
    if not principal.is_bound and _may_act_for_others(principal, None):
        return
    raise HTTPException(status_code=403, detail="forbidden")


def _known_agent_ids() -> set[str]:
    """Every agent id known to the system (shared org models + embedded orgs).

    Absences are org-wide and keyed by agent id; this backs a helpful 404 when an
    unknown agent id is submitted. The union spans the shared org store and every
    schema's embedded org model, so a per-schema agent is recognised too.
    """

    ids = _all_agent_ids()
    for schema_id in _store.list_ids():
        schema = _store.get(schema_id)
        if schema is not None:
            ids.update(schema.org_model.agents.keys())
    return ids


class CreateAbsenceRequest(BaseModel):
    """Define an absence window for an agent (worklist self-service)."""

    start_at: datetime
    end_at: datetime
    note: str = ""


def _list_absences(agent_id: str) -> list[AbsenceEntry]:
    """The recorded absences of one agent, earliest window first."""

    entries = [e for e in _absence_store.list_entries() if e.agent_id == agent_id]
    entries.sort(key=lambda e: (e.start_at, e.end_at))
    return entries


def _require_acting_agent(
    principal: Principal, requested: str | None, instance: ProcessInstance
) -> str:
    """Resolve the acting agent for ownership operations; 422 when unknown.

    Claim/start need a concrete owner (W1/W4) -- unlike completion there is no
    meaningful anonymous variant, so an open-dev-mode call without an agent id
    is a request error, not a permission problem.
    """

    acting = _resolve_acting_agent(principal, requested, instance)
    if acting is None:
        raise HTTPException(
            status_code=422, detail="agent_id is required for this operation"
        )
    return acting


def _detail_audit(
    event: EventType,
    instance: ProcessInstance,
    after: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str | None,
    detail: dict[str, str] | None = None,
    principal: Principal | None = None,
) -> None:
    """Append one audit event of a worklist act on a step (skipped for tests).

    Shared by the E1 ownership acts (claim, return, start) and the E2
    detail-state acts (suspend, resume, fail, reset), which all record the
    same shape: the step's node and label, the acting agent and an optional
    detail. A throw-away test instance of a draft records no audit events, so
    it never reaches the monitoring KPIs (mirrors instance creation).

    :param event: the event type to record.
    :param instance: the instance *before* the act (decides the test skip).
    :param after: the instance after the act (id, schema, version).
    :param schema: the effective schema, for the step's label.
    :param node_id: the step acted on.
    :param agent_id: the acting agent, ``None`` when nobody is named.
    :param detail: extra detail (e.g. the failure reason), may be ``None``.
    :param principal: when given, the delegating sender is merged in as
        ``detail.actor`` if the act was done in an agent's name
        (:func:`_delegation_detail`); without delegation ``detail`` stays
        as passed (``None`` remains ``None``).
    """

    if instance.is_test:
        return
    if principal is not None:
        delegation = _delegation_detail(principal, agent_id)
        if delegation:
            detail = {**(detail or {}), **delegation}
    _audit.append(
        event,
        after.id,
        after.schema_id,
        schema_version=after.schema_version,
        node_id=node_id,
        label=_label_of(schema, node_id),
        agent_id=agent_id,
        detail=detail,
    )


def _implicit_start_audit(
    act: str,
    instance: ProcessInstance,
    after: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str | None,
    principal: Principal | None = None,
) -> None:
    """Record the claim and start an act took along implicitly (E1/E2).

    Suspending or failing a step that is only offered (or only claimed)
    claims and starts it on the way -- the engine's Started collapse
    (``execution._require_started_owner``). The history must show these
    transitions with the same event types as the explicit way, before the
    act's own event: otherwise it reads as if a step nobody had taken was
    paused, and "worked on since" has no event to begin with.

    Derived from the difference ``instance`` -> ``after``, never from the
    request: ``ACTIVITY_CLAIMED`` when the step had no owner before and has
    one now, ``ACTIVITY_STARTED`` when it was not RUNNING before and is now.
    A step already started, an idempotent repeat and a refused act (no
    ``after``) record nothing here. Each event carries ``{"implicit": act}``
    so a reader sees which act took it along; the delegation detail is
    merged as in :func:`_detail_audit`, which appends them (test instances
    record nothing). Called **before** the act's own event, each append
    extends the hash chain like any other event.

    :param act: the act that took the transitions along (``suspend``/``fail``).
    :param instance: the instance before the act.
    :param after: the instance after the act.
    :param schema: the effective schema, for the step's label.
    :param node_id: the step acted on.
    :param agent_id: the acting agent.
    :param principal: the caller, for the delegation detail.
    """

    implicit = {"implicit": act}
    if instance.claimed_by.get(node_id) is None and after.claimed_by.get(node_id) is not None:
        _detail_audit(EventType.ACTIVITY_CLAIMED, instance, after, schema, node_id,
                      agent_id, detail=implicit, principal=principal)
    if (instance.node_states.get(node_id) is not NodeState.RUNNING
            and after.node_states.get(node_id) is NodeState.RUNNING):
        _detail_audit(EventType.ACTIVITY_STARTED, instance, after, schema, node_id,
                      agent_id, detail=implicit, principal=principal)


def _force_foreign_claim(
    principal: Principal,
    instance: ProcessInstance,
    node_id: str,
    acting: str | None,
) -> bool:
    """Decide whether a return/reset acts on *someone else's* claim.

    The owner may always return or reset their own claim. For a step claimed by
    another agent the same supervisory authority applies as for the worklist
    and absence endpoints (:func:`_require_agent_self_or_supervisor`, 403
    otherwise), and the engine must be told to override the ownership check.

    :param principal: the caller.
    :param instance: the instance before the act.
    :param node_id: the step acted on.
    :param acting: the resolved acting agent (``None`` when nobody is named).
    :returns: ``True`` when the step is claimed by someone other than
        ``acting`` (and the caller may override it), else ``False``.
    :raises HTTPException: 403 when the caller lacks supervisory authority.
    """

    holder = instance.claimed_by.get(node_id)
    if holder is None or acting == holder:
        return False
    _require_agent_self_or_supervisor(principal, holder)
    return True


# --- ad-hoc changes (per-instance variant; R1/R2) ------------------------
# (endpoints: procworks/api_changes.py)

#: Hinweis, wenn einer Ad-hoc-Änderung der Anlass fehlt.
ADHOC_REASON_REQUIRED = (
    "Ad-hoc-Änderung: Bitte einen Anlass angeben – er wird mit deinem Namen im "
    "Verlauf des Vorgangs festgehalten."
)


def _adhoc_audit_detail(
    instance: ProcessInstance, principal: Principal, reason: str | None
) -> dict[str, str]:
    """Check the reason of an ad-hoc change and build its audit detail.

    An ad-hoc change alters *how one running case continues* -- so it must be
    traceable who changed it and why. Like a supervisory data correction
    (:func:`_authorize_data_write`), a real instance needs a reason, and the event
    records who acted. Test instances write no audit events and need none.

    :param instance: the instance to be changed (before the change).
    :param principal: the caller (already restricted to modeler/admin).
    :param reason: the reason from the request, may be blank.
    :returns: ``{"actor": ..., "reason": ...}`` for the audit event (the
        reason only when given).
    :raises HTTPException: 422 ``REQ.reason-required`` (``what`` ``adhoc``,
        message :data:`ADHOC_REASON_REQUIRED`) when a real instance is changed
        without a visible reason.
    """

    text = _visible_reason(reason) or ""
    if not text and not instance.is_test:
        raise _reason_required("adhoc", ADHOC_REASON_REQUIRED)
    detail = {"actor": principal.subject}
    if text:
        detail["reason"] = text
    return detail


# --- schema evolution + instance migration (M1-M5) -----------------------
# (endpoints: procworks/api_changes.py)


def _migrate_and_record(
    instance: ProcessInstance,
    target: ProcessSchema,
    data_mapping: dict[str, object] | None,
    principal: Principal | None = None,
) -> ProcessInstance:
    """Migrate one instance (M1-M5 via the core) and do the boundary follow-ups.

    The single path shared by ``POST /instances/{id}/migrate`` and the bulk
    assistant (``POST /schemas/{id}/migrate-instances``), so both write the same
    audit event and trigger the same notifications. Raises HTTP 422 with the
    findings when the core refuses; the stored instance is then untouched
    (``_commit_instance_or_422`` persists only a successful result).
    """

    source = _get_or_404(instance.schema_id)
    before_states = dict(instance.node_states)
    after = _commit_instance_or_422(
        lambda: migration.migrate_instance(
            instance,
            source,
            target,
            data_mapping=data_mapping,
            resolver=_resolver,
        )
    )
    # Wer hat migriert? Eine Migration ist eine Entscheidung, kein
    # Maschinenereignis -- der Verlauf nennt deshalb den Handelnden statt
    # "System". Gebundener Login: der Agent (der Verlauf loest ihn zum
    # Namen auf); ungebundener: der Login in ``detail.actor``, dieselbe
    # Schreibweise wie beim Abschluss (:func:`_completion_detail`).
    detail: dict[str, str] = {
        "source_schema_id": instance.schema_id,
        "target_schema_id": target.id,
    }
    if principal is not None and principal.agent_id is None:
        detail["actor"] = principal.subject
    _audit.append(
        EventType.INSTANCE_MIGRATED,
        after.id,
        after.schema_id,
        schema_version=after.schema_version,
        agent_id=principal.agent_id if principal is not None else None,
        detail=detail,
    )
    if not instance.is_test:
        # Migration can activate mail-bound nodes on the *target* schema (a step
        # the source did not have, or a re-mapped position) -> notify.
        _after_advance(_effective_schema_for(after), before_states, after)
    return after


# --- migration assistant (bulk, per target revision) ----------------------
# (endpoints: procworks/api_changes.py)


class MigrationCandidate(BaseModel):
    """One running instance of an earlier revision, assessed against a target."""

    instance_id: str
    schema_id: str
    schema_version: int
    migratable: bool
    findings: list[ValidationFinding]
    #: Mandatory elements the instance lacks and the target can no longer
    #: produce (M4) -- the assistant asks for start values for exactly these.
    missing_data: list[str]
    #: When the instance started -- the assistant names it by that when it has
    #: no naming values yet, instead of showing the bare id.
    started_at: datetime | None = None


class MigrationAssistantReport(BaseModel):
    target_schema_id: str
    target_version: int
    candidates: list[MigrationCandidate]


class BulkMigrateRequest(BaseModel):
    #: Instances to migrate; ``None`` = every candidate of the target.
    instance_ids: list[str] | None = None
    #: Start values for newly required data (M4), shared by all instances.
    #: Only applied to instances that actually lack the element.
    data_mapping: dict[str, object] = Field(default_factory=dict)
    #: ``False`` (default) = dry run: report what *would* happen, change nothing.
    execute: bool = False


class BulkMigrateResult(BaseModel):
    instance_id: str
    migrated: bool
    findings: list[ValidationFinding]


class BulkMigrateReport(BaseModel):
    target_schema_id: str
    executed: bool
    results: list[BulkMigrateResult]


class MigrationTarget(BaseModel):
    """The newest released revision an instance could move to (or none)."""

    schema_id: str | None = None
    version: int | None = None
    name: str | None = None


def _migration_candidates(target: ProcessSchema) -> list[ProcessInstance]:
    """Running top-level instances of earlier revisions of ``target``.

    Excluded on purpose: test instances (throw-away, no productive state),
    finished instances (nothing left to change) and **child instances of a
    sub-process** -- they run on the version their parent's SUBPROCESS binding
    pinned, and moving one on its own would silently change that binding. The
    single-instance endpoint stays available for deliberate cases.
    """

    schemas = [s for sid in _store.list_ids() if (s := _store.get(sid)) is not None]
    sources = migration.predecessor_ids(target, schemas)
    candidates: list[ProcessInstance] = []
    for iid in _instances.list_ids():
        inst = _instances.get(iid)
        if (
            inst is None
            or inst.schema_id not in sources
            or inst.state is not InstanceState.RUNNING
            or inst.is_test
            or inst.parent_instance_id is not None
        ):
            continue
        candidates.append(inst)
    return sorted(candidates, key=lambda i: i.id)


def _assess(
    instance: ProcessInstance,
    target: ProcessSchema,
    data_mapping: dict[str, object] | None,
) -> tuple[list[ValidationFinding], list[str]]:
    """M1-M5 findings plus the element ids still missing (M4) for one instance."""

    source = _get_or_404(instance.schema_id)
    findings = migration.check_migration(
        instance, source, target, resolver=_resolver, data_mapping=data_mapping
    )
    missing = sorted(
        {e for e, _ in migration.missing_mandatory_data(instance, target, data_mapping)}
    )
    return findings, missing


def _mapping_for(
    instance: ProcessInstance, target: ProcessSchema, shared: dict[str, object]
) -> dict[str, object] | None:
    """The part of the shared start values this instance actually needs.

    A shared value never overwrites data the instance already carries -- the
    assistant fills gaps, it does not rewrite history.
    """

    needed = {e for e, _ in migration.missing_mandatory_data(instance, target)}
    picked = {k: v for k, v in shared.items() if k in needed}
    return picked or None


def _visible_migration_candidates(
    target: ProcessSchema, principal: Principal
) -> list[ProcessInstance]:
    """Migration candidates the caller may see and act on.

    Everyone but a limited operator gets all candidates. A limited operator
    (see :func:`_reads_only_own_instances`) gets only the instances it is
    involved in -- the migration assistant opened from the instance view passes
    exactly that one id, and nobody else's case can be listed or moved.
    """

    candidates = _migration_candidates(target)
    if not _reads_only_own_instances(principal):
        return candidates
    own = _involved_instance_ids(principal)
    return [inst for inst in candidates if inst.id in own]


# --- monitoring + audit (step 15) ----------------------------------------
# (endpoints: procworks/api_monitoring.py)


def _agents_with_login() -> frozenset[str] | None:
    """Agent ids bound to a stored login, or ``None`` when that is unknowable.

    Only password mode keeps every login in a credential store. In token and
    JWT mode people sign in without a stored record, so "has no login" cannot
    be decided there -- reporting it would raise false alarms.
    """

    if not isinstance(_auth_backend, PasswordAuthBackend):
        return None
    return frozenset(u.agent_id for u in _auth_backend.store.list_users() if u.agent_id)


# --- versioned integration API (/v1) — inbound control by external tools -
#
# These endpoints mirror the existing runtime endpoints under a stable,
# versioned ``/v1`` prefix and gate them with integration *scopes* (so a service
# token is confined to least privilege), while remaining fully usable by human/
# open principals via their roles. Mutating calls honour an ``Idempotency-Key``
# header. The endpoints add no new domain logic: they reuse the same
# validate-before-commit core path as the GUI (Section 5.4, API-first).
#
# (endpoints: procworks/api_v1.py)

class SetDataRequest(BaseModel):
    values: dict[str, object] = Field(
        ..., examples=[{"betrag": 1200, "status": "open"}]
    )
    #: Begründung einer Datenkorrektur als Aufsichtseingriff (Modellierer/Admin
    #: an Elementen, die kein eigener offener Schritt schreibt); siehe
    #: ``_authorize_data_write``.
    reason: str | None = None


class V1CompleteRequest(BaseModel):
    data: dict[str, object] = Field(default_factory=dict)
    agent_id: str | None = Field(default=None, examples=["a1"])
    #: Begründung eines Aufsichtseingriffs (ungebundener Token ohne ``agent_id``
    #: an einem Schritt mit Bearbeiterregel); siehe ``_require_supervision_reason``.
    supervision_reason: str | None = None


def _unwritable_completion_keys(
    schema: ProcessSchema, node_id: str, data: dict[str, object]
) -> list[ValidationFinding]:
    """D6: completion values the step is not allowed to set.

    Allowed are exactly the elements the step has a WRITE/READ_WRITE access to
    (the same set its input mask may write, U3). Everything else is refused:

    * a **known** element the step does not write (``D6.not-writable``) --
      e.g. an approval step overwriting the amount it only reads;
    * an **unknown** key (``D6.unknown-element``).

    Unknown nodes are left to the engine (409 as before), so this never turns a
    wrong ``node_id`` into a data finding.
    """

    if node_id not in schema.nodes:
        return []
    writable = {a.element_id for a in schema.accesses_of(node_id) if a.mode in WRITE_MODES}
    step = node_name(schema, node_id)
    findings: list[ValidationFinding] = []
    for key in data:
        if key in writable:
            continue
        element = schema.data_elements.get(key)
        if element is None:
            findings.append(
                ValidationFinding(
                    rule="D6",
                    node_id=node_id,
                    message=f"'{key}' is no data element of this process",
                    code="D6.unknown-element",
                    params={"element": key, "step": step},
                )
            )
        else:
            findings.append(
                ValidationFinding(
                    rule="D6",
                    node_id=node_id,
                    message=f"step '{node_id}' does not write '{key}'",
                    code="D6.not-writable",
                    params={"element": element.name, "step": step},
                )
            )
    return findings


def _validate_data_values(
    schema: ProcessSchema, values: dict[str, object]
) -> list[ValidationFinding]:
    """Boundary type/existence check for inbound data writes (runtime D3)."""

    findings: list[ValidationFinding] = []
    for element_id, value in values.items():
        element = schema.data_elements.get(element_id)
        if element is None:
            findings.append(
                ValidationFinding(
                    rule="D3",
                    message=f"unknown data element '{element_id}'",
                    code="D3.unknown-element",
                    params={"element": element_id},
                )
            )
            continue
        if not value_matches_type(element.data_type, value):
            findings.append(
                ValidationFinding(
                    rule="D3",
                    message=(
                        f"value for '{element_id}' is not a "
                        f"{element.data_type.value}"
                    ),
                    code="D3.wrong-type",
                    params={"element": element.name, "type": element.data_type.value},
                )
            )
    return findings


def _own_task_writes(
    schema: ProcessSchema, instance: ProcessInstance, agent_id: str
) -> set[str]:
    """Data elements the agent may write *right now* through its own open steps.

    A step counts when it is open (ACTIVATED/RUNNING, with a staff rule) and
    either claimed by ``agent_id`` or unclaimed with ``agent_id`` among its
    eligible agents (deputies and escalation included, exactly as the worklist
    resolves them). Of those steps, every WRITE/READ_WRITE binding counts.
    A step claimed by somebody else never counts -- that is their work item.
    """

    writable: set[str] = set()
    for task in assignment.open_tasks(
        schema, instance, absent_agents=_current_absent_agents()
    ):
        holder = task.claimed_by
        if holder == agent_id or (holder is None and agent_id in task.eligible_agents):
            writable.update(
                a.element_id
                for a in schema.accesses_of(task.node_id)
                if a.mode in WRITE_MODES
            )
    return writable


def _authorize_data_write(
    principal: Principal,
    instance: ProcessInstance,
    schema: ProcessSchema,
    element_ids: Iterable[str],
    reason: str | None,
) -> str | None:
    """Decide whether the caller may set these values directly.

    ``PUT /instances/{id}/data`` must not undermine what a step decided: a
    value a four-eyes approval rests on may change only with a right, a reason
    and an audit event. The rules, all at the boundary (identity is boundary knowledge):

    * **Test instances** stay free (no audit, no productive work).
    * A **completed** instance is closed: 409 for everybody.
    * **Machine identities** -- open dev mode, static tokens, the
      ``integration`` role -- keep the documented integration path
      (``PUT /v1/instances/{id}/data`` with scope ``data:write``); the event
      records the sender.
    * A **bound** caller may set exactly the elements its own open steps write
      (:func:`_own_task_writes`) -- entering the data of the work at hand.
    * Anything else is a correction outside one's own work: only
      ``modeler``/``admin`` may do it, and only as a supervision act with a
      mandatory reason (422 starting with ``"Aufsichtseingriff"`` when it is
      missing, which the web client recognises). Everyone else gets 403.

    Returns the trimmed supervision reason, or ``None`` when no supervision is
    involved.
    """

    if instance.is_test:
        return None
    if instance.state is not InstanceState.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=(
                "Der Vorgang ist abgeschlossen; seine Daten lassen sich nicht "
                "mehr ändern."
            ),
        )
    if _may_act_for_others(principal, instance):
        return None
    wanted = set(element_ids)
    if principal.agent_id is not None and wanted <= _own_task_writes(
        schema, instance, principal.agent_id
    ):
        return None
    if not principal.roles & {"modeler", "admin"}:
        raise HTTPException(
            status_code=403,
            detail=(
                "Diese Daten gehören zu keinem Schritt, den du gerade bearbeitest. "
                "Werte gibst du beim Abschließen deines Schritts ein; eine "
                "Korrektur darüber hinaus nimmt die Prozessverantwortung vor."
            ),
        )
    cleaned = _visible_reason(reason)
    if cleaned is None:
        raise _reason_required("data-correction", DATA_CORRECTION_REASON_REQUIRED)
    return cleaned


#: The ``message`` of the 422 for a data correction without a reason
#: (``REQ.reason-required``, ``what`` ``data-correction``).
DATA_CORRECTION_REASON_REQUIRED = (
    "Aufsichtseingriff: Diese Werte gehören zu keinem Schritt, den du "
    "gerade bearbeitest. Bitte eine Begründung für die Korrektur "
    "angeben – sie wird im Audit-Verlauf festgehalten."
)


def _set_instance_data(
    instance_id: str, req: SetDataRequest, principal: Principal
) -> dict[str, object]:
    """Shared body of both data endpoints: authorise, type-check, store, audit.

    Order matters: first the instance must be readable for the caller (a
    foreign one is 404, so a D3 finding never describes the data of a case the
    caller may not read), then the D3
    type check (a malformed request is a 422 regardless of who sends it), then
    the mask checks of every mask entering the element (U4,
    :func:`procworks.validator.element_value_findings`), then
    :func:`_authorize_data_write`. Every
    element whose value actually changes gets one ``INSTANCE_DATA_SET`` event
    with ``old``/``new`` as JSON (``null`` for a previously unset element),
    the sender as ``actor`` where it is not the bound agent itself, and the
    supervision ``reason`` where there is one. Unchanged values write nothing.
    Test instances are stored but never audited (as everywhere else).
    """

    instance = _readable_instance_or_404(instance_id, principal)
    schema = _effective_schema_for(instance)
    findings = _validate_data_values(schema, req.values)
    if findings:
        raise _findings_422(findings)
    # U4: a value set directly passes every mask that enters its element --
    # for bearers, integrations and supervision alike (supervision overrides
    # who may act, never what a value must look like).
    mask_findings = element_value_findings(schema, req.values)
    if mask_findings:
        raise _findings_422(mask_findings)
    supervision = _authorize_data_write(
        principal, instance, schema, req.values.keys(), req.reason
    )
    # Noted for every open step that writes a corrected element: once a loop
    # repeats a step, its mandatory write counts as supplied only when it was
    # set during the step's current activation (completion check).
    updated = record_data_correction(instance, schema, req.values.keys())
    updated.data_values.update(req.values)
    _instances.put(updated)
    if not instance.is_test:
        for element_id, new in req.values.items():
            missing = element_id not in instance.data_values
            old = instance.data_values.get(element_id)
            if not missing and old == new:
                continue
            detail = {
                "element": element_id,
                "old": json.dumps(old, ensure_ascii=False, default=str),
                "new": json.dumps(new, ensure_ascii=False, default=str),
            }
            if not principal.is_bound and _auth_mode() != "open":
                detail["actor"] = principal.subject
            if supervision is not None:
                detail["reason"] = supervision
            element = schema.data_elements.get(element_id)
            _audit.append(
                EventType.INSTANCE_DATA_SET,
                updated.id,
                updated.schema_id,
                schema_version=updated.schema_version,
                label=element.name if element is not None else element_id,
                agent_id=principal.agent_id,
                detail=detail,
            )
    return dict(updated.data_values)


# --- External-task runtime (outbound integration boundary, roadmap E11) ----
# (endpoints: procworks/api_v1.py)


# Worker-supplied numbers are bounded like the modelled ones (``model.MAX_*``):
# a lock or back-off of ``10**400`` ms could not be turned into a point in
# time (the request failed instead of answering 422), and ``10**13`` parked a
# task for centuries. Out-of-range values are refused before anything is
# touched, naming the field and the bound; the bound itself is accepted.


class FetchAndLockRequest(BaseModel):
    """Body of ``POST /v1/external-tasks/fetch-and-lock``.

    ``lock_ms`` is how long the fetched tasks stay with this worker,
    1 ms .. one day (:data:`~procworks.model.MAX_LOCK_MS`).
    """

    worker_id: str = Field(..., examples=["worker-1"])
    topics: list[str] = Field(..., examples=[["invoice-check"]])
    lock_ms: int = Field(default=300_000, ge=1, le=MAX_LOCK_MS, examples=[300000])
    max_tasks: int = Field(default=1, ge=1, examples=[10])
    use_priority: bool = True


class CompleteTaskRequest(BaseModel):
    worker_id: str = Field(..., examples=["worker-1"])
    variables: dict[str, object] = Field(
        default_factory=dict, examples=[{"approved": True}]
    )


class FailureRequest(BaseModel):
    """Body of ``POST /v1/external-tasks/{id}/failure``.

    ``retries`` (absolute remaining budget) is at most the modelled bound
    :data:`~procworks.model.MAX_RETRY_MAX`; ``retry_timeout_ms`` (the wait
    before the task is offered again) at most one day,
    :data:`~procworks.model.MAX_RETRY_BACKOFF_MS`. ``None`` keeps the step's
    modelled behaviour.
    """

    worker_id: str = Field(..., examples=["worker-1"])
    error_message: str = Field(..., examples=["connector timeout"])
    retries: int | None = Field(default=None, ge=0, le=MAX_RETRY_MAX, examples=[3])
    retry_timeout_ms: int | None = Field(
        default=None, ge=0, le=MAX_RETRY_BACKOFF_MS, examples=[5000]
    )


class BpmnErrorRequest(BaseModel):
    worker_id: str = Field(..., examples=["worker-1"])
    error_code: str = Field(..., examples=["INSUFFICIENT_FUNDS"])


class ExtendLockRequest(BaseModel):
    """Body of ``POST /v1/external-tasks/{id}/extend-lock``.

    ``lock_ms`` is the new lock window from now, 1 ms .. one day
    (:data:`~procworks.model.MAX_LOCK_MS`); a longer job extends again.
    """

    worker_id: str = Field(..., examples=["worker-1"])
    lock_ms: int = Field(..., ge=1, le=MAX_LOCK_MS, examples=[60000])


class WorkerRequest(BaseModel):
    worker_id: str = Field(..., examples=["worker-1"])


# --- Data connectors (registry test / sample read, roadmap P3) -------------
# (endpoints: procworks/api_v1.py)


class ConnectorInfo(BaseModel):
    connector_id: str = Field(..., examples=["erp"])
    kind: ConnectorKind = Field(..., examples=[ConnectorKind.MS_SQL])


class ConnectorTestResult(BaseModel):
    connector_id: str
    ok: bool


class SampleReadRequest(BaseModel):
    entity: str = Field(..., examples=["Kunde"])
    limit: int = Field(default=1, ge=1, le=100, examples=[5])


class ColumnInfo(BaseModel):
    column: str = Field(..., examples=["name"])
    sql_type: str = Field(..., examples=["VARCHAR(200)"])
    data_type: DataType | None = Field(default=None, examples=[DataType.STRING])


def _require_connector(connector_id: str) -> None:
    if not _connections.has(connector_id):
        raise HTTPException(
            status_code=404,
            detail={"message": f"connector '{connector_id}' is not configured"},
        )


def _require_safe_entity_name(entity: str) -> None:
    """Refuse an unsafe or system-catalogue table name at the boundary (422).

    The boundary face of :func:`procworks.dal.check_entity_name`, the one check
    the modelled bindings and the run time use as well. Called before any
    connector is asked, so a refused name never reaches the external system --
    also not for a connector (in-memory, custom) that would not check itself.

    :param entity: the table name from the request.
    :raises HTTPException: 422 via :func:`_unsafe_name_422`.
    """

    try:
        check_entity_name(entity)
    except UnsafeIdentifierError as err:
        raise _unsafe_name_422(err, entity) from err


def _unsafe_name_422(err: UnsafeIdentifierError, name: str) -> HTTPException:
    """The 422 answer for a refused table or column name.

    :param err: the DAL's refusal; its ``code`` (``DAL.unsafe-name`` or
        ``DAL.system-entity``) lets the client word it from its catalog.
    :param name: the name the caller sent, as ``params.name`` for that text.
    :returns: the exception to raise (``detail`` = ``message``, ``code``,
        ``params``).
    """

    return HTTPException(
        status_code=422,
        detail={"message": str(err), "code": err.code, "params": {"name": name}},
    )


def _check_sample_entity(connector_id: str, entity: str) -> None:
    """Admit only an entity the connector itself offers (sample-read allowlist).

    The sample read offers exactly what ``/entities`` offers -- the business
    tables -- and never a system catalogue such as ``sqlite_master``. Rules:

    * unsafe names and system catalogues → 422 with code, always, also when
      schema-qualified or in another spelling (``main.sqlite_master``,
      ``PG_CATALOG.pg_authid``; :func:`_require_safe_entity_name`);
    * an unqualified name must be in the connector's catalogue
      (``_connections.entities``) → 422 otherwise;
    * a schema-qualified name (``schema.table``) is admitted when it passed the
      first rule -- SQL catalogues list only the default schema, and a
      customer integration regularly lives in another one;
    * a connector that cannot introspect (empty catalogue) keeps the manual
      entry that worked before.

    A catalogue read that fails is the external system's fault (502).
    """

    _require_safe_entity_name(entity)
    try:
        catalogue = _connections.entities(connector_id)
    except DataAccessError as err:
        raise HTTPException(status_code=502, detail={"message": str(err)}) from err
    if catalogue and "." not in entity and entity not in catalogue:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"Die Tabelle „{entity}“ bietet dieser Connector nicht an. "
                    "Bitte eine der vorgeschlagenen Tabellen wählen."
                )
            },
        )


# --- Webhooks (event side of the open API, roadmap P4/E13) -----------------
# (endpoints: procworks/api_v1.py)


class WebhookCreateRequest(BaseModel):
    url: str = Field(..., examples=["https://hooks.example.com/procworks"])
    events: list[str] = Field(..., examples=[["instance.completed", "task.incident"]])
    secret_ref: str = Field(
        default="", examples=["WEBHOOK_SECRET"], description="Server-side secret name"
    )


def _run_webhook(action: Callable[[], object]) -> object:
    """Execute a webhook action, mapping WebhookError to an HTTP error."""

    try:
        return action()
    except WebhookError as err:
        # Code und Parameter reisen mit, damit der Client den Befund in seiner
        # Sprache formulieren kann (Meldungskatalog); ``message`` bleibt die
        # unveraenderte technische Basis.
        detail: dict[str, object] = {"message": err.message}
        if err.code:
            detail["code"] = err.code
            detail["params"] = err.params
        raise HTTPException(status_code=err.status, detail=detail) from err


class WebhookPreviewRequest(BaseModel):
    url: str = Field(..., examples=["https://hooks.example.com/procworks"])
    event: str = Field(default="task.completed", examples=["task.completed"])
    secret_ref: str = Field(default="", examples=["WEBHOOK_SECRET"])


class WebhookPreviewResponse(BaseModel):
    """Verdict + exact request of a delivery that is NOT sent (see ``preview_delivery``)."""

    allowed: bool
    reason: str
    resolved_address: str
    #: Port der Zustellung (Standardport ohne Angabe, ``:080`` = 80); 0, wenn
    #: das Ziel abgelehnt ist. Additiv -- aeltere Clients ignorieren es.
    resolved_port: int = 0
    egress_locked: bool
    would_send: bool
    signed: bool
    headers: dict[str, str]
    body: str
    #: Ablehnungsgrund sprachneutral (Code + Parameter), damit der Client ihn
    #: formulieren kann; ``reason`` bleibt der technische Satz.
    reason_code: str = ""
    reason_params: dict[str, str] = Field(default_factory=dict)
    #: Angefragte Secret-Referenz und ob der Server sie kennt -- ohne beides war
    #: "nicht signiert" eine Sackgasse (siehe ``DeliveryPreview``).
    secret_ref: str = ""
    secret_known: bool = False


def _raise_webhook_404(subscription_id: str) -> object:
    raise WebhookError(f"subscription '{subscription_id}' not found", 404)


# --- endpoint modules ------------------------------------------------------
#
# The endpoints live in flat per-area modules (procworks/api_<area>.py). They
# read the shared state and helpers of this module at call time, so they are
# imported only here, after every definition they use. The include order is
# the route order: it decides which route matches first and the path order of
# the OpenAPI document.
from procworks import (  # noqa: E402 -- needs every definition above (import cycle)
    api_admin,
    api_changes,
    api_execution,
    api_licensing,
    api_modelling,
    api_monitoring,
    api_org,
    api_schemas,
    api_v1,
)

app.include_router(api_admin.router)
app.include_router(api_schemas.router)
app.include_router(api_org.router)
app.include_router(api_licensing.router)
app.include_router(api_modelling.router)
app.include_router(api_execution.router)
app.include_router(api_changes.router)
app.include_router(api_monitoring.router)
app.include_router(api_v1.instance_data_router)
app.include_router(api_v1.router)


class _ApiPrefixShim:
    """ASGI shim: strip a leading ``/api`` segment from request paths.

    In the single-container demo the SPA is served from the *same* origin as the
    API and computes its API base as ``origin + "/api"`` -- its full-stack
    convention, where Caddy routes ``/api`` to the API and ``/`` to the SPA. This
    process instead serves the API at root, so without this shim every
    ``/api/...`` call from the co-served SPA would 404 (login, schemas, ...).

    Installed **only** when the SPA is co-served (:func:`_maybe_mount_web`), i.e.
    exactly the single-container demo; a regular deployment never mounts the SPA
    here and so never installs it. Static SPA paths (``/``, ``/app.js`` ...) and
    ``/docs`` carry no ``/api`` prefix and pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/api" or path.startswith("/api/"):
                stripped = path[len("/api") :] or "/"
                scope = dict(scope)
                scope["path"] = stripped
                if scope.get("raw_path") is not None:
                    scope["raw_path"] = stripped.encode("latin-1")
        await self._app(scope, receive, send)


#: Browser hardening and the caching rule for the SPA. Must equal the headers in
#: ``deploy/Caddyfile`` (SPA block) -- ``test_spa_headers_match_the_caddyfile``
#: and ``test_caddyfile_revalidates_the_spa_but_not_the_api``.
SPA_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' https:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
    ),
    "X-Frame-Options": "DENY",
    # Not a security header, but needed on the same responses: the client is
    # several scripts that must all come from one version. ``no-cache`` lets
    # the browser keep them yet revalidate (ETag -> 304) on every load, so an
    # update never mixes old and new files. Same value as in the Caddyfile.
    "Cache-Control": "no-cache",
}


class _SpaSecurityHeaders:
    """ASGI middleware: add :data:`SPA_SECURITY_HEADERS` to SPA responses.

    Only for the single-container demo, where this process serves the SPA
    itself (:func:`_maybe_mount_web`); behind Caddy the Caddyfile does it.
    The headers are the CSP and ``X-Frame-Options`` (browser hardening) plus
    ``Cache-Control: no-cache``, so the browser revalidates every client
    script and does not keep files of an older version after an update.
    API paths (``/api/...``), ``/docs``, ``/redoc`` and ``/openapi.json`` are
    left alone: Swagger UI loads its scripts from a CDN and would break under
    the CSP, and JSON responses gain nothing from it. The headers are appended,
    not merged -- the static files carry none of them on their own.
    """

    _SKIP = ("/api", "/docs", "/redoc", "/openapi.json")

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if scope["type"] != "http" or path.startswith(self._SKIP):
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                for name, value in SPA_SECURITY_HEADERS.items():
                    headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)


def _maybe_mount_web(target: FastAPI, web_dir: str) -> bool:
    """Mount the static web client at ``/`` when ``web_dir`` is a real directory.

    D0b: optionally serve the static web
    client from this same process, so one container = whole app = one URL --
    the simplest UX for a throw-away cloud demo (no separate Caddy reverse
    proxy). Off by default: regular deployments front the SPA with Caddy and
    leave ``PROCWORKS_WEB_DIR`` unset, so this is a no-op and returns False.

    Must be called LAST (after every router is included) so the mount only
    catches paths no API route -- nor ``/docs``/``/openapi.json`` -- already
    claimed; ``html=True`` then serves ``index.html`` at ``/`` and for unknown
    sub-paths. When it mounts, it also installs :class:`_ApiPrefixShim` so the
    co-served SPA's ``/api/...`` calls reach the root-mounted API. Returns True
    when a mount was added (factored out so the wiring is unit-testable without
    reloading the module).
    """
    web_dir = web_dir.strip()
    if web_dir and os.path.isdir(web_dir):
        target.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
        # Single-container demo only: let the SPA's /api-prefixed calls through.
        target.add_middleware(_ApiPrefixShim)
        # Added last = outermost: sees the original path (still /api-prefixed).
        target.add_middleware(_SpaSecurityHeaders)
        return True
    return False


_maybe_mount_web(app, os.environ.get("PROCWORKS_WEB_DIR", ""))

