# SPDX-License-Identifier: BUSL-1.1
"""Headless HTTP API (FastAPI) for the procworks kernel.

This is the single entry point to the domain core (Section 5.4, API-first):
the same operations are available to any client -- GUI, CLI, other systems --
and every mutation goes through the validate-before-commit path. The GUI has
no privileged side door.

Run locally:
    uvicorn procworks.api:app --reload
Interactive docs at /docs (OpenAPI is generated automatically).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from procworks import (
    __version__,
    adhoc,
    assignment,
    backups,
    demo,
    mail_runtime,
    metrics,
    migration,
    worklist_priority,
)
from procworks import bpmn as bpmn_io
from procworks import execution as exe
from procworks import operations as ops
from procworks import org as org_ops
from procworks import (
    templates as builtin_templates_mod,
)
from procworks.assignment import OpenTask
from procworks.audit import (
    AuditEvent,
    EventType,
    KpiReport,
    ProcessMap,
    compute_kpis,
    create_audit_log,
    discover_process_map,
    instance_timeline,
)
from procworks.auth import (
    INTEGRATION,
    SCOPE_DATA_READ,
    SCOPE_DATA_WRITE,
    SCOPE_EVENTS_SUBSCRIBE,
    SCOPE_INSTANCES_START,
    SCOPE_TASKS_COMPLETE,
    SCOPE_TASKS_FETCH,
    AuthError,
    OpenAuthBackend,
    Principal,
    create_auth_backend,
)
from procworks.auth_password import (
    DEFAULT_ADMIN_LOGIN,
    PasswordAuthBackend,
    PasswordPolicyError,
    UserView,
    user_view,
)
from procworks.bpmn import BpmnError
from procworks.connections import build_connection_registry
from procworks.dal import DataAccessError
from procworks.execution import ExecutionError
from procworks.integration_runtime import ExternalTaskError, ExternalTaskRuntime
from procworks.licensing import (
    LICENSE_PSEUDO_ID,
    AgentLicenseView,
    License,
    LicenseError,
    LicenseManager,
    PendingClaim,
    SlotSummary,
    TimeAnchor,
    create_license_store,
)
from procworks.metrics import ModelReport
from procworks.model import (
    PRIORITY_RANK,
    AbsenceEntry,
    AccessMode,
    AggregateKind,
    AutomationKind,
    Cardinality,
    ConnectorKind,
    DataType,
    ExecutorKind,
    ExternalTask,
    FilterOperator,
    FollowUpMode,
    FollowUpTrigger,
    ImpactUrgency,
    Incident,
    InstanceState,
    LifecycleState,
    MailBinding,
    MailOutboxEntry,
    MailOutboxState,
    NodeState,
    NodeType,
    OrderBy,
    OrgModel,
    ProcessInstance,
    ProcessSchema,
    ProcessTemplate,
    QueryFilter,
    ServiceBinding,
    StaffRule,
    TemplateOrigin,
    TemplateParameter,
    TimeConstraint,
    ValueClass,
    WebhookDelivery,
    WebhookSubscription,
    WidgetKind,
    WorkItemPriority,
    value_matches_type,
)
from procworks.outbox import (
    OutboxDispatcher,
    WebhookError,
    build_push_endpoint_registry,
)
from procworks.store import (
    create_absence_store,
    create_external_task_store,
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
)
from procworks.validator import (
    CorrectnessError,
    ValidationFinding,
    _possible_agents,
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
    """Application lifespan: optionally seed the demo world at boot.

    When ``PROCWORKS_LOAD_DEMO`` is truthy, the built-in demo cosmos is loaded
    once into the (still empty) module singletons, so a throw-away cloud demo
    container comes up *ready* -- no manual ``POST /admin/reset`` needed (see
    docs/Demo-Hosting-Konzept.md, D0a). This is a pure boundary convenience and
    touches no correctness rule; the seed goes through the same
    ``demo.load_demo`` path as the admin reset.

    Idempotent by design: it only seeds when no schema exists yet, so a
    re-entrant lifespan (test client, ``--reload``) or an already-populated
    store is left untouched. Off by default -- without the env var nothing runs.
    """
    if _env_truthy("PROCWORKS_LOAD_DEMO") and not _store.list_ids():
        _seed_demo()
    yield


app = FastAPI(
    title="Process-Core API",
    version=__version__,
    summary="Headless, block-structured process engine kernel (Correctness by Construction).",
    lifespan=_lifespan,
)

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
_context = exe.ExecutionContext(_resolver, _instances)
_audit = create_audit_log()

#: Licensing / agent metering (dormant by default). Without a configured
#: ``PROCWORKS_LICENSE_PUBKEY`` the manager is *not* enforced: every guard is a
#: no-op and no bindings are written, so the whole layer is inert until a
#: licensor key is set. Activation later is only setting that env var. The
#: offline time ratchet is fed by the append-only, hash-chained audit log's
#: newest timestamp; a trusted anchor is embedded back into that same log.
_license_store = create_license_store()


def _seed_demo() -> None:
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
    )


def _write_time_anchor(ts: float, trusted: bool) -> str:
    """Embed a licensing time-ratchet checkpoint into the hash-chained log.

    Returns the new head hash so the anchor can record the chain position that
    witnessed it (tamper evidence, licensing concept §5A.4).
    """

    event = _audit.append(
        EventType.TIME_ANCHOR,
        LICENSE_PSEUDO_ID,
        LICENSE_PSEUDO_ID,
        detail={"hwm": f"{ts:.3f}", "trusted": "true" if trusted else "false"},
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

# Auth is a coarse boundary layer (Auth concept, Variant C). The backend is
# swapped via ``PROCWORKS_AUTH``; the default open backend grants every role and
# leaves ``agent_id`` unbound, so existing clients/tests keep working unchanged.
_auth_backend = create_auth_backend()


def get_principal(request: Request) -> Principal:
    """FastAPI dependency: the verified identity behind the request (401)."""

    try:
        return _auth_backend.authenticate(request.headers.get("Authorization"))
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


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


# Reusable role gates (see Auth concept 3.4). ``viewer`` is the read floor that
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


class _IdempotencyStore:
    """In-memory ``(subject, key) -> response`` cache for inbound retries.

    Mutating ``/v1`` calls may carry an ``Idempotency-Key`` header; a repeated
    key from the same identity replays the first **successful** response without
    re-executing the operation, so a network retry can never start a second
    instance or complete a task twice. Failures are never cached (the caller may
    retry). This is the simple in-process variant; a DB-backed store with a TTL
    is a later, drop-in step (roadmap P2).
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], object] = {}

    def get(self, subject: str, key: str) -> object | None:
        return self._seen.get((subject, key))

    def put(self, subject: str, key: str, response: object) -> None:
        self._seen[(subject, key)] = response


_idempotency = _IdempotencyStore()


def _idempotent(
    principal: Principal, key: str | None, produce: Callable[[], object]
) -> object:
    """Run ``produce`` once per ``(identity, Idempotency-Key)``; replay after."""

    if not key:
        return produce()
    cached = _idempotency.get(principal.subject, key)
    if cached is not None:
        return cached
    result = produce()
    _idempotency.put(principal.subject, key, result)
    return result



def _auth_mode() -> str:
    """Report the active auth backend kind for the client's login UI."""

    if isinstance(_auth_backend, PasswordAuthBackend):
        return "password"
    if isinstance(_auth_backend, OpenAuthBackend):
        return "open"
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



def _resolve_acting_agent(principal: Principal, requested: str | None) -> str | None:
    """Pick the acting agent id, never trusting the request body over identity.

    A *bound* principal (token/JWT) acts only as itself: a divergent
    ``req.agent_id`` is rejected (403). An *unbound* principal (open dev mode)
    falls back to the requested id so the quickstart keeps working -- the core
    BZR check still rejects an ineligible agent with 409.
    """

    if principal.is_bound:
        if requested is not None and requested != principal.agent_id:
            raise HTTPException(
                status_code=403, detail="cannot act on behalf of another agent"
            )
        return principal.agent_id
    return requested


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


class ResetResponse(BaseModel):
    demo_loaded: bool
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


class SerialInsertRequest(BaseModel):
    label: str = Field(..., examples=["Antrag prüfen"])
    after_node_id: str = Field(..., examples=["start"])


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


class SetFormRequest(BaseModel):
    title: str = ""
    fields: list[FormFieldRequest]


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


class StartActivityRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])


class CompleteActivityRequest(BaseModel):
    node_id: str = Field(..., examples=["act_1"])
    data: dict[str, object] = Field(default_factory=dict)
    agent_id: str | None = Field(default=None, examples=["a1"])


class AdhocInsertRequest(BaseModel):
    after_node_id: str = Field(..., examples=["act_1"])
    label: str = Field(..., examples=["Zusatzpruefung"])


class AdhocDeleteRequest(BaseModel):
    node_id: str = Field(..., examples=["act_2"])


class AdhocRenameRequest(BaseModel):
    node_id: str = Field(..., examples=["act_2"])
    label: str = Field(..., examples=["Zusatzpruefung (angepasst)"])


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


# --- helpers -------------------------------------------------------------


def _get_or_404(schema_id: str) -> ProcessSchema:
    schema = _store.get(schema_id)
    if schema is None:
        raise HTTPException(status_code=404, detail=f"schema '{schema_id}' not found")
    return hydrate_org(schema, _org_resolver)


def _persist_schema(schema: ProcessSchema) -> ProcessSchema:
    """Store a schema, clearing hydrated shared-org data first (single source)."""

    _store.put(dehydrate_org(schema))
    return schema


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


def _commit_or_422(result_fn: object) -> ProcessSchema:
    """Execute an operation callable; map CorrectnessError to HTTP 422."""

    try:
        schema = result_fn()  # type: ignore[operator]
    except CorrectnessError as exc:
        raise HTTPException(
            status_code=422,
            detail={"findings": [f.model_dump() for f in exc.findings]},
        ) from exc
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


def _commit_org_or_422(result_fn: object) -> OrgModel:
    """Apply a shared-org change, re-validating every referencing schema.

    The org op is validated for internal consistency (validate-before-commit);
    additionally each schema that references the org is hydrated with the
    *candidate* org and re-validated, so an org edit can never silently break a
    referencing process's staffing. Only if every referencing schema stays
    correct is the new org persisted (atomic across the org boundary).
    """

    try:
        org = result_fn()  # type: ignore[operator]
    except CorrectnessError as exc:
        raise HTTPException(
            status_code=422,
            detail={"findings": [f.model_dump() for f in exc.findings]},
        ) from exc
    if org.id is not None:
        breaking: list[ValidationFinding] = []
        for schema in _schemas_referencing(org.id):
            hydrated = schema.model_copy(update={"org_model": org.model_copy(deep=True)})
            breaking += validate(hydrated, _resolver)
        if breaking:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "org change would break referencing schemas",
                    "findings": [f.model_dump() for f in breaking],
                },
            )
    return _org_store.put(org)


def _get_instance_or_404(instance_id: str) -> ProcessInstance:
    instance = _instances.get(instance_id)
    if instance is None:
        raise HTTPException(
            status_code=404, detail=f"instance '{instance_id}' not found"
        )
    return instance


def _run_or_409(result_fn: object) -> ProcessInstance:
    """Execute a runtime operation callable; map ExecutionError to HTTP 409."""

    try:
        instance = result_fn()  # type: ignore[operator]
    except ExecutionError as exc:
        raise HTTPException(status_code=409, detail={"message": exc.message}) from exc
    return _instances.put(instance)


def _effective_schema_for(instance: ProcessInstance) -> ProcessSchema:
    """Return the schema an instance currently runs against.

    Ad-hoc changed instances carry their own per-instance variant
    (``ad_hoc_schema``); everything else runs against the released base schema.
    """

    base = _get_or_404(instance.schema_id)
    return hydrate_org(adhoc.effective_schema(instance, base), _org_resolver)


def _emit_event(event_type: str, payload: dict[str, object]) -> None:
    """Enqueue a domain event on the outbox and attempt best-effort delivery.

    The webhook side of the open API (E13): emission is transactional (the entry
    is queued before delivery), and dispatch is attempted synchronously so a
    healthy receiver is notified promptly. Delivery never blocks or fails the
    triggering request -- exhausted retries become dead-letters instead.
    """

    _outbox.emit(event_type, payload)
    _outbox.dispatch_pending()


def _push_external(binding: ServiceBinding, payload: dict[str, object]) -> None:
    """Deliver an ``HTTP_PUSH`` activity package to its tool endpoint (E11/E13).

    Resolves the binding's ``endpoint_ref`` to a server-configured, trusted URL
    (and optional signing secret), then enqueues the push on the same robust
    outbox used for webhooks (durable, signed, retried, circuit-broken). Raising
    on an unresolved/blocked endpoint leaves the task pending so it is retried on
    a later drive -- the engine is never affected.
    """

    target = _push_endpoints.resolve(binding.endpoint_ref or "")
    _outbox.push(
        target.url,
        target.secret_ref,
        "task.push",
        payload,
        max_attempts=binding.retry_max,
    )
    _outbox.dispatch_pending()


def _drive_pushes() -> None:
    """Best-effort push of newly activated ``HTTP_PUSH`` steps after an advance.

    Called after every engine advance (start/complete). It never raises:
    a push problem must not fail the triggering request nor corrupt the instance,
    so failures are swallowed and retried on the next advance.
    """

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
    ``mail.sent`` audit event; an exhausted retry budget records ``mail.failed``.
    Only **metadata** is logged (recipient count, attempts, error), never the
    address list or the body (DSGVO data minimisation, concept §8). Transient
    failures stay silent -- they will be retried on a later drive. Never raises.
    """

    for result in _mail_outbox.dispatch_pending(_mail_sender):
        entry = result.entry
        instance = _instances.get(entry.instance_id)
        version = instance.schema_version if instance is not None else 1
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
    outbox is driven synchronously so a healthy mail server is notified promptly.
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
    _dispatch_mail_outbox()


def _after_advance(
    schema: ProcessSchema,
    before_states: dict[str, NodeState] | None,
    after: ProcessInstance,
) -> None:
    """Run all boundary side effects of one engine advance, in the right order.

    Stamps the worklist activation clock **before** the mail notification (the
    durable outbox derives its per-activation idempotency key from that stamp),
    drives the ``HTTP_PUSH`` sink, then persists the (stamp-mutated) instance.
    Shared by every runtime-advance path -- the human mainline (start/complete),
    the external-task completion, ad-hoc changes and migration -- so a task that
    becomes ready is notified no matter which path activated it (concept §5, §10).
    """

    _stamp_activations(schema, before_states, after)
    _drive_pushes()
    _notify_ready(schema, before_states, after)
    _instances.put(after)


def _stamp_activations(
    schema: ProcessSchema,
    before_states: dict[str, NodeState] | None,
    after: ProcessInstance,
) -> None:
    """Stamp the runtime clock of the time-based worklist prioritisation.

    Records, per human-task ACTIVITY that *just* became ready (ACTIVATED in
    ``after`` but not before), the current wall-clock into
    ``after.node_activated_at`` -- the origin from which an open task's reaction
    time is measured (Zeitbasierte-Priorisierung-Konzept, Section 4). On a fresh
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
    )


def _run_external(action: Callable[[], object]) -> object:
    """Execute an external-task action, mapping ExternalTaskError to HTTP."""

    try:
        return action()
    except ExternalTaskError as err:
        raise HTTPException(
            status_code=err.status, detail={"message": err.message}
        ) from err



def _commit_instance_or_422(result_fn: object) -> ProcessInstance:
    """Execute an instance change op; map CorrectnessError to HTTP 422."""

    try:
        instance = result_fn()  # type: ignore[operator]
    except CorrectnessError as exc:
        raise HTTPException(
            status_code=422,
            detail={"findings": [f.model_dump() for f in exc.findings]},
        ) from exc
    return _instances.put(instance)



# --- endpoints -----------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/auth/me", response_model=Principal)
def get_me(principal: Principal = Depends(get_principal)) -> Principal:
    """Return the verified identity of the caller (for the client's login UI)."""

    return principal


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
    if mode == "password" and _demo_mode():
        from procworks.demo import DEMO_AUTOLOGIN, DEMO_PASSWORD, DEMO_USERS

        cfg.demo = True
        cfg.demo_password = DEMO_PASSWORD
        cfg.demo_autologin = DEMO_AUTOLOGIN
        cfg.demo_logins = [
            DemoLogin(login=login, name=name, role=next(iter(roles), "viewer"))
            for login, name, roles, _agent in DEMO_USERS
        ]
    return cfg


@app.post("/auth/login", response_model=LoginResponse)
def post_login(req: LoginRequest) -> LoginResponse:
    """Exchange username + password for a session bearer token (password mode)."""

    backend = _password_backend()
    try:
        result = backend.login(req.login, req.password)
    except AuthError as exc:
        raise HTTPException(
            status_code=401, detail=exc.message, headers={"WWW-Authenticate": "Bearer"}
        ) from exc
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
def post_change_password(
    req: ChangePasswordRequest,
    principal: Principal = Depends(get_principal),
) -> Response:
    """Self-service password change; clears the forced-change flag."""

    backend = _password_backend()
    try:
        backend.change_password(
            principal.subject, req.current_password, req.new_password
        )
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=204)


@app.get("/users", response_model=list[UserView], dependencies=[_admin])
def list_users() -> list[UserView]:
    """List login users (admin only); never exposes password hashes."""

    backend = _password_backend()
    return [user_view(u) for u in backend.store.list_users()]


@app.post("/users", response_model=CreateUserResponse, status_code=201, dependencies=[_admin])
def create_user(req: CreateUserRequest) -> CreateUserResponse:
    """Provision a login from an agent; returns the initial password once (admin)."""

    backend = _password_backend()
    display_name = req.display_name
    if display_name is None and req.agent_id is not None:
        display_name = _find_agent_name(req.agent_id)
    subject = req.login or display_name or req.agent_id
    if not subject:
        raise HTTPException(
            status_code=400, detail="need login, display_name or agent_id"
        )
    try:
        user, initial_password = backend.create_user(
            subject=subject,
            roles=req.roles,
            agent_id=req.agent_id,
            login=req.login,
            display_name=display_name,
        )
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
def reset_user_password(login: str) -> ResetPasswordResponse:
    """Set a fresh initial password (forces change); returns it once (admin)."""

    backend = _password_backend()
    try:
        initial_password = backend.reset_password(login)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
    return ResetPasswordResponse(login=login, initial_password=initial_password)


@app.delete("/users/{login}", status_code=204, dependencies=[_admin])
def delete_user(login: str) -> Response:
    """Remove a login user (admin only)."""

    backend = _password_backend()
    backend.store.delete_user(login)
    return Response(status_code=204)


def _wipe_users(keep_logins: set[str]) -> None:
    """Delete every login except the ones in ``keep_logins`` (password mode).

    The acting admin (and any explicitly kept admin) survive a reset so the
    operator stays logged in and the system remains administrable. In open/token
    mode there is no credential store, so there is nothing to wipe.
    """

    if not isinstance(_auth_backend, PasswordAuthBackend):
        return
    for user in _auth_backend.store.list_users():
        if user.login not in keep_logins:
            _auth_backend.store.delete_user(user.login)


def _user_count() -> int:
    if isinstance(_auth_backend, PasswordAuthBackend):
        return len(_auth_backend.store.list_users())
    return 0


@app.post("/admin/reset", response_model=ResetResponse)
def post_admin_reset(
    req: ResetRequest, principal: Principal = Depends(require_role("admin")),
) -> ResetResponse:
    """Wipe all process data to zero, optionally reloading the demo (admin only).

    This is a deliberately destructive maintenance action: every schema,
    instance, audit event and shared org model is removed. In password mode all
    logins are dropped too, except the acting admin and the bootstrap ``admin``
    so nobody gets locked out. With ``load_demo`` the built-in demo world is
    loaded afterwards (the same data that ships for a guided first look).
    """

    _store.clear()
    _instances.clear()
    _org_store.clear()
    _audit.clear()
    _mail_outbox_store.clear()
    _absence_store.clear()
    # Drop user-created templates. Built-in templates are code, so they survive.
    _template_store.clear()
    # Drop licenses/bindings/anchor. The install id deliberately survives (it
    # identifies the installation, and bought packs are bound to it).
    _license_store.clear()

    keep = {DEFAULT_ADMIN_LOGIN}
    if principal.subject:
        keep.add(principal.subject)
    _wipe_users(keep)

    if req.load_demo:
        _seed_demo()

    return ResetResponse(
        demo_loaded=req.load_demo,
        schemas=len(_store.list_ids()),
        instances=len(_instances.list_ids()),
        org_models=len(_org_store.list_ids()),
        users=_user_count(),
    )


class RunNowResponse(BaseModel):
    """Result of asking the backup scheduler to run now."""

    requested: bool = Field(description="True when the .run-now marker was written")


@app.get("/admin/backups", response_model=backups.BackupsStatus, dependencies=[_admin])
def get_admin_backups() -> backups.BackupsStatus:
    """List known datensicherungen and their status (admin only, read-only).

    Reads only the metadata index the backup scheduler publishes into the shared
    control directory -- never the dump volume itself (the API has no access to
    the dumps, per the concept's security rule). Reports ``available = false``
    when no control directory is configured or nothing has been published yet,
    so the GUI can show a clear "not configured" state instead of an error.
    """
    return backups.load_status(backups.control_dir())


@app.post("/admin/backups/run-now", response_model=RunNowResponse, dependencies=[_admin])
def post_admin_backup_run_now() -> RunNowResponse:
    """Ask the scheduler to take a backup now (admin only).

    The API never runs ``pg_dump`` itself; it only drops a ``.run-now`` marker in
    the shared control directory, which the scheduler polls (file-based handoff,
    no coupling, no database dump rights in the API). Returns HTTP 503 when the
    backup control surface is not wired for this deployment, and HTTP 500 if the
    marker cannot be written (e.g. a read-only control volume).
    """
    directory = backups.control_dir()
    if directory is None:
        raise HTTPException(
            status_code=503,
            detail="Backup control directory is not configured for this deployment.",
        )
    try:
        backups.request_run_now(directory)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not write the backup trigger: {exc}"
        ) from exc
    return RunNowResponse(requested=True)


class MailOutboxEntryView(BaseModel):
    """Read-only, data-minimised projection of a queued notification (admin).

    Deliberately omits the recipient address list and the rendered body (DSGVO
    data minimisation, concept §8): the ops view needs the delivery *state*, not
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
        entries=[_mail_outbox_view(entry) for entry in entries],
    )


@app.get("/admin/mail-outbox", response_model=MailOutboxStatus, dependencies=[_admin])
def get_admin_mail_outbox() -> MailOutboxStatus:
    """Read the durable mail outbox: queued notifications and their state (admin).

    Read-only ops view of the modelled e-mail notifications (rule group N). Shows
    per-state counts and the (data-minimised) entries so an operator can see what
    is pending, being retried, delivered or dead-lettered -- and whether an SMTP
    sender is configured at all. Never exposes recipient addresses or the body.
    """

    return _mail_outbox_status()


@app.post(
    "/admin/mail-outbox/dispatch", response_model=MailOutboxStatus, dependencies=[_admin]
)
def post_admin_mail_outbox_dispatch() -> MailOutboxStatus:
    """Retry all due queued notifications now, then return the outbox (admin).

    A manual flush for operators: after fixing an SMTP outage, this drives the
    durable outbox once so ``PENDING``/``FAILED`` entries are re-attempted (and
    ``mail.sent``/``mail.failed`` audit events are recorded) without waiting for
    the next process advance. ``DEAD`` entries stay dead-lettered.
    """

    _dispatch_mail_outbox()
    return _mail_outbox_status()


@app.get("/schemas", dependencies=[_read])
def list_schemas() -> list[str]:
    return _store.list_ids()


@app.post(
    "/schemas", response_model=ProcessSchema, status_code=201, dependencies=[_model]
)
def create_schema(req: CreateSchemaRequest) -> ProcessSchema:
    return _commit_or_422(lambda: ops.create_empty_schema(req.name))


@app.get("/templates", response_model=list[TemplateSummary], dependencies=[_read])
def list_templates() -> list[TemplateSummary]:
    """List all process templates (built-in library + saved user templates).

    Returns lightweight summaries; fetch a single template or instantiate it to
    obtain the full blueprint. Built-in templates come first, user templates
    after, each block sorted by name for a stable gallery order.
    """

    templates = list(_all_templates().values())
    templates.sort(key=lambda t: (t.origin is not TemplateOrigin.BUILTIN, t.name.lower()))
    return [
        TemplateSummary(
            id=t.id,
            name=t.name,
            description=t.description,
            category=t.category,
            origin=t.origin,
        )
        for t in templates
    ]


@app.get("/templates/{template_id}", response_model=ProcessTemplate, dependencies=[_read])
def get_template(template_id: str) -> ProcessTemplate:
    """Fetch a single template including its full schema blueprint."""

    return _get_template_or_404(template_id)


@app.post(
    "/templates", response_model=ProcessTemplate, status_code=201, dependencies=[_model]
)
def save_template(req: SaveTemplateRequest) -> ProcessTemplate:
    """Save an existing schema as a reusable *user* template (modeller/admin).

    The source schema is captured as a self-contained, validated blueprint (its
    org master data is embedded and it is reset to a clean draft). Only correct
    schemas can be captured -- the operation runs the full validation, so a
    broken blueprint is rejected with HTTP 422 (validate-before-commit).
    """

    schema = _get_or_404(req.schema_id)
    try:
        template = ops.save_as_template(
            schema,
            name=req.name,
            description=req.description,
            category=req.category,
            origin=TemplateOrigin.USER,
        )
    except CorrectnessError as exc:
        raise HTTPException(
            status_code=422,
            detail={"findings": [f.model_dump() for f in exc.findings]},
        ) from exc
    return _template_store.put(template)


@app.post(
    "/templates/{template_id}/instantiate",
    response_model=ProcessSchema,
    status_code=201,
    dependencies=[_model],
)
def instantiate_template(
    template_id: str, req: InstantiateTemplateRequest
) -> ProcessSchema:
    """Create a fresh, editable draft schema from a template (modeller/admin).

    Deep-copies the template blueprint into a new schema with a fresh id and
    ``ENTWURF`` state; the modeller edits and releases it like any other draft.
    """

    template = _get_template_or_404(template_id)
    return _commit_or_422(lambda: ops.instantiate_template(template, name=req.name))


@app.delete("/templates/{template_id}", status_code=204, dependencies=[_model])
def delete_template(template_id: str) -> Response:
    """Delete a *user* template (modeller/admin). Built-in templates are code.

    Deleting a built-in template is rejected with HTTP 422 -- it ships with the
    product and cannot be removed. A missing template yields HTTP 404.
    """

    if _template_store.get(template_id) is None:
        # Not a user template: either a built-in (refuse) or unknown (404).
        _get_template_or_404(template_id)  # raises 404 if truly unknown
        raise HTTPException(
            status_code=422, detail="built-in templates cannot be deleted"
        )
    _template_store.delete(template_id)
    return Response(status_code=204)


@app.get("/schemas/{schema_id}", response_model=ProcessSchema, dependencies=[_read])
def get_schema(schema_id: str) -> ProcessSchema:
    return _get_or_404(schema_id)


@app.get(
    "/schemas/{schema_id}/validation",
    response_model=ValidationReport,
    dependencies=[_read],
)
def get_validation(schema_id: str) -> ValidationReport:
    schema = _get_or_404(schema_id)
    findings = validate(schema, _resolver)
    return ValidationReport(correct=not findings, findings=findings)


@app.get(
    "/schemas/{schema_id}/metrics",
    response_model=ModelReport,
    dependencies=[_read],
)
def get_metrics(schema_id: str) -> ModelReport:
    """Read-only model metrics, 7PMG hints and value-class mix (roadmap E7/E3).

    These figures are advisory only and never affect Stufe-A/B correctness.
    """

    schema = _get_or_404(schema_id)
    return metrics.model_report(schema)


@app.post(
    "/schemas/{schema_id}/serial-insert",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_serial_insert(schema_id: str, req: SerialInsertRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.serial_insert(schema, req.label, req.after_node_id))


@app.post(
    "/schemas/{schema_id}/parallel-insert",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_parallel_insert(schema_id: str, req: ParallelInsertRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.parallel_insert(schema, req.branch_labels, req.after_node_id)
    )


@app.post(
    "/schemas/{schema_id}/conditional-insert",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_conditional_insert(schema_id: str, req: ConditionalInsertRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    branches = [
        ops.BranchSpec(
            label=b.label,
            upper=b.upper,
            bool_value=b.bool_value,
            values=tuple(b.values),
            is_else=b.is_else,
        )
        for b in req.branches
    ]
    return _commit_or_422(
        lambda: ops.conditional_insert(
            schema, req.after_node_id, discriminator=req.discriminator, branches=branches
        )
    )


@app.patch(
    "/schemas/{schema_id}/nodes/{node_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def patch_rename_node(
    schema_id: str, node_id: str, req: RenameNodeRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.rename_node(schema, node_id, req.label))


@app.delete(
    "/schemas/{schema_id}/nodes/{node_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_schema_node(schema_id: str, node_id: str) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.delete_node(schema, node_id))


@app.post(
    "/schemas/{schema_id}/nodes/{node_id}/remove-empty-branch",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_remove_empty_branch(schema_id: str, node_id: str) -> ProcessSchema:
    """Remove the empty branch of the XOR split ``node_id`` (validate-before-commit).

    ``node_id`` is the XOR_SPLIT that currently carries one empty ``split ->
    join`` branch. Dissolves the whole gateway when only the non-empty branch
    would remain, otherwise drops just the empty cell; an invalid result (e.g. a
    lost catch-all) is rejected with HTTP 422.
    """

    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.remove_empty_branch(schema, node_id))


@app.post(
    "/schemas/{schema_id}/data-elements",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_add_data_element(schema_id: str, req: AddDataElementRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.add_data_element(schema, req.name, req.data_type, req.element_id)
    )


@app.patch(
    "/schemas/{schema_id}/data-elements/{element_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def patch_data_element(
    schema_id: str, element_id: str, req: UpdateDataElementRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.update_data_element(
            schema, element_id, name=req.name, data_type=req.data_type
        )
    )


@app.delete(
    "/schemas/{schema_id}/data-elements/{element_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_schema_data_element(schema_id: str, element_id: str) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.delete_data_element(schema, element_id))


@app.post(
    "/schemas/{schema_id}/data-elements/{element_id}/reset-source",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_reset_data_element_source(schema_id: str, element_id: str) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.reset_data_element_source(schema, element_id))


@app.post(
    "/schemas/{schema_id}/data-access",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_connect_data(schema_id: str, req: ConnectDataRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.connect_data(
            schema,
            req.node_id,
            req.element_id,
            req.mode,
            mandatory=req.mandatory,
            param_type=req.param_type,
        )
    )


@app.delete(
    "/schemas/{schema_id}/data-access/{node_id}/{element_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_data_access(
    schema_id: str, node_id: str, element_id: str, mode: AccessMode | None = None
) -> ProcessSchema:
    """Remove a data binding of ``element_id`` from ``node_id``.

    Without ``mode`` every access of the element on that node is removed; with
    ``mode`` only that direction. The core re-checks D1-D4 (e.g. removing the
    sole writer behind a mandatory read elsewhere is rejected with HTTP 422).
    """
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.disconnect_data(schema, node_id, element_id, mode)
    )


@app.post(
    "/schemas/{schema_id}/nodes/{node_id}/form",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_form(
    schema_id: str, node_id: str, req: SetFormRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    specs = [
        ops.FormFieldSpec(
            element_id=f.element_id,
            widget=f.widget,
            label=f.label,
            mode=f.mode,
            required=f.required,
            options=tuple(f.options),
            help_text=f.help_text,
        )
        for f in req.fields
    ]
    return _commit_or_422(
        lambda: ops.set_form(schema, node_id, title=req.title, fields=specs)
    )


@app.delete(
    "/schemas/{schema_id}/nodes/{node_id}/form",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_schema_form(schema_id: str, node_id: str) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.delete_form(schema, node_id))


@app.post(
    "/schemas/{schema_id}/connectors",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_register_connector(
    schema_id: str, req: RegisterConnectorRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.register_connector(
            schema, req.name, req.kind, connector_id=req.connector_id
        )
    )


@app.post(
    "/schemas/{schema_id}/data-elements/{element_id}/external",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_bind_external_data(
    schema_id: str, element_id: str, req: BindExternalDataRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.bind_external_data(
            schema,
            element_id,
            connector_id=req.connector_id,
            entity=req.entity,
            key_element_id=req.key_element_id,
        )
    )


@app.post(
    "/schemas/{schema_id}/data-elements/{element_id}/sql-select",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_bind_sql_select(
    schema_id: str, element_id: str, req: SqlSelectRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    filters = [
        QueryFilter(
            column=f.column,
            column_type=f.column_type,
            operator=f.operator,
            key_element_id=f.key_element_id,
        )
        for f in req.filters
    ]
    order_by = [OrderBy(column=o.column, descending=o.descending) for o in req.order_by]
    return _commit_or_422(
        lambda: ops.bind_sql_select(
            schema,
            element_id,
            connector_id=req.connector_id,
            entity=req.entity,
            column=req.column,
            column_type=req.column_type,
            aggregate=req.aggregate,
            filters=filters,
            cardinality=req.cardinality,
            order_by=order_by,
            unique_column=req.unique_column,
        )
    )


@app.post(
    "/schemas/{schema_id}/data-elements/{element_id}/sql-write",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_bind_sql_write(
    schema_id: str, element_id: str, req: SqlWriteRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    filters = [
        QueryFilter(
            column=f.column,
            column_type=f.column_type,
            operator=f.operator,
            key_element_id=f.key_element_id,
        )
        for f in req.filters
    ]
    return _commit_or_422(
        lambda: ops.bind_sql_write(
            schema,
            element_id,
            connector_id=req.connector_id,
            entity=req.entity,
            column=req.column,
            column_type=req.column_type,
            filters=filters,
            unique_column=req.unique_column,
        )
    )


@app.get("/schemas/{schema_id}/bpmn", dependencies=[_read])
def get_export_bpmn(schema_id: str) -> Response:
    schema = _get_or_404(schema_id)
    return Response(content=bpmn_io.export_bpmn(schema), media_type="application/xml")


@app.post(
    "/bpmn-import",
    response_model=ProcessSchema,
    status_code=201,
    dependencies=[_model],
)
def post_import_bpmn(req: ImportBpmnRequest) -> ProcessSchema:
    try:
        schema = bpmn_io.import_bpmn(
            req.xml, schema_id=req.schema_id, name=req.name, resolver=_resolver
        )
    except BpmnError as exc:
        raise HTTPException(
            status_code=422, detail={"message": str(exc)}
        ) from exc
    except CorrectnessError as exc:
        raise HTTPException(
            status_code=422,
            detail={"findings": [f.model_dump() for f in exc.findings]},
        ) from exc
    return _store.put(schema)


# --- shared, cross-schema organisation models ---------------------------


@app.get("/org-models", response_model=list[OrgModel], dependencies=[_read])
def get_org_models() -> list[OrgModel]:
    return [org for oid in _org_store.list_ids() if (org := _org_store.get(oid)) is not None]


@app.post(
    "/org-models", response_model=OrgModel, status_code=201, dependencies=[_admin]
)
def post_create_org_model(req: CreateOrgModelRequest) -> OrgModel:
    org = org_ops.create_org_model(req.name, org_id=req.org_model_id)
    return _org_store.put(org)


@app.get("/org-models/{org_id}", response_model=OrgModel, dependencies=[_read])
def get_org_model(org_id: str) -> OrgModel:
    return _get_org_or_404(org_id)


@app.post("/org-models/{org_id}/roles", response_model=OrgModel, dependencies=[_admin])
def post_org_add_role(org_id: str, req: AddRoleRequest) -> OrgModel:
    org = _get_org_or_404(org_id)
    return _commit_org_or_422(lambda: org_ops.org_add_role(org, req.name, role_id=req.role_id))


@app.post(
    "/org-models/{org_id}/org-units", response_model=OrgModel, dependencies=[_admin]
)
def post_org_add_unit(org_id: str, req: AddOrgUnitRequest) -> OrgModel:
    org = _get_org_or_404(org_id)
    return _commit_org_or_422(
        lambda: org_ops.org_add_unit(
            org,
            req.name,
            parent_id=req.parent_id,
            org_unit_id=req.org_unit_id,
            manager_id=req.manager_id,
        )
    )


@app.post(
    "/org-models/{org_id}/org-units/{org_unit_id}/manager",
    response_model=OrgModel,
    dependencies=[_admin],
)
def post_org_set_manager(org_id: str, org_unit_id: str, req: SetManagerRequest) -> OrgModel:
    org = _get_org_or_404(org_id)
    return _commit_org_or_422(lambda: org_ops.org_set_manager(org, org_unit_id, req.manager_id))


@app.post(
    "/org-models/{org_id}/org-units/{org_unit_id}/parent",
    response_model=OrgModel,
    dependencies=[_admin],
)
def post_org_set_parent(org_id: str, org_unit_id: str, req: SetParentRequest) -> OrgModel:
    org = _get_org_or_404(org_id)
    return _commit_org_or_422(lambda: org_ops.org_set_parent(org, org_unit_id, req.parent_id))


@app.post("/org-models/{org_id}/agents", response_model=OrgModel, dependencies=[_admin])
def post_org_add_agent(org_id: str, req: AddAgentRequest) -> OrgModel:
    org = _get_org_or_404(org_id)
    # Licensing guard (dormant unless enforced): creating an agent beyond the
    # covered contingent is a purchase offer, not an error (HTTP 402). No effect
    # while licensing is off; then no quota applies and no binding is written.
    before_ids = _all_agent_ids()
    if _license.enforced and not _license.can_create_agent(before_ids):
        raise HTTPException(
            status_code=402,
            detail=(
                "Agenten-Kontingent ausgeschöpft – bitte ein Agenten-Paket "
                "(+5 Agenten / 1 Jahr) hinzubuchen."
            ),
        )
    updated = _commit_org_or_422(
        lambda: org_ops.org_add_agent(
            org,
            req.name,
            role_ids=req.role_ids,
            org_unit_id=req.org_unit_id,
            agent_id=req.agent_id,
            deputy_id=req.deputy_id,
            email=req.email,
        )
    )
    if _license.enforced:
        # Bind exactly the agent(s) just added (set difference vs. the earlier
        # universe) to spare capacity, so the new agent counts against a slot.
        all_ids = _all_agent_ids()
        for new_id in set(updated.agents) - before_ids:
            try:
                _license.auto_bind_new_agent(new_id, all_ids)
            except LicenseError:
                # Quota was checked above; a race here should not fail the write.
                break
    return updated


# --- Licensing / agent metering (dormant unless enforced) ------------------
#
# All of these endpoints exist unconditionally. While licensing is off
# (``_license.enforced == False``) they still answer, reporting the free
# contingent, so the web client can render the agent page uniformly. Only when a
# licensor public key is configured do the mutating actions actually gate work.


class ActivateLicenseRequest(BaseModel):
    """A signed license token (base64-of-JSON or raw JSON) to install."""

    token: str


class CheckoutRequest(BaseModel):
    """Requested pack size / duration for a purchase (defaults per concept)."""

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
    """Optional trusted timestamp (epoch seconds) from a signed time source."""

    trusted_now: float | None = None


@app.get("/license/status", response_model=SlotSummary, dependencies=[_read])
def get_license_status() -> SlotSummary:
    """Contingent overview (slots used/total, next expiry, install id)."""

    return _license.summary(_all_agent_ids())


@app.get("/license/agents", response_model=list[AgentLicenseView], dependencies=[_read])
def get_license_agents() -> list[AgentLicenseView]:
    """Per-agent licensing badge data for the agent page."""

    if _license.enforced:
        _license.reconcile(_all_agent_ids())
    return _license.agent_views(sorted(_all_agent_ids()))


@app.post("/license/checkout", response_model=CheckoutResponse, dependencies=[_model])
def post_license_checkout(req: CheckoutRequest) -> CheckoutResponse:
    """Start a purchase: hand back the hosted checkout URL and the install id.

    The actual payment/issuance runs on a *separate* licensor backend (never on
    this self-hosted instance). Its base URL is configured via
    ``PROCWORKS_LICENSE_CHECKOUT_URL``; without it the product reports that
    self-service purchase is not configured (self-hosted, offline by default).
    """

    base = os.environ.get("PROCWORKS_LICENSE_CHECKOUT_URL", "").strip()
    install_id = _license.install_id()
    if not base:
        return CheckoutResponse(
            checkout_url=None,
            install_id=install_id,
            message=(
                "Self-Service-Kauf ist für diese Installation nicht konfiguriert. "
                "Bitte den Anbieter kontaktieren."
            ),
        )
    # Optional online auto-pull: when a claim endpoint is configured and
    # licensing is enforced, mint a single-use claim, hand its token to the
    # licensor via the deep-link and let the instance fetch the issued pack
    # itself. Otherwise the response omits the claim and the operator uses the
    # manual copy-&-paste activation -- both paths stay fully additive.
    claim_base = os.environ.get("PROCWORKS_LICENSE_CLAIM_URL", "").strip()
    claim: PendingClaim | None = None
    if claim_base and _license.enforced:
        claim = _license.new_claim(claim_base, slots=req.slots, months=req.months)
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}install_id={install_id}&slots={req.slots}&months={req.months}"
    if claim is not None:
        url = f"{url}&claim_token={claim.claim_token}"
    return CheckoutResponse(
        checkout_url=url,
        install_id=install_id,
        message="Kauf im Browser abschließen; die Lizenz wird danach eingespielt.",
        claim_token=claim.claim_token if claim is not None else None,
        poll_url=claim.poll_url if claim is not None else None,
    )


@app.post("/license/claims/poll", response_model=ClaimPollResult, dependencies=[_read])
def post_license_claims_poll() -> ClaimPollResult:
    """Run one best-effort auto-pull pass and report the resulting contingent.

    The web client calls this after starting a checkout (and on the license
    page) to fetch and activate a freshly issued pack without copy-&-paste. It
    never blocks a process step and is a no-op while licensing is not enforced
    or no claim endpoint is configured (there are simply no open claims then).
    """

    activated = _license.poll_claims(_claim_fetcher)
    pending = len(_license.pending_claims())
    return ClaimPollResult(
        activated=len(activated),
        pending=pending,
        summary=_license.summary(_all_agent_ids()),
    )


@app.post("/license/activate", response_model=License, dependencies=[_admin])
def post_license_activate(req: ActivateLicenseRequest) -> License:
    """Install a signed license token (verifies signature + install binding)."""

    try:
        return _license.activate(req.token)
    except LicenseError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.post("/license/refresh-time", response_model=TimeAnchor, dependencies=[_read])
def post_license_refresh_time(req: RefreshTimeRequest) -> TimeAnchor:
    """Advance the offline time ratchet (optionally from a trusted timestamp)."""

    return _license.refresh_time(req.trusted_now)


@app.post(
    "/agents/{agent_id}/bind-license",
    response_model=AgentLicenseView,
    dependencies=[_model],
)
def post_bind_agent_license(agent_id: str, req: BindLicenseRequest) -> AgentLicenseView:
    """Re-home an agent onto another (valid, non-full) license contingent."""

    if agent_id not in _all_agent_ids():
        raise HTTPException(status_code=404, detail="unknown agent")
    try:
        _license.bind(agent_id, req.license_id, _all_agent_ids())
    except LicenseError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return _license.agent_view(agent_id)


@app.patch(
    "/org-models/{org_id}/agents/{agent_id}",
    response_model=OrgModel,
    dependencies=[_admin],
)
def patch_org_update_agent(org_id: str, agent_id: str, req: UpdateAgentRequest) -> OrgModel:
    org = _get_org_or_404(org_id)
    org_unit = req.org_unit_id if "org_unit_id" in req.model_fields_set else org_ops.KEEP
    email = req.email if "email" in req.model_fields_set else org_ops.KEEP
    return _commit_org_or_422(
        lambda: org_ops.org_update_agent(
            org,
            agent_id,
            name=req.name,
            role_ids=req.role_ids,
            org_unit_id=org_unit,
            email=email,
        )
    )


@app.post(
    "/org-models/{org_id}/agents/{agent_id}/deputy",
    response_model=OrgModel,
    dependencies=[_admin],
)
def post_org_set_deputy(org_id: str, agent_id: str, req: SetDeputyRequest) -> OrgModel:
    org = _get_org_or_404(org_id)
    return _commit_org_or_422(lambda: org_ops.org_set_deputy(org, agent_id, req.deputy_id))


@app.put(
    "/org-models/{org_id}/roles/{role_id}/mailbox",
    response_model=OrgModel,
    dependencies=[_admin],
)
def put_org_role_mailbox(org_id: str, role_id: str, req: SetMailboxRequest) -> OrgModel:
    """Set (or clear) a role's shared group mailbox (rule group N).

    A malformed address is rejected (N1, HTTP 422). Re-validates every schema
    referencing this org so removing a mailbox that a group notification needs
    (N3) is refused rather than silently breaking a released process.
    """

    org = _get_org_or_404(org_id)
    return _commit_org_or_422(
        lambda: org_ops.org_set_role_mailbox(org, role_id, req.mailbox)
    )


@app.put(
    "/org-models/{org_id}/units/{org_unit_id}/mailbox",
    response_model=OrgModel,
    dependencies=[_admin],
)
def put_org_unit_mailbox(
    org_id: str, org_unit_id: str, req: SetMailboxRequest
) -> OrgModel:
    """Set (or clear) an org unit's department mailbox (rule group N)."""

    org = _get_org_or_404(org_id)
    return _commit_org_or_422(
        lambda: org_ops.org_set_unit_mailbox(org, org_unit_id, req.mailbox)
    )


@app.post(
    "/schemas/{schema_id}/org-model",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_link_org_model(schema_id: str, req: LinkOrgModelRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    org = _get_org_or_404(req.org_model_id)
    return _commit_or_422(lambda: ops.link_org_model(schema, req.org_model_id, org))


@app.delete(
    "/schemas/{schema_id}/org-model",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_unlink_org_model(schema_id: str) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.unlink_org_model(schema))


@app.post("/schemas/{schema_id}/roles", response_model=ProcessSchema, dependencies=[_model])
def post_add_role(schema_id: str, req: AddRoleRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.add_role(schema, req.name, req.role_id))


@app.post(
    "/schemas/{schema_id}/org-units", response_model=ProcessSchema, dependencies=[_model]
)
def post_add_org_unit(schema_id: str, req: AddOrgUnitRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.add_org_unit(
            schema, req.name, req.parent_id, req.org_unit_id, req.manager_id
        )
    )


@app.post(
    "/schemas/{schema_id}/org-units/{org_unit_id}/manager",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_org_unit_manager(
    schema_id: str, org_unit_id: str, req: SetManagerRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_org_unit_manager(schema, org_unit_id, req.manager_id)
    )


@app.post(
    "/schemas/{schema_id}/org-units/{org_unit_id}/parent",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_org_unit_parent(
    schema_id: str, org_unit_id: str, req: SetParentRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_org_unit_parent(schema, org_unit_id, req.parent_id)
    )


@app.put(
    "/schemas/{schema_id}/roles/{role_id}/mailbox",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def put_role_mailbox(
    schema_id: str, role_id: str, req: SetMailboxRequest
) -> ProcessSchema:
    """Set (or clear) a role's group mailbox on the schema's embedded org (N)."""

    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_role_mailbox(schema, role_id, req.mailbox)
    )


@app.put(
    "/schemas/{schema_id}/org-units/{org_unit_id}/mailbox",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def put_unit_mailbox(
    schema_id: str, org_unit_id: str, req: SetMailboxRequest
) -> ProcessSchema:
    """Set (or clear) an org unit's mailbox on the schema's embedded org (N)."""

    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_unit_mailbox(schema, org_unit_id, req.mailbox)
    )


@app.post("/schemas/{schema_id}/agents", response_model=ProcessSchema, dependencies=[_model])
def post_add_agent(schema_id: str, req: AddAgentRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.add_agent(
            schema,
            req.name,
            req.role_ids,
            req.org_unit_id,
            req.agent_id,
            req.deputy_id,
            email=req.email,
        )
    )


@app.patch(
    "/schemas/{schema_id}/agents/{agent_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def patch_update_agent(
    schema_id: str, agent_id: str, req: UpdateAgentRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    # Distinguish "org_unit_id omitted" (keep) from "org_unit_id: null" (detach);
    # same for the e-mail address.
    org_unit = req.org_unit_id if "org_unit_id" in req.model_fields_set else ops.KEEP
    email = req.email if "email" in req.model_fields_set else ops.KEEP
    return _commit_or_422(
        lambda: ops.update_agent(
            schema,
            agent_id,
            name=req.name,
            role_ids=req.role_ids,
            org_unit_id=org_unit,
            email=email,
        )
    )


@app.post(
    "/schemas/{schema_id}/agents/{agent_id}/deputy",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_agent_deputy(
    schema_id: str, agent_id: str, req: SetDeputyRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_agent_deputy(schema, agent_id, req.deputy_id)
    )


@app.post(
    "/schemas/{schema_id}/activity-templates",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_add_activity_template(
    schema_id: str, req: AddActivityTemplateRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.add_activity_template(
            schema,
            req.name,
            req.executor,
            inputs=req.inputs,
            outputs=req.outputs,
            template_id=req.template_id,
        )
    )


@app.post("/schemas/{schema_id}/service", response_model=ProcessSchema, dependencies=[_model])
def post_assign_service(schema_id: str, req: AssignServiceRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.assign_service(
            schema,
            req.node_id,
            req.name,
            automatic=req.automatic,
            template_id=req.template_id,
            parameter_mapping=req.parameter_mapping,
        )
    )


@app.delete(
    "/schemas/{schema_id}/service/{node_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_service(schema_id: str, node_id: str) -> ProcessSchema:
    """Remove the executing service (and its automation config) from ``node_id``.

    Validated like every other change (No-Bypass); a step without a service is
    well-formed in the draft (B1 is enforced at release). Returns HTTP 422 should
    the removal ever leave the model incorrect, otherwise the updated schema.
    """
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.unassign_service(schema, node_id))


@app.post(
    "/schemas/{schema_id}/automation", response_model=ProcessSchema, dependencies=[_model]
)
def post_set_automation(schema_id: str, req: SetAutomationRequest) -> ProcessSchema:
    """Configure how an automatic ACTIVITY is driven (E11: external task / push)."""

    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_automation(
            schema,
            req.node_id,
            req.automation,
            topic=req.topic,
            endpoint_ref=req.endpoint_ref,
            retry_max=req.retry_max,
            retry_backoff_ms=req.retry_backoff_ms,
            request_timeout_ms=req.request_timeout_ms,
        )
    )


@app.post("/schemas/{schema_id}/staff-rule", response_model=ProcessSchema, dependencies=[_model])
def post_assign_staff_rule(schema_id: str, req: AssignStaffRuleRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.assign_staff_rule(schema, req.node_id, req.rule))


@app.delete(
    "/schemas/{schema_id}/staff-rule/{node_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_staff_rule(schema_id: str, node_id: str) -> ProcessSchema:
    """Remove the staff-assignment rule (BZR) from ``node_id``.

    Validated like every other change (No-Bypass); returns HTTP 422 should the
    removal ever leave the model incorrect, otherwise the updated schema.
    """
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.clear_staff_rule(schema, node_id))


@app.post(
    "/schemas/{schema_id}/value-class",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_value_class(schema_id: str, req: SetValueClassRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_value_class(schema, req.node_id, req.value_class)
    )


@app.post(
    "/schemas/{schema_id}/priority",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_priority(schema_id: str, req: SetPriorityRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_node_priority(schema, req.node_id, req.priority)
    )


@app.post(
    "/schemas/{schema_id}/mail-binding",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_mail_binding(schema_id: str, req: SetMailBindingRequest) -> ProcessSchema:
    """Attach (or clear with ``binding: null``) a modelled e-mail notification.

    Validated like every other change (No-Bypass): the mail rules N1-N4 run
    before commit, so a notification is only accepted when every possible
    recipient has an address and every template placeholder resolves; otherwise
    HTTP 422. It never sends a mail -- it only models when one is sent.
    """

    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_mail_binding(schema, req.node_id, req.binding)
    )


@app.post(
    "/schemas/{schema_id}/time-constraint",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_time_constraint(
    schema_id: str, req: SetTimeConstraintRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_time_constraint(schema, req.node_id, req.constraint)
    )


@app.post(
    "/schemas/{schema_id}/deadline",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_set_deadline(schema_id: str, req: SetDeadlineRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.set_deadline(schema, req.deadline_seconds))


@app.post("/schemas/{schema_id}/subprocess", response_model=ProcessSchema, dependencies=[_model])
def post_insert_subprocess(
    schema_id: str, req: InsertSubprocessRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.insert_subprocess(
            schema,
            req.after_node_id,
            req.target_schema_id,
            req.target_version,
            label=req.label,
            input_mapping=req.input_mapping,
            output_mapping=req.output_mapping,
            resolver=_resolver,
        )
    )


@app.post(
    "/schemas/{schema_id}/subprocess-mapping",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_subprocess_mapping(
    schema_id: str, req: SubprocessMappingRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_subprocess_mapping(
            schema,
            req.node_id,
            req.input_mapping,
            req.output_mapping,
            resolver=_resolver,
        )
    )


@app.post(
    "/schemas/{schema_id}/convert-to-subprocess",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_convert_to_subprocess(
    schema_id: str, req: ConvertToSubprocessRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.convert_activity_to_subprocess(
            schema,
            req.node_id,
            req.target_schema_id,
            req.target_version,
            input_mapping=req.input_mapping,
            output_mapping=req.output_mapping,
            resolver=_resolver,
        )
    )


@app.post(
    "/schemas/{schema_id}/subprocess-binding",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_subprocess_binding(
    schema_id: str, req: SetSubprocessBindingRequest
) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.set_subprocess_binding(
            schema,
            req.node_id,
            req.target_schema_id,
            req.target_version,
            input_mapping=req.input_mapping,
            output_mapping=req.output_mapping,
            resolver=_resolver,
        )
    )


@app.post(
    "/schemas/{schema_id}/library-flag",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def post_library_flag(schema_id: str, req: LibraryFlagRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.set_library_subprocess(schema, req.is_library))


@app.get(
    "/subprocess-library",
    response_model=list[SubprocessLibraryEntry],
    dependencies=[_read],
)
def get_subprocess_library() -> list[SubprocessLibraryEntry]:
    entries: list[SubprocessLibraryEntry] = []
    for sid in _store.list_ids():
        schema = _store.get(sid)
        if schema is None:
            continue
        if not schema.is_library_subprocess:
            continue
        if schema.lifecycle_state is not LifecycleState.RELEASED:
            continue
        entries.append(
            SubprocessLibraryEntry(
                id=schema.id,
                name=schema.name,
                version=schema.version,
                data_elements=[
                    LibraryDataElement(
                        id=el.id, name=el.name, data_type=el.data_type.value
                    )
                    for el in schema.data_elements.values()
                ],
            )
        )
    return entries


@app.post("/schemas/{schema_id}/follow-up", response_model=ProcessSchema, dependencies=[_model])
def post_link_follow_up(schema_id: str, req: LinkFollowUpRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.link_follow_up(
            schema,
            req.target_schema_id,
            target_version=req.target_version,
            trigger=req.trigger,
            condition=req.condition,
            handover_mapping=req.handover_mapping,
            mode=req.mode,
            resolver=_resolver,
        )
    )


@app.delete(
    "/schemas/{schema_id}/follow-up/{link_id}",
    response_model=ProcessSchema,
    dependencies=[_model],
)
def delete_follow_up(schema_id: str, link_id: str) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.unlink_follow_up(schema, link_id))


@app.post("/schemas/{schema_id}/release", response_model=ProcessSchema, dependencies=[_model])
def post_release(schema_id: str) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(lambda: ops.release(schema, _resolver))


# --- execution endpoints -------------------------------------------------


@app.get("/instances", dependencies=[_read])
def list_instances() -> list[str]:
    return _instances.list_ids()


@app.post(
    "/schemas/{schema_id}/instances",
    response_model=ProcessInstance,
    status_code=201,
)
def post_instantiate(
    schema_id: str, principal: Principal = Depends(get_principal)
) -> ProcessInstance:
    """Start an instance of a schema.

    A RELEASED schema may be instantiated for real by operator/modeler/admin.
    A non-released (draft) schema may only be started as a throw-away *test*
    instance, and only by a modeller/admin -- it is flagged ``is_test``. A test
    instance records *no* audit events for its whole lifecycle (creation, step
    start/complete, ad-hoc changes, completion) and triggers no webhooks or
    external pushes, so it never pollutes the monitoring KPIs or the audit log.
    """

    schema = _get_or_404(schema_id)
    released = schema.lifecycle_state is LifecycleState.RELEASED
    if released:
        if not principal.roles.intersection({"operator", "modeler", "admin"}):
            raise HTTPException(status_code=403, detail="forbidden")
    elif not principal.roles.intersection({"modeler", "admin"}):
        raise HTTPException(
            status_code=403,
            detail="only modellers/admins may start a test instance of a draft",
        )
    if released:
        # Licensing guard (dormant unless enforced): block *new* instances of a
        # schema that references an expired/uncovered agent. Running instances
        # are never touched. No effect while licensing is off. Test/draft starts
        # (throw-away, no real work) are deliberately exempt.
        try:
            _license.assert_agents_licensed(_required_agent_ids(schema))
        except LicenseError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    is_test = not released
    instance = _run_or_409(
        lambda: exe.instantiate(
            schema, context=_context, allow_unreleased=not released, is_test=is_test
        )
    )
    if is_test:
        # Test instances stay out of the audit log (and therefore the KPIs).
        return instance
    _audit.append(
        EventType.INSTANCE_CREATED,
        instance.id,
        instance.schema_id,
        schema_version=instance.schema_version,
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
    return instance


@app.get("/instances/{instance_id}", response_model=ProcessInstance, dependencies=[_read])
def get_instance(instance_id: str) -> ProcessInstance:
    return _get_instance_or_404(instance_id)


@app.get(
    "/instances/{instance_id}/worklist",
    response_model=WorklistReport,
    dependencies=[_read],
)
def get_worklist(instance_id: str) -> WorklistReport:
    instance = _get_instance_or_404(instance_id)
    schema = _effective_schema_for(instance)
    return WorklistReport(
        state=instance.state.value,
        ready_activities=exe.worklist(instance, schema),
        pending_decisions=exe.pending_decisions(instance, schema),
    )


@app.get(
    "/instances/{instance_id}/tasks",
    response_model=list[OpenTask],
    dependencies=[_read],
)
def get_instance_tasks(instance_id: str) -> list[OpenTask]:
    instance = _get_instance_or_404(instance_id)
    schema = _effective_schema_for(instance)
    return assignment.open_tasks(
        schema,
        instance,
        _time_context(instance, schema),
        absent_agents=_current_absent_agents(),
    )


def _tasks_for_agent(agent_id: str) -> list[OpenTask]:
    """Collect the open tasks an agent is currently eligible for (incl. deputy).

    Each instance's tasks are prioritised with its own time context (time-based
    worklist prioritisation); the cross-instance list is then re-sorted so the
    agent sees one coherent, most-urgent-first todo list across all instances.
    """

    tasks: list[OpenTask] = []
    absent = _current_absent_agents()
    for instance_id in _instances.list_ids():
        instance = _instances.get(instance_id)
        if instance is None or instance.state is not InstanceState.RUNNING:
            continue
        schema = _effective_schema_for(instance)
        ctx = _time_context(instance, schema)
        for task in assignment.open_tasks(schema, instance, ctx, absent_agents=absent):
            if agent_id in task.eligible_agents:
                tasks.append(task)
    tasks.sort(
        key=lambda t: worklist_priority.sort_key(
            t.time_criticality,
            PRIORITY_RANK[t.priority],
            t.due_at,
            t.label,
            t.node_id,
        )
    )
    return tasks


@app.get("/me/tasks", response_model=list[OpenTask])
def get_my_tasks(
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> list[OpenTask]:
    """The worklist of the logged-in agent (the bound principal's own tasks)."""

    if principal.agent_id is None:
        # Open dev mode: no bound agent -> use /agents/{id}/tasks with a picker.
        return []
    return _tasks_for_agent(principal.agent_id)


@app.get("/agents/{agent_id}/tasks", response_model=list[OpenTask])
def get_agent_tasks(
    agent_id: str,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> list[OpenTask]:
    _require_agent_self_or_supervisor(principal, agent_id)
    return _tasks_for_agent(agent_id)


# --- absence / deputy substitution ---------------------------------------


def _require_agent_self_or_supervisor(principal: Principal, agent_id: str) -> None:
    """Guard: a bound, non-supervisor operator may act only on their own record.

    An admin/modeler may act for any agent (supervision); an unbound principal
    (open dev mode) is unrestricted. Raises 403 otherwise. Shared by the worklist
    and the absence endpoints so the access rule is defined once.
    """

    if (
        principal.is_bound
        and principal.agent_id != agent_id
        and not principal.roles.intersection({"admin", "modeler"})
    ):
        raise HTTPException(status_code=403, detail="forbidden")


def _known_agent_ids() -> set[str]:
    """Every agent id known to the system (shared org models + embedded orgs).

    Absences are org-wide and keyed by agent id; this backs a helpful 404 when an
    unknown agent id is submitted. The union spans the shared org store and every
    schema's embedded org model, so a per-schema agent is recognised too.
    """

    ids: set[str] = set()
    for org_id in _org_store.list_ids():
        org = _org_store.get(org_id)
        if org is not None:
            ids.update(org.agents.keys())
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


@app.get("/agents/{agent_id}/absences", response_model=list[AbsenceEntry])
def get_agent_absences(
    agent_id: str,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> list[AbsenceEntry]:
    """List an agent's absence windows (self, or any agent for admin/modeler)."""

    _require_agent_self_or_supervisor(principal, agent_id)
    return _list_absences(agent_id)


@app.get("/me/absences", response_model=list[AbsenceEntry])
def get_my_absences(
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> list[AbsenceEntry]:
    """The logged-in agent's own absence windows (empty when no bound agent)."""

    if principal.agent_id is None:
        return []
    return _list_absences(principal.agent_id)


@app.post("/agents/{agent_id}/absences", response_model=AbsenceEntry, status_code=201)
def post_agent_absence(
    agent_id: str,
    req: CreateAbsenceRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> AbsenceEntry:
    """Record an absence window for an agent (deputy stands in during it).

    Self-service or supervisory. The window must be non-empty (``end_at >=
    start_at``) and the agent must be known. The absence never removes the agent
    from any worklist -- it only adds the deputy in parallel -- so it cannot stall
    an instance even if no deputy is registered.
    """

    _require_agent_self_or_supervisor(principal, agent_id)
    if agent_id not in _known_agent_ids():
        raise HTTPException(status_code=404, detail=f"unknown agent '{agent_id}'")
    if req.end_at < req.start_at:
        raise HTTPException(
            status_code=422, detail="end_at must not be before start_at"
        )
    entry = AbsenceEntry(
        id=f"abs_{uuid.uuid4().hex}",
        agent_id=agent_id,
        start_at=req.start_at,
        end_at=req.end_at,
        note=req.note,
    )
    return _absence_store.put_entry(entry)


@app.delete("/agents/{agent_id}/absences/{absence_id}", status_code=204)
def delete_agent_absence(
    agent_id: str,
    absence_id: str,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> Response:
    """Remove an absence window (self, or any agent for admin/modeler)."""

    _require_agent_self_or_supervisor(principal, agent_id)
    entry = _absence_store.get_entry(absence_id)
    if entry is None or entry.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="absence not found")
    _absence_store.delete_entry(absence_id)
    return Response(status_code=204)


@app.post("/instances/{instance_id}/start", response_model=ProcessInstance, dependencies=[_run])
def post_start_activity(instance_id: str, req: StartActivityRequest) -> ProcessInstance:
    instance = _get_instance_or_404(instance_id)
    schema = _effective_schema_for(instance)
    after = _run_or_409(lambda: exe.start_activity(instance, schema, req.node_id))
    if not instance.is_test:
        # A throw-away test instance of a draft records no audit events, so it
        # never reaches the monitoring KPIs (mirrors instance creation).
        _audit.append(
            EventType.ACTIVITY_STARTED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
            node_id=req.node_id,
            label=_label_of(schema, req.node_id),
        )
    return after


@app.post("/instances/{instance_id}/complete", response_model=ProcessInstance)
def post_complete_activity(
    instance_id: str,
    req: CompleteActivityRequest,
    principal: Principal = Depends(require_role("operator", "modeler", "admin")),
) -> ProcessInstance:
    before = _get_instance_or_404(instance_id)
    schema = _effective_schema_for(before)
    before_states = dict(before.node_states)
    acting_agent = _resolve_acting_agent(principal, req.agent_id)
    after = _run_or_409(
        lambda: exe.complete_activity(
            before,
            schema,
            req.node_id,
            req.data,
            agent_id=acting_agent,
            context=_context,
            absent_agents=_current_absent_agents(),
        )
    )
    if not before.is_test:
        # A throw-away test instance stays out of the audit log / KPIs and
        # triggers no external side effects (mirrors instance creation).
        _audit.append(
            EventType.ACTIVITY_COMPLETED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
            node_id=req.node_id,
            label=_label_of(schema, req.node_id),
            agent_id=acting_agent,
        )
        _record_completion(before, after)
        _after_advance(schema, before_states, after)
    return after


# --- ad-hoc changes (per-instance variant; R1/R2) ------------------------


@app.post(
    "/instances/{instance_id}/adhoc/insert",
    response_model=ProcessInstance,
    dependencies=[_run],
)
def post_adhoc_insert(instance_id: str, req: AdhocInsertRequest) -> ProcessInstance:
    instance = _get_instance_or_404(instance_id)
    schema = _effective_schema_for(instance)
    before_states = dict(instance.node_states)
    after = _commit_instance_or_422(
        lambda: adhoc.adhoc_insert_activity(
            instance, schema, req.after_node_id, req.label, resolver=_resolver
        )
    )
    if not instance.is_test:
        # Test instances record no audit events (see instance creation).
        _audit.append(
            EventType.ADHOC_INSERTED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
            node_id=req.after_node_id,
            detail={"label": req.label},
        )
        # An ad-hoc insert can make a mail-bound activity ready -> notify (§10.7).
        _after_advance(_effective_schema_for(after), before_states, after)
    return after


@app.post(
    "/instances/{instance_id}/adhoc/delete",
    response_model=ProcessInstance,
    dependencies=[_run],
)
def post_adhoc_delete(instance_id: str, req: AdhocDeleteRequest) -> ProcessInstance:
    instance = _get_instance_or_404(instance_id)
    schema = _effective_schema_for(instance)
    before_states = dict(instance.node_states)
    after = _commit_instance_or_422(
        lambda: adhoc.adhoc_delete_node(
            instance, schema, req.node_id, resolver=_resolver
        )
    )
    if not instance.is_test:
        # Test instances record no audit events (see instance creation).
        _audit.append(
            EventType.ADHOC_DELETED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
            node_id=req.node_id,
        )
        # Deleting a node can hand control to a mail-bound successor -> notify.
        _after_advance(_effective_schema_for(after), before_states, after)
    return after


@app.post(
    "/instances/{instance_id}/adhoc/rename",
    response_model=ProcessInstance,
    dependencies=[_run],
)
def post_adhoc_rename(instance_id: str, req: AdhocRenameRequest) -> ProcessInstance:
    instance = _get_instance_or_404(instance_id)
    schema = _effective_schema_for(instance)
    after = _commit_instance_or_422(
        lambda: adhoc.adhoc_rename_activity(
            instance, schema, req.node_id, req.label, resolver=_resolver
        )
    )
    if not instance.is_test:
        # Test instances record no audit events (see instance creation).
        _audit.append(
            EventType.ADHOC_RENAMED,
            after.id,
            after.schema_id,
            schema_version=after.schema_version,
            node_id=req.node_id,
            detail={"label": req.label},
        )
    return after


# --- schema evolution + instance migration (M1-M5) -----------------------


@app.post("/schemas/{schema_id}/revision", response_model=ProcessSchema, dependencies=[_model])
def post_new_revision(schema_id: str, req: RevisionRequest) -> ProcessSchema:
    schema = _get_or_404(schema_id)
    return _commit_or_422(
        lambda: ops.new_revision(schema, new_schema_id=req.new_schema_id)
    )


@app.post(
    "/instances/{instance_id}/migration-check",
    response_model=MigrationReport,
    dependencies=[_run],
)
def post_migration_check(
    instance_id: str, req: MigrateRequest
) -> MigrationReport:
    instance = _get_instance_or_404(instance_id)
    source = _get_or_404(instance.schema_id)
    target = _get_or_404(req.target_schema_id)
    findings = migration.check_migration(
        instance,
        source,
        target,
        resolver=_resolver,
        data_mapping=req.data_mapping or None,
    )
    return MigrationReport(migratable=not findings, findings=findings)


@app.post("/instances/{instance_id}/migrate", response_model=ProcessInstance, dependencies=[_run])
def post_migrate(instance_id: str, req: MigrateRequest) -> ProcessInstance:
    instance = _get_instance_or_404(instance_id)
    source = _get_or_404(instance.schema_id)
    target = _get_or_404(req.target_schema_id)
    before_states = dict(instance.node_states)
    after = _commit_instance_or_422(
        lambda: migration.migrate_instance(
            instance,
            source,
            target,
            data_mapping=req.data_mapping or None,
            resolver=_resolver,
        )
    )
    _audit.append(
        EventType.INSTANCE_MIGRATED,
        after.id,
        after.schema_id,
        schema_version=after.schema_version,
        detail={
            "source_schema_id": instance.schema_id,
            "target_schema_id": req.target_schema_id,
        },
    )
    if not instance.is_test:
        # Migration can activate mail-bound nodes on the *target* schema (a step
        # the source did not have, or a re-mapped position) -> notify (§10.7).
        _after_advance(_effective_schema_for(after), before_states, after)
    return after


# --- monitoring + audit (step 15) ----------------------------------------


@app.get(
    "/instances/{instance_id}/audit",
    response_model=list[AuditEvent],
    dependencies=[_read],
)
def get_instance_audit(instance_id: str) -> list[AuditEvent]:
    _get_instance_or_404(instance_id)
    return instance_timeline(_audit.list_all(), instance_id)


@app.get("/audit", response_model=list[AuditEvent], dependencies=[_read])
def get_audit(
    schema_id: str | None = None, instance_id: str | None = None
) -> list[AuditEvent]:
    events = _audit.list_all()
    if schema_id is not None:
        events = [e for e in events if e.schema_id == schema_id]
    if instance_id is not None:
        events = [e for e in events if e.instance_id == instance_id]
    return events


@app.get("/monitoring/kpis", response_model=KpiReport, dependencies=[_read])
def get_kpis(schema_id: str | None = None) -> KpiReport:
    return compute_kpis(_audit.list_all(), schema_id)


@app.get("/monitoring/process-map", response_model=ProcessMap, dependencies=[_read])
def get_process_map(schema_id: str | None = None) -> ProcessMap:
    return discover_process_map(_audit.list_all(), schema_id)


@app.get(
    "/monitoring/revision",
    response_model=MonitoringRevision,
    dependencies=[_read],
)
def get_monitoring_revision() -> MonitoringRevision:
    """Return the current runtime-event revision for cheap client polling.

    The revision is a monotonic counter that increases whenever a runtime event
    (instance/activity progress) is recorded. The web client polls this endpoint
    and re-renders the live views (tasks, monitoring, the running instance) when
    the value changes, so progress made by others appears automatically.
    """

    return MonitoringRevision(revision=_audit.revision())


# --- versioned integration API (/v1) — inbound control by external tools -
#
# These endpoints mirror the existing runtime endpoints under a stable,
# versioned ``/v1`` prefix and gate them with integration *scopes* (so a service
# token is confined to least privilege), while remaining fully usable by human/
# open principals via their roles. Mutating calls honour an ``Idempotency-Key``
# header. The endpoints add no new domain logic: they reuse the same
# validate-before-commit core path as the GUI (Section 5.4, API-first).

_v1 = APIRouter(prefix="/v1", tags=["integration"])


class SetDataRequest(BaseModel):
    values: dict[str, object] = Field(
        ..., examples=[{"betrag": 1200, "status": "open"}]
    )


class V1CompleteRequest(BaseModel):
    data: dict[str, object] = Field(default_factory=dict)
    agent_id: str | None = Field(default=None, examples=["a1"])


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
                )
            )
    return findings


@app.put(
    "/instances/{instance_id}/data",
    response_model=dict[str, object],
    dependencies=[_run],
)
def put_instance_data(instance_id: str, req: SetDataRequest) -> dict[str, object]:
    """Set process variable values directly on an instance (type-checked, D3).

    This lets an operator/modeller enter instance data at any time -- in
    particular right after starting an instance, before the first activity is
    worked on -- without having to go through an activity completion. Only
    ``INSTANCE`` data elements can be written; unknown elements or type
    mismatches are rejected with HTTP 422 (D3). The write records no audit
    event, so it never pollutes the monitoring KPIs.
    """

    instance = _get_instance_or_404(instance_id)
    schema = _effective_schema_for(instance)
    findings = _validate_data_values(schema, req.values)
    if findings:
        raise HTTPException(
            status_code=422,
            detail={"findings": [f.model_dump() for f in findings]},
        )
    updated = instance.model_copy(deep=True)
    updated.data_values.update(req.values)
    _instances.put(updated)
    return dict(updated.data_values)


@_v1.post(
    "/schemas/{schema_id}/instances",
    response_model=ProcessInstance,
    status_code=201,
)
def v1_start_instance(
    schema_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(
        require_scope(SCOPE_INSTANCES_START, "operator", "modeler", "admin")
    ),
) -> ProcessInstance:
    """Start an instance of a RELEASED schema (integration entry point).

    Unlike the legacy endpoint, this never starts a throw-away *test* instance
    of a draft: a service may only run released processes (409 otherwise).
    """

    def produce() -> ProcessInstance:
        schema = _get_or_404(schema_id)
        if schema.lifecycle_state is not LifecycleState.RELEASED:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "only released schemas can be instantiated via /v1"
                },
            )
        # Licensing guard (dormant unless enforced): mirror the GUI entry point.
        try:
            _license.assert_agents_licensed(_required_agent_ids(schema))
        except LicenseError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        instance = _run_or_409(lambda: exe.instantiate(schema, context=_context))
        _audit.append(
            EventType.INSTANCE_CREATED,
            instance.id,
            instance.schema_id,
            schema_version=instance.schema_version,
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
        return instance

    result = _idempotent(principal, idempotency_key, produce)
    assert isinstance(result, ProcessInstance)
    return result


@_v1.get("/instances/{instance_id}", response_model=ProcessInstance)
def v1_get_instance(
    instance_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> ProcessInstance:
    return _get_instance_or_404(instance_id)


@_v1.get("/instances/{instance_id}/tasks", response_model=list[OpenTask])
def v1_get_instance_tasks(
    instance_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> list[OpenTask]:
    instance = _get_instance_or_404(instance_id)
    schema = _effective_schema_for(instance)
    return assignment.open_tasks(
        schema, instance, absent_agents=_current_absent_agents()
    )


@_v1.post(
    "/instances/{instance_id}/nodes/{node_id}/complete",
    response_model=ProcessInstance,
)
def v1_complete_task(
    instance_id: str,
    node_id: str,
    req: V1CompleteRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ProcessInstance:
    """Complete a task and hand over its data (mirror of ``/complete``)."""

    body = req or V1CompleteRequest()

    def produce() -> ProcessInstance:
        completion = CompleteActivityRequest(
            node_id=node_id, data=body.data, agent_id=body.agent_id
        )
        return post_complete_activity(instance_id, completion, principal)

    result = _idempotent(principal, idempotency_key, produce)
    assert isinstance(result, ProcessInstance)
    return result


@_v1.get("/instances/{instance_id}/data", response_model=dict[str, object])
def v1_get_instance_data(
    instance_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> dict[str, object]:
    """Read all process variable values of an instance."""

    return dict(_get_instance_or_404(instance_id).data_values)


@_v1.put("/instances/{instance_id}/data", response_model=dict[str, object])
def v1_put_instance_data(
    instance_id: str,
    req: SetDataRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_WRITE, "operator", "modeler", "admin")
    ),
) -> dict[str, object]:
    """Set process variable values, type-checked against the schema (D3)."""

    def produce() -> dict[str, object]:
        instance = _get_instance_or_404(instance_id)
        schema = _effective_schema_for(instance)
        findings = _validate_data_values(schema, req.values)
        if findings:
            raise HTTPException(
                status_code=422,
                detail={"findings": [f.model_dump() for f in findings]},
            )
        updated = instance.model_copy(deep=True)
        updated.data_values.update(req.values)
        _instances.put(updated)
        return dict(updated.data_values)

    result = _idempotent(principal, idempotency_key, produce)
    assert isinstance(result, dict)
    return result


# --- External-task runtime (outbound integration boundary, roadmap E11) ----


class FetchAndLockRequest(BaseModel):
    worker_id: str = Field(..., examples=["worker-1"])
    topics: list[str] = Field(..., examples=[["invoice-check"]])
    lock_ms: int = Field(default=300_000, ge=1, examples=[300000])
    max_tasks: int = Field(default=1, ge=1, examples=[10])
    use_priority: bool = True


class CompleteTaskRequest(BaseModel):
    worker_id: str = Field(..., examples=["worker-1"])
    variables: dict[str, object] = Field(
        default_factory=dict, examples=[{"approved": True}]
    )


class FailureRequest(BaseModel):
    worker_id: str = Field(..., examples=["worker-1"])
    error_message: str = Field(..., examples=["connector timeout"])
    retries: int | None = Field(default=None, ge=0, examples=[3])
    retry_timeout_ms: int | None = Field(default=None, ge=0, examples=[5000])


class BpmnErrorRequest(BaseModel):
    worker_id: str = Field(..., examples=["worker-1"])
    error_code: str = Field(..., examples=["INSUFFICIENT_FUNDS"])


class ExtendLockRequest(BaseModel):
    worker_id: str = Field(..., examples=["worker-1"])
    lock_ms: int = Field(..., ge=1, examples=[60000])


class WorkerRequest(BaseModel):
    worker_id: str = Field(..., examples=["worker-1"])


@_v1.post("/external-tasks/fetch-and-lock", response_model=list[ExternalTask])
def v1_fetch_and_lock(
    req: FetchAndLockRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "operator", "modeler", "admin")
    ),
) -> list[ExternalTask]:
    """Claim automatic external-task work for the given topics (outbound pull)."""

    result = _run_external(
        lambda: _external_runtime().fetch_and_lock(
            req.worker_id,
            req.topics,
            lock_ms=req.lock_ms,
            max_tasks=req.max_tasks,
            use_priority=req.use_priority,
        )
    )
    assert isinstance(result, list)
    return result


@_v1.get("/external-tasks/{task_id}", response_model=ExternalTask)
def v1_get_external_task(
    task_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "viewer", "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    task = _external_runtime().get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404, detail={"message": f"external task '{task_id}' not found"}
        )
    return task


@_v1.post("/external-tasks/{task_id}/complete", response_model=ExternalTask)
def v1_complete_external_task(
    task_id: str,
    req: CompleteTaskRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Report success: write outputs and advance the instance (exactly-once)."""

    def produce() -> ExternalTask:
        result = _run_external(
            lambda: _external_runtime().complete(
                task_id, req.worker_id, req.variables
            )
        )
        assert isinstance(result, ExternalTask)
        _drive_pushes()
        return result

    outcome = _idempotent(principal, idempotency_key, produce)
    assert isinstance(outcome, ExternalTask)
    return outcome


@_v1.post("/external-tasks/{task_id}/failure", response_model=ExternalTask)
def v1_fail_external_task(
    task_id: str,
    req: FailureRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Report a technical failure: re-queue with back-off or raise an incident."""

    result = _run_external(
        lambda: _external_runtime().failure(
            task_id,
            req.worker_id,
            req.error_message,
            retries=req.retries,
            retry_timeout_ms=req.retry_timeout_ms,
        )
    )
    assert isinstance(result, ExternalTask)
    return result


@_v1.post("/external-tasks/{task_id}/bpmn-error", response_model=ExternalTask)
def v1_bpmn_error_external_task(
    task_id: str,
    req: BpmnErrorRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Report a business (BPMN) error for the locked task."""

    result = _run_external(
        lambda: _external_runtime().bpmn_error(
            task_id, req.worker_id, req.error_code
        )
    )
    assert isinstance(result, ExternalTask)
    return result


@_v1.post("/external-tasks/{task_id}/extend-lock", response_model=ExternalTask)
def v1_extend_lock_external_task(
    task_id: str,
    req: ExtendLockRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Prolong the lock on a long-running task."""

    result = _run_external(
        lambda: _external_runtime().extend_lock(task_id, req.worker_id, req.lock_ms)
    )
    assert isinstance(result, ExternalTask)
    return result


@_v1.post("/external-tasks/{task_id}/unlock", response_model=ExternalTask)
def v1_unlock_external_task(
    task_id: str,
    req: WorkerRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "modeler", "admin")
    ),
) -> ExternalTask:
    """Release the lock, returning the task to the queue immediately."""

    result = _run_external(
        lambda: _external_runtime().unlock(task_id, req.worker_id)
    )
    assert isinstance(result, ExternalTask)
    return result


@_v1.get("/incidents", response_model=list[Incident])
def v1_list_incidents(
    unresolved_only: bool = False,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "viewer", "operator", "modeler", "admin")
    ),
) -> list[Incident]:
    """List external-task incidents (optionally only the unresolved ones)."""

    return _external_runtime().list_incidents(unresolved_only=unresolved_only)


@_v1.post("/incidents/{incident_id}/resolve", response_model=Incident)
def v1_resolve_incident(
    incident_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_COMPLETE, "operator", "admin")
    ),
) -> Incident:
    """Resolve an incident and re-queue its task for another attempt."""

    result = _run_external(
        lambda: _external_runtime().resolve_incident(incident_id)
    )
    assert isinstance(result, Incident)
    return result


@_v1.get("/push-endpoints", response_model=list[str])
def v1_list_push_endpoints(
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "viewer", "operator", "modeler", "admin")
    ),
) -> list[str]:
    """List configured ``HTTP_PUSH`` endpoint references (never URLs/secrets).

    A modeller binds an automatic step to one of these references; the concrete
    URL and signing secret stay server-side (``PROCWORKS_PUSH_ENDPOINTS``).
    """

    return _push_endpoints.refs()


@_v1.post("/external-tasks/drive-push", response_model=list[ExternalTask])
def v1_drive_push(
    principal: Principal = Depends(
        require_scope(SCOPE_TASKS_FETCH, "operator", "admin")
    ),
) -> list[ExternalTask]:
    """Push activated ``HTTP_PUSH`` steps now (e.g. re-push after a back-off).

    Pushes happen automatically after every advance; this endpoint lets an
    operator force a drive so tasks waiting out a failure back-off are re-pushed
    without waiting for the next process event. Idempotent and side-effect-safe.
    """

    return _external_runtime().drive_push()


# --- Data connectors (registry test / sample read, roadmap P3) -------------


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


@_v1.get("/connectors", response_model=list[ConnectorInfo])
def v1_list_connectors(
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> list[ConnectorInfo]:
    """List configured data connectors (metadata only -- never secrets)."""

    return [
        ConnectorInfo(connector_id=cfg.connector_id, kind=cfg.kind)
        for cfg in _connections.configs()
    ]


@_v1.post("/connectors/{connector_id}/test", response_model=ConnectorTestResult)
def v1_test_connector(
    connector_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "operator", "modeler", "admin")
    ),
) -> ConnectorTestResult:
    """Run a read-only connection check without revealing any secret."""

    _require_connector(connector_id)
    try:
        _connections.test(connector_id)
    except DataAccessError as err:
        raise HTTPException(status_code=502, detail={"message": str(err)}) from err
    return ConnectorTestResult(connector_id=connector_id, ok=True)


@_v1.post("/connectors/{connector_id}/sample-read", response_model=list[dict[str, object]])
def v1_sample_read_connector(
    connector_id: str,
    req: SampleReadRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "operator", "modeler", "admin")
    ),
) -> list[dict[str, object]]:
    """Return a few sample records of an entity for GUI mapping help."""

    _require_connector(connector_id)
    try:
        rows = _connections.sample_read(connector_id, req.entity, limit=req.limit)
    except DataAccessError as err:
        raise HTTPException(status_code=502, detail={"message": str(err)}) from err
    return [dict(row) for row in rows]


@_v1.get("/connectors/{connector_id}/columns", response_model=list[ColumnInfo])
def v1_connector_columns(
    connector_id: str,
    entity: str,
    principal: Principal = Depends(
        require_scope(SCOPE_DATA_READ, "viewer", "operator", "modeler", "admin")
    ),
) -> list[ColumnInfo]:
    """Reflect an entity's columns + mapped data types for the GUI assistant."""

    _require_connector(connector_id)
    try:
        columns = _connections.columns(connector_id, entity)
    except DataAccessError as err:
        raise HTTPException(status_code=502, detail={"message": str(err)}) from err
    return [ColumnInfo.model_validate(column) for column in columns]


# --- Webhooks (event side of the open API, roadmap P4/E13) -----------------


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
        raise HTTPException(
            status_code=err.status, detail={"message": err.message}
        ) from err


@_v1.get("/webhooks", response_model=list[WebhookSubscription])
def v1_list_webhooks(
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> list[WebhookSubscription]:
    """List webhook subscriptions (the secret itself is never returned)."""

    return _outbox.list_subscriptions()


@_v1.post("/webhooks", response_model=WebhookSubscription, status_code=201)
def v1_create_webhook(
    req: WebhookCreateRequest,
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> WebhookSubscription:
    """Register a webhook subscription (validates events and the SSRF policy)."""

    result = _run_webhook(
        lambda: _outbox.subscribe(req.url, req.events, req.secret_ref)
    )
    assert isinstance(result, WebhookSubscription)
    return result


@_v1.delete("/webhooks/{subscription_id}", status_code=204)
def v1_delete_webhook(
    subscription_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> None:
    """Remove a webhook subscription."""

    _run_webhook(lambda: _outbox.unsubscribe(subscription_id))


@_v1.post("/webhooks/{subscription_id}/test", response_model=WebhookDelivery)
def v1_test_webhook(
    subscription_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> WebhookDelivery:
    """Send a synthetic ping to one subscription and return the attempt result."""

    result = _run_webhook(lambda: _outbox.test_delivery(subscription_id))
    assert isinstance(result, WebhookDelivery)
    return result


@_v1.get(
    "/webhooks/{subscription_id}/deliveries", response_model=list[WebhookDelivery]
)
def v1_webhook_deliveries(
    subscription_id: str,
    principal: Principal = Depends(
        require_scope(SCOPE_EVENTS_SUBSCRIBE, "modeler", "admin")
    ),
) -> list[WebhookDelivery]:
    """Return the delivery log of one subscription (audit / debugging)."""

    _run_webhook(
        lambda: _outbox.get_subscription(subscription_id)
        or _raise_webhook_404(subscription_id)
    )
    return _outbox.deliveries(subscription_id)


def _raise_webhook_404(subscription_id: str) -> object:
    raise WebhookError(f"subscription '{subscription_id}' not found", 404)


app.include_router(_v1)


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


def _maybe_mount_web(target: FastAPI, web_dir: str) -> bool:
    """Mount the static web client at ``/`` when ``web_dir`` is a real directory.

    D0b (Demo-Hosting-Konzept, Variante A): optionally serve the static web
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
        return True
    return False


_maybe_mount_web(app, os.environ.get("PROCWORKS_WEB_DIR", ""))

