# SPDX-License-Identifier: BUSL-1.1
"""Administration endpoints: reset, backups, mail outbox and escalation sweep.

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

from fastapi import APIRouter, Depends, HTTPException

from procworks import api, backups
from procworks.api import (
    MailOutboxStatus,
    ResetRequest,
    ResetResponse,
    RunNowResponse,
    _admin,
    require_role,
)
from procworks.auth import Principal
from procworks.auth_password import DEFAULT_ADMIN_LOGIN

router: APIRouter = APIRouter()


@router.post("/admin/reset", response_model=ResetResponse)
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
def post_admin_escalation_sweep() -> dict[str, int]:
    """Fire all currently due escalation stages now (T3/E9, admin).

    The cron anchor of the lazy boundary timer: the worklist endpoints sweep
    on every read, but an unattended deployment can drive escalations
    deterministically by scheduling this endpoint (same pattern as the
    mail-outbox dispatch). Idempotent -- a stage fires at most once per
    activation.
    """

    return {"fired": api._escalation_sweep()}
