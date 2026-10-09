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

import json
import logging
import os
import secrets
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from procworks import (
    __version__,
    adhoc,
    assignment,
    demo,
    demo_o2c,
    ids,
    mail_runtime,
    migration,
    worklist_priority,
)
from procworks import execution as exe
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
    LoginThrottle,
    PasswordAuthBackend,
    PasswordPolicyError,
    UserView,
    user_view,
)
from procworks.connections import build_connection_registry
from procworks.dal import DataAccessError
from procworks.execution import ExecutionError
from procworks.integration_runtime import ExternalTaskError, ExternalTaskRuntime
from procworks.licensing import (
    LICENSE_PSEUDO_ID,
    LicenseManager,
    SlotSummary,
    create_license_store,
)
from procworks.model import (
    PRIORITY_RANK,
    WRITE_MODES,
    AbsenceEntry,
    AccessMode,
    AggregateKind,
    AutomationKind,
    Cardinality,
    ConnectorKind,
    DataType,
    EscalationKind,
    EscalationPolicy,
    EscalationStage,
    ExecutorKind,
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
    TemplateOrigin,
    TemplateParameter,
    TimeConstraint,
    ValueClass,
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
)
from procworks.validator import (
    CorrectnessError,
    ValidationFinding,
    _possible_agents,
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
    """
    # Zuerst die ID-Zaehler hinter den gespeicherten Bestand setzen -- sonst
    # ersetzt der erste neue Vorgang nach einem Neustart ``instance_1``.
    _reserve_stored_ids()
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
    witnessed it (tamper evidence).
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

# Auth is a coarse boundary layer. The backend is
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
    * Otherwise a non-blank ``reason`` is mandatory (422) -- the visible price
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
    cleaned = (reason or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=422,
            detail=(
                "Aufsichtseingriff: Dieser Login ist keinem Bearbeiter zugeordnet. "
                "Bitte eine Begründung angeben, warum der Schritt an der "
                "Bearbeiterregel vorbei abgeschlossen wird."
            ),
        )
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
    """Apply a shared-org change, re-validating every referencing schema.

    The org op is validated for internal consistency (validate-before-commit);
    additionally each schema that references the org is hydrated with the
    *candidate* org and re-validated, so an org edit can never silently break a
    referencing process's staffing. Only if every referencing schema stays
    correct is the new org persisted (atomic across the org boundary).
    """

    try:
        org = result_fn()
    except CorrectnessError as exc:
        raise _findings_422(exc.findings) from exc
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
    address list or the body (data minimisation). Transient
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
    becomes ready is notified no matter which path activated it.
    """

    _stamp_activations(schema, before_states, after)
    _drive_pushes()
    _notify_ready(schema, before_states, after)
    _instances.put(after)
    _record_subprocess_joins(schema, before_states, after)


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
            if parent.state is InstanceState.COMPLETED:
                _audit.append(
                    EventType.INSTANCE_COMPLETED,
                    parent.id,
                    parent.schema_id,
                    schema_version=parent.schema_version,
                )
                _emit_event("instance.completed", _instance_event_payload(parent))
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
    )


def _run_external(action: Callable[[], object]) -> object:
    """Execute an external-task action, mapping ExternalTaskError to HTTP."""

    try:
        return action()
    except ExternalTaskError as err:
        raise HTTPException(
            status_code=err.status, detail={"message": err.message}
        ) from err



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
    return {"status": "ok", "version": __version__}


@app.get("/auth/me", response_model=Principal)
def get_me(principal: Principal = Depends(get_principal)) -> Principal:
    """Return the verified identity of the caller (for the client's login UI)."""

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


def _client_address(request: Request) -> str | None:
    """Best-effort client address for the login throttle.

    Behind the bundled Caddy the TCP peer is the proxy, so its address would
    lump every user together. Caddy *sets* ``X-Forwarded-For`` to the real
    client (it ignores a client-sent value unless ``trusted_proxies`` is
    configured). The header is therefore honoured **only** when the peer is a
    private/loopback address -- i.e. a proxy of our own stack -- and then its
    last entry is taken (the hop Caddy appended). A public peer is the client
    itself; a spoofed header from it is ignored.
    """

    import ipaddress

    peer = request.client.host if request.client else None
    try:
        internal = peer is not None and (
            ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
        )
    except ValueError:
        internal = False  # e.g. the TestClient's "testclient"
    forwarded = request.headers.get("X-Forwarded-For", "")
    if internal and forwarded.strip():
        return forwarded.split(",")[-1].strip() or peer
    return peer


@app.post("/auth/login", response_model=LoginResponse)
def post_login(req: LoginRequest, request: Request) -> LoginResponse:
    """Exchange username + password for a session bearer token (password mode).

    Guarded by :data:`_login_throttle`: after repeated failures per
    login or per client address the endpoint answers 429 with ``Retry-After``
    -- *before* the password is even checked, so a locked key gives no oracle.
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
        result = backend.login(req.login, req.password)
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
        raise HTTPException(status_code=400, detail=_policy_detail(exc)) from exc
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


# Serialises "is this the last admin?" with the deletion itself. Without it two
# admins deleting each other at the same moment would both see the other one
# still present and both succeed, leaving no admin. The lock guards one API
# process. With several replicas (the Helm chart defaults to two) a window of
# milliseconds remains between processes; accepted, because it needs two admins
# deleting each other at the same instant, and setting PROCWORKS_ADMIN_LOGIN
# plus PROCWORKS_ADMIN_PASSWORD re-creates a missing admin on the next start.
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
        _delete_user_checked(backend, login, principal)
    backend.revoke_sessions(login)  # sessions persist across restarts -- end them
    return Response(status_code=204)


def _delete_user_checked(
    backend: PasswordAuthBackend, login: str, principal: Principal
) -> None:
    """Refuse self/last-admin deletion, otherwise delete (caller holds the lock).

    :raises HTTPException: 409 ``USERS.delete-self`` / ``USERS.last-admin``.
    """

    target = backend.store.get_user(login)
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
    """Optional trusted timestamp (epoch seconds) from a signed time source."""

    trusted_now: float | None = None


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
    """

    agent = principal.agent_id
    if agent is None:
        return set()
    ids = {e.instance_id for e in _audit.list_all() if e.agent_id == agent}
    absent = _current_absent_agents()
    for instance_id in _instances.list_ids():
        if instance_id in ids:
            continue
        instance = _instances.get(instance_id)
        if instance is None or instance.state is not InstanceState.RUNNING:
            continue
        tasks = assignment.open_tasks(
            _effective_schema_for(instance), instance, absent_agents=absent
        )
        if any(t.claimed_by == agent or agent in t.eligible_agents for t in tasks):
            ids.add(instance_id)
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

    A limited caller gets **404** for an instance it is not involved in -- the
    same answer as for a missing one, so the endpoint does not reveal which
    instance ids exist. The action endpoints (claim, return, suspend, resume,
    fail, reset, start, complete, data) load through here too: a 409 or a D3
    finding for a foreign instance would reveal its state or its data.
    """

    instance = _get_instance_or_404(instance_id)
    if _reads_only_own_instances(principal) and not _is_involved(principal, instance):
        raise HTTPException(status_code=404, detail="instance not found")
    return instance


class DisplayFieldsRequest(BaseModel):
    element_ids: list[str] = Field(default_factory=list, examples=[["bestellnr", "lieferant"]])


def _read_gate(principal: Principal) -> None:
    """The read floor (viewer and up) for endpoints that need the principal."""

    if not principal.roles & {"viewer", "operator", "modeler", "admin"}:
        raise HTTPException(status_code=403, detail="forbidden")


def _escalation_sweep(now: datetime | None = None) -> int:
    """Fire due escalation stages across all running instances (T3/E9).

    The lazy boundary timer of the escalation feature: called before
    the worklist reads and by ``POST /admin/escalations/sweep`` -- there is
    deliberately no background scheduler. Idempotent through the persisted
    per-activation stage counter; the mail outbox additionally dedups per
    ``esc|instance|node|stage|activation``. Returns the number of stages
    fired in this pass. Cheap when nothing escalates: instances without
    policies are skipped after one dict check.
    """

    moment = now or datetime.now(UTC)
    fired_total = 0
    absent = _current_absent_agents()
    for instance_id in _instances.list_ids():
        instance = _instances.get(instance_id)
        if instance is None or instance.state is not InstanceState.RUNNING:
            continue
        schema = _effective_schema_for(instance)
        if not schema.escalation_policies:
            continue
        changed = False
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
            # Shared due computation with the worklist bands (rule B): once
            # claimed, a modelled processing duration counts from the claim.
            # The pause credit (net time, opt-in) shifts the due instant the
            # same way it shifts the bands -- one computation, two consumers.
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
            due = origin + timedelta(seconds=target)
            already = instance.escalated_stages.get(node_id, 0)
            for index in range(already, len(policy.stages)):
                stage = policy.stages[index]
                if moment < due + timedelta(seconds=stage.after_seconds):
                    break  # stages are strictly ascending (T3b)
                _fire_escalation_stage(instance, schema, node_id, index, stage, absent)
                instance.escalated_stages[node_id] = index + 1
                changed = True
                fired_total += 1
        if changed:
            _instances.put(instance)
    return fired_total


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
    _dispatch_mail_outbox()


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
    """

    _escalation_sweep()  # lazy boundary timer (T3/E9): fire due stages first
    tasks: list[OpenTask] = []
    absent = _current_absent_agents()
    for instance_id in _instances.list_ids():
        instance = _instances.get(instance_id)
        if instance is None or instance.state is not InstanceState.RUNNING:
            continue
        if instance.is_test:
            continue
        schema = _effective_schema_for(instance)
        ctx = _time_context(instance, schema)
        for task in assignment.open_tasks(schema, instance, ctx, absent_agents=absent):
            if task.claimed_by is not None and task.claimed_by != agent_id:
                continue  # withdrawn: someone else took it over
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


# --- absence / deputy substitution ---------------------------------------
# (endpoints: procworks/api_execution.py)


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
    :raises HTTPException: 422 when a real instance is changed without reason.
    """

    text = (reason or "").strip()
    if not text and not instance.is_test:
        raise HTTPException(status_code=422, detail=ADHOC_REASON_REQUIRED)
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
    cleaned = (reason or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=422,
            detail=(
                "Aufsichtseingriff: Diese Werte gehören zu keinem Schritt, den du "
                "gerade bearbeitest. Bitte eine Begründung für die Korrektur "
                "angeben – sie wird im Audit-Verlauf festgehalten."
            ),
        )
    return cleaned


def _set_instance_data(
    instance_id: str, req: SetDataRequest, principal: Principal
) -> dict[str, object]:
    """Shared body of both data endpoints: authorise, type-check, store, audit.

    Order matters: first the instance must be readable for the caller (a
    foreign one is 404, so a D3 finding never describes the data of a case the
    caller may not read), then the D3
    type check (a malformed request is a 422 regardless of who sends it), then
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
    supervision = _authorize_data_write(
        principal, instance, schema, req.values.keys(), req.reason
    )
    updated = instance.model_copy(deep=True)
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


#: Schemas/prefixes of database *system* catalogues. The sample read never
#: reads them, whatever the connector's catalogue says.
_SYSTEM_ENTITY_PREFIXES = (
    "sqlite_",
    "information_schema.",
    "pg_catalog.",
    "pg_",
    "sys.",
    "mysql.",
    "performance_schema.",
)


def _check_sample_entity(connector_id: str, entity: str) -> None:
    """Admit only an entity the connector itself offers (sample-read allowlist).

    The sample read offers exactly what ``/entities`` offers -- the business
    tables -- and never a system catalogue such as ``sqlite_master``. Rules:

    * system catalogues (:data:`_SYSTEM_ENTITY_PREFIXES`) → 422, always;
    * an unqualified name must be in the connector's catalogue
      (``_connections.entities``) → 422 otherwise;
    * a schema-qualified name (``schema.table``) is admitted when its schema is
      no system schema -- SQL catalogues list only the default schema, and a
      customer integration regularly lives in another one;
    * a connector that cannot introspect (empty catalogue) keeps the manual
      entry that worked before -- the identifier whitelist in ``dal`` still
      applies.

    A catalogue read that fails is the external system's fault (502).
    """

    lowered = entity.lower()
    if lowered.startswith(_SYSTEM_ENTITY_PREFIXES):
        raise HTTPException(
            status_code=422,
            detail={"message": f"Systemtabellen lassen sich nicht testlesen: {entity}"},
        )
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

