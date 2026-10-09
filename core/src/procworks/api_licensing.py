# SPDX-License-Identifier: BUSL-1.1
"""Licensing / agent metering endpoints (dormant unless enforced).

The endpoints always answer. While licensing is off they report the free
contingent; only with a configured licensor public key do the mutating
actions gate work.

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

import os

from fastapi import APIRouter, HTTPException

from procworks import api
from procworks.api import (
    ActivateLicenseRequest,
    BindLicenseRequest,
    CheckoutRequest,
    CheckoutResponse,
    ClaimPollResult,
    RefreshTimeRequest,
    _admin,
    _model,
    _read,
)
from procworks.licensing import (
    AgentLicenseView,
    License,
    LicenseError,
    PendingClaim,
    SlotSummary,
    TimeAnchor,
)

router: APIRouter = APIRouter()


# --- Licensing / agent metering (dormant unless enforced) ------------------
#
# All of these endpoints exist unconditionally. While licensing is off
# (``_license.enforced == False``) they still answer, reporting the free
# contingent, so the web client can render the agent page uniformly. Only when a
# licensor public key is configured do the mutating actions actually gate work.


@router.get("/license/status", response_model=SlotSummary, dependencies=[_read])
def get_license_status() -> SlotSummary:
    """Contingent overview (slots used/total, next expiry, install id)."""

    return api._license.summary(api._all_agent_ids())


@router.get("/license/agents", response_model=list[AgentLicenseView], dependencies=[_read])
def get_license_agents() -> list[AgentLicenseView]:
    """Per-agent licensing badge data for the agent page."""

    if api._license.enforced:
        api._license.reconcile(api._all_agent_ids())
    return api._license.agent_views(sorted(api._all_agent_ids()))


@router.post("/license/checkout", response_model=CheckoutResponse, dependencies=[_model])
def post_license_checkout(req: CheckoutRequest) -> CheckoutResponse:
    """Start a purchase: hand back the hosted checkout URL and the install id.

    The actual payment/issuance runs on a *separate* licensor backend (never on
    this self-hosted instance). Its base URL is configured via
    ``PROCWORKS_LICENSE_CHECKOUT_URL``; without it the product reports that
    self-service purchase is not configured (self-hosted, offline by default).
    """

    base = os.environ.get("PROCWORKS_LICENSE_CHECKOUT_URL", "").strip()
    install_id = api._license.install_id()
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
    if claim_base and api._license.enforced:
        claim = api._license.new_claim(claim_base, slots=req.slots, months=req.months)
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


@router.post("/license/claims/poll", response_model=ClaimPollResult, dependencies=[_read])
def post_license_claims_poll() -> ClaimPollResult:
    """Run one best-effort auto-pull pass and report the resulting contingent.

    The web client calls this after starting a checkout (and on the license
    page) to fetch and activate a freshly issued pack without copy-&-paste. It
    never blocks a process step and is a no-op while licensing is not enforced
    or no claim endpoint is configured (there are simply no open claims then).
    """

    activated = api._license.poll_claims(api._claim_fetcher)
    pending = len(api._license.pending_claims())
    return ClaimPollResult(
        activated=len(activated),
        pending=pending,
        summary=api._license.summary(api._all_agent_ids()),
    )


@router.post("/license/activate", response_model=License, dependencies=[_admin])
def post_license_activate(req: ActivateLicenseRequest) -> License:
    """Install a signed license token (verifies signature + install binding)."""

    try:
        return api._license.activate(req.token)
    except LicenseError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("/license/refresh-time", response_model=TimeAnchor, dependencies=[_read])
def post_license_refresh_time(req: RefreshTimeRequest) -> TimeAnchor:
    """Advance the offline time ratchet (optionally from a trusted timestamp)."""

    return api._license.refresh_time(req.trusted_now)


@router.post(
    "/agents/{agent_id}/bind-license",
    response_model=AgentLicenseView,
    dependencies=[_model],
)
def post_bind_agent_license(agent_id: str, req: BindLicenseRequest) -> AgentLicenseView:
    """Re-home an agent onto another (valid, non-full) license contingent."""

    if agent_id not in api._all_agent_ids():
        raise HTTPException(status_code=404, detail="unknown agent")
    try:
        api._license.bind(agent_id, req.license_id, api._all_agent_ids())
    except LicenseError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return api._license.agent_view(agent_id)
