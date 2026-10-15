# SPDX-License-Identifier: BUSL-1.1
"""Administration endpoints: reset, backups, mail outbox, escalation sweep and
the check of the audit hash chain.

All of them are admin-only operational actions on the running system; none
changes a process model.

Shared state (stores, audit log, auth backend, licensing, runtimes) and the
shared helpers live in :mod:`procworks.api` and are read through the module
at call time (``api._store``, ``api._get_or_404(...)``), so tests and
configuration can replace them. Only what FastAPI needs when the route is
declared -- request/response models, role gates and dependencies in the
signatures -- is imported by name. :mod:`procworks.api` includes ``router``
into the app after all of its own definitions, in a fixed order that is
the route order.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from procworks import api, backups, demo, demo_o2c
from procworks.api import (
    MailOutboxStatus,
    ResetRequest,
    ResetResponse,
    RunNowResponse,
    _admin,
    require_role,
)
from procworks.audit import (
    CHAIN_PSEUDO_ID,
    AuditEvent,
    ChainReport,
    ChainState,
    EventType,
    anchor_record,
    check_chain,
    verify_chain,
)
from procworks.auth import Principal
from procworks.auth_password import DEFAULT_ADMIN_LOGIN

router: APIRouter = APIRouter(route_class=api.TransactionalRoute)

#: Sentence of the 422 for an anchor without a reason (callers without the
#: client's catalogue; the client words ``REQ.reason-required`` itself).
CHAIN_ANCHOR_REASON_REQUIRED = (
    "Bitte eine Begründung angeben: Wer hat die historischen Verzweigungen geprüft, "
    "und mit welchem Ergebnis?"
)


class ChainAnchorRequest(BaseModel):
    """Close the historic branches of the audit hash chain -- with a reason."""

    reason: str = Field(
        ...,
        examples=["Verzweigungen aus der Zeit vor dem Update geprüft, Inhalte unverändert"],
    )


@router.get("/admin/audit-chain", response_model=ChainReport, dependencies=[_admin])
def get_audit_chain() -> ChainReport:
    """Check the hash chain of the whole audit log (admin only, read-only).

    ``status`` ``intact`` (one line, or every historic branch anchored),
    ``open`` (historic branches from before appending was serialised wait for
    an anchor) or ``defective`` (an event was changed or removed, or a writer
    bypassed the lock) -- see :func:`procworks.audit.verify_chain`. Historic
    branches stay listed with their time after the anchor.
    """

    return check_chain(api._audit)


@router.post("/admin/audit-chain/anchor", response_model=ChainReport)
def post_audit_chain_anchor(
    req: ChainAnchorRequest, principal: Principal = Depends(require_role("admin")),
) -> ChainReport:
    """Close the open historic branches with an anchor event (admin only).

    Appends one ``CHAIN_ANCHOR`` event naming every open branch
    (``branches``) and recording it and every open dead end with its hash
    (``branch_hashes``, ``dead_ends`` -- :func:`procworks.audit.anchor_record`),
    plus the newest event checked (``checked_through``), the ``reason`` and
    the ``actor``. Nothing existing is rewritten; the branches stay visible
    in the check, marked as anchored, and the check holds the log to the
    record from then on (a removed or rewritten dead end is a defect).

    An installation anchored before dead ends were recorded has no open
    branch but open dead ends: a new anchor then records just those.

    Refused, with nothing written:

    * 422 ``REQ.reason-required`` (``what`` ``chain-anchor``) -- no visible
      reason; the anchor records a human review, the reason is its content;
    * 409 ``AUD.chain-defective`` (``defects``: their number) -- the chain has
      a defect. An anchor only acknowledges branches from concurrent writing;
      it must never cover a changed or removed event;
    * 409 ``AUD.chain-nothing-open`` -- no open historic branch and no
      unrecorded dead end.

    The check and the append run under the lock of the chain end
    (:meth:`procworks.audit.AuditLog.append_checked`): two anchors sent at
    the same moment are written one after another, and the second finds
    nothing open (409). A defect includes ``historic-range-changed`` -- the
    history from before the update no longer matches what the update
    recorded; an anchor must never record such a state as reviewed.

    :returns: the check after the anchor (``status`` ``intact``).
    """

    reason = api._visible_reason(req.reason)
    if reason is None:
        raise api._reason_required("chain-anchor", CHAIN_ANCHOR_REASON_REQUIRED)
    actor = principal.subject or "anonymous"

    def decide(events: list[AuditEvent], state: ChainState) -> dict[str, str]:
        """Judge the chain as read under the lock; the anchor's detail or 409."""

        report = verify_chain(events, state)
        if report.defects:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "The audit chain has defects; an anchor would hide them.",
                    "code": "AUD.chain-defective",
                    "params": {"defects": str(len(report.defects))},
                },
            )
        record = anchor_record(events, report)
        if not record["branches"] and not record["dead_ends"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "The audit chain has no open historic branch.",
                    "code": "AUD.chain-nothing-open",
                    "params": {},
                },
            )
        return {
            **record,
            "checked_through": str(report.head_seq),
            "reason": reason,
            "actor": actor,
        }

    # Check and append under the lock of the chain end: a second anchor at
    # the same moment waits, then sees this one and finds nothing open.
    api._audit.append_checked(decide, EventType.CHAIN_ANCHOR, CHAIN_PSEUDO_ID, CHAIN_PSEUDO_ID)
    return check_chain(api._audit)


def _example_logins(*, load_demo: bool, load_o2c: bool) -> list[str]:
    """The logins the loaded example data sets bring, from their own tables.

    The dialog after loading names a few of them as examples. It used to name
    two fixed logins of the basic demo -- after loading only the
    Order-to-Cash set those did not exist. Taken from
    :data:`procworks.demo.DEMO_USERS` and
    :data:`procworks.demo_o2c.O2C_USERS`, the same tables the seed uses, so
    the list cannot drift from what was created.

    :param load_demo: the basic demo was loaded.
    :param load_o2c: the Order-to-Cash set was loaded.
    :returns: the logins in table order, basic demo first; empty when
        neither was loaded. The reset removes every other login before the
        seed (only the acting admin and ``admin`` stay, neither is in the
        tables), so each named login was created with the reported password.
    """

    logins: list[str] = []
    if load_demo:
        logins += [login for login, *_ in demo.DEMO_USERS]
    if load_o2c:
        logins += [login for login, *_ in demo_o2c.O2C_USERS]
    return logins


@router.post("/admin/reset", response_model=ResetResponse)
@api.excludes_all_writers  # no case may be saved beside the wipe (see there)
def post_admin_reset(
    req: ResetRequest, principal: Principal = Depends(require_role("admin")),
) -> ResetResponse:
    """Wipe all process data to zero, optionally reloading the demo (admin only).

    This is a deliberately destructive maintenance action: every schema,
    instance, audit event and shared org model is removed. In password mode all
    logins are dropped too, except the acting admin and the bootstrap ``admin``
    so nobody gets locked out. With ``load_demo`` the built-in demo world is
    loaded afterwards (the same data that ships for a guided first look); with
    ``load_o2c`` the large Order-to-Cash data set (own org, six schemas, nine
    orders). Both flags are independent and may be combined.

    Runs while no other request writes (:func:`procworks.api.excludes_all_writers`):
    it waits for the requests already under way, and requests arriving
    meanwhile wait until the reset is stored. A case started at the same
    moment therefore either ends before the reset (and is wiped with the
    rest) or after it (and finds its model gone, 404) -- never a case left
    without its model.
    """

    api._store.clear()
    api._instances.clear()
    api._org_store.clear()
    api._audit.clear()
    api._mail_outbox_store.clear()
    api._absence_store.clear()
    # Drop user-created templates. Built-in templates are code, so they survive.
    api._template_store.clear()
    # The id reservations (``_id_claims``) are kept on purpose: another API
    # process may hold a reservation it has not saved yet, and a process whose
    # counter lags behind could otherwise claim that id a second time.
    # Drop licenses/bindings/anchor. The install id deliberately survives (it
    # identifies the installation, and bought packs are bound to it).
    api._license_store.clear()

    keep = {DEFAULT_ADMIN_LOGIN}
    if principal.subject:
        keep.add(principal.subject)
    api._wipe_users(keep)

    password = api._example_password()
    if req.load_demo:
        api._seed_demo(password)
    if req.load_o2c:
        api._seed_o2c(password)
    seeded_logins = (req.load_demo or req.load_o2c) and api._password_login_active()

    return ResetResponse(
        demo_loaded=req.load_demo,
        o2c_loaded=req.load_o2c,
        example_password=password if seeded_logins else None,
        example_logins=(
            _example_logins(load_demo=req.load_demo, load_o2c=req.load_o2c)
            if seeded_logins else []
        ),
        schemas=len(api._store.list_ids()),
        instances=len(api._instances.list_ids()),
        org_models=len(api._org_store.list_ids()),
        users=api._user_count(),
    )


@router.get("/admin/backups", response_model=backups.BackupsStatus, dependencies=[_admin])
def get_admin_backups() -> backups.BackupsStatus:
    """List known datensicherungen and their status (admin only, read-only).

    Reads only the metadata index the backup scheduler publishes into the shared
    control directory -- never the dump volume itself (the API has no access to
    the dumps, per the backup security rule). Reports ``available = false``
    when no control directory is configured or nothing has been published yet,
    so the GUI can show a clear "not configured" state instead of an error.
    """
    return backups.load_status(backups.control_dir())


@router.post("/admin/backups/run-now", response_model=RunNowResponse, dependencies=[_admin])
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


@router.get("/admin/mail-outbox", response_model=MailOutboxStatus, dependencies=[_admin])
def get_admin_mail_outbox() -> MailOutboxStatus:
    """Read the durable mail outbox: queued notifications and their state (admin).

    Read-only ops view of the modelled e-mail notifications (rule group N). Shows
    per-state counts and the (data-minimised) entries so an operator can see what
    is pending, being retried, delivered or dead-lettered -- and whether an SMTP
    sender is configured at all. Never exposes recipient addresses or the body.
    """

    return api._mail_outbox_status()


@router.post(
    "/admin/mail-outbox/dispatch", response_model=MailOutboxStatus, dependencies=[_admin]
)
@api.manages_own_units  # sends mail: no transaction around the SMTP calls
def post_admin_mail_outbox_dispatch() -> MailOutboxStatus:
    """Retry all due queued notifications now, then return the outbox (admin).

    A manual flush for operators: after fixing an SMTP outage, this drives the
    durable outbox once so ``PENDING``/``FAILED`` entries are re-attempted (and
    ``mail.sent``/``mail.failed`` audit events are recorded) without waiting for
    the next process advance. ``DEAD`` entries stay dead-lettered.
    """

    api._dispatch_mail_outbox()
    return api._mail_outbox_status()


@router.post("/admin/escalations/sweep", dependencies=[_admin])
@api.manages_own_units  # one unit per escalated case (see api._escalation_sweep)
def post_admin_escalation_sweep() -> dict[str, int]:
    """Fire all currently due escalation stages now (T3/E9, admin).

    Due stages normally fire in the background: every API process runs the
    escalation sweep in a fixed rhythm (``PROCWORKS_ESCALATION_SWEEP_SECONDS``,
    default 60 s, ``0`` = off in that process); reading a worklist does not
    sweep. This endpoint is an additional trigger -- for a process with the
    background sweep switched off, or to fire due stages at once instead of
    at the next tick. Unbounded (one pass over every due case), idempotent --
    a stage fires at most once per activation, also with several processes
    sweeping at the same time.

    The answer is ``{"fired": <stages fired>, "skipped": <due cases not
    escalated>}``. A case is skipped -- and logged with its id -- when it
    changed meanwhile or its escalation failed (e.g. a database collision or
    a damaged stored record); the other cases are escalated all the same, and
    the next pass tries the skipped ones again. A skipped case is therefore
    never an error answer of this endpoint. A **database outage** is: the
    pass stops at once and the answer is ``503 DB.unavailable`` (or
    ``DB.outcome-unknown`` for a broken-off commit), as for every request --
    the stages fired before it stay stored.
    """

    outcome = api._sweep_escalations(datetime.now(UTC), None)
    return {"fired": outcome.fired, "skipped": outcome.skipped}
