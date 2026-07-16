# SPDX-License-Identifier: BUSL-1.1
"""Demo broker: mints one isolated demo instance per visitor.

A tiny, stateless HTTP service behind the landing page's "Start test version"
button. Responsibilities:

1. Enforce abuse/cost limits **without requiring a Captcha** (see below).
2. Provision one isolated demo instance via the :class:`ProvisionPort`.
3. Return the visitor's demo URL.

**Bounding instances without a Captcha.** Three layers:

* **Authoritative concurrent cap** -- before provisioning, the broker asks the
  platform how many demo instances are *actually* live (``provisioner.list_ids``)
  and refuses at ``DEMO_MAX_ACTIVE``. Because the count comes from the platform,
  it is the true ceiling and survives a broker restart (no in-memory state to
  lose). This is what makes "infinitely many instances" impossible.
* **Per-IP rate limit** -- a sliding window (``DEMO_MAX_PER_IP`` per
  ``DEMO_IP_WINDOW_SECONDS``) so one actor cannot drain the global budget.
* **Global daily cap** -- a coarse absolute ceiling per UTC day
  (``DEMO_MAX_PER_DAY``).

Together with the reaper's hard TTL (which bounds each instance's lifetime),
the number and cost of instances is bounded from every direction. A Captcha is
**optional**: set ``CAPTCHA_SECRET`` to require Cloudflare Turnstile; unset (the
default) it is skipped.

This is a deployment artifact, not part of the correctness core.

Run locally against the fake provisioner:
    uvicorn app:app --port 8080
    curl -X POST localhost:8080/trial -H 'content-type: application/json' -d '{}'
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from mailer import LeadNotifier, create_lead_notifier
from provision import (
    DemoInstance,
    FlyProvisioner,
    InMemoryProvisioner,
    ProvisionPort,
    new_trial_id,
)
from pydantic import BaseModel


def _select_provisioner() -> ProvisionPort:
    """Pick the provisioner from the environment.

    ``DEMO_PROVISIONER=fly`` uses the real Fly skeleton; anything else (the
    default) uses the in-memory fake, so the broker is runnable out of the box
    for local development without any cloud credentials.
    """
    if os.environ.get("DEMO_PROVISIONER", "").lower() == "fly":
        return FlyProvisioner()
    return InMemoryProvisioner()


class _AbuseGuard:
    """Rate limits that need no Captcha: per-IP sliding window + global daily cap.

    These are the *rate*-shaping layers. The hard ceiling on concurrent
    instances (the real cost driver) is enforced separately and authoritatively
    from the platform in the request handler, so in-process state here is
    acceptable: even a broker restart cannot exceed the concurrent cap.

    ``admit(ip)`` returns ``None`` when allowed (and records the hit), else a
    short human reason for the 429. ``refund(ip)`` undoes a hit when the
    subsequent provisioning fails, so a transient platform error does not eat a
    visitor's quota.
    """

    def __init__(self, *, max_per_day: int, max_per_ip: int, ip_window_seconds: int) -> None:
        self._max_per_day = max_per_day
        self._max_per_ip = max_per_ip
        self._ip_window = ip_window_seconds
        self._today_count = 0
        self._day = time.gmtime().tm_yday
        self._ip_hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def admit(self, ip: str) -> str | None:
        with self._lock:
            now = time.time()
            today = time.gmtime().tm_yday
            if today != self._day:  # new UTC day -> reset the daily counter
                self._day = today
                self._today_count = 0
            if self._today_count >= self._max_per_day:
                return "Das Test-Kontingent für heute ist erschöpft -- bitte morgen wieder."
            hits = self._ip_hits[ip]
            while hits and now - hits[0] > self._ip_window:
                hits.popleft()
            if len(hits) >= self._max_per_ip:
                return "Zu viele Startversuche von deinem Anschluss -- bitte kurz warten."
            hits.append(now)
            self._today_count += 1
            return None

    def refund(self, ip: str) -> None:
        with self._lock:
            self._today_count = max(0, self._today_count - 1)
            hits = self._ip_hits.get(ip)
            if hits:
                hits.pop()


class _Metrics:
    """Thread-safe, in-process counters for the demo broker (observability, D4).

    Best-effort and **non-persistent** (like the API's ``metrics.py``): a broker
    restart resets them, and they never influence request handling -- purely
    observational. The *live* active-instance count is deliberately **not** kept
    here; it is read authoritatively from the provisioner at read time (that is
    the real cost driver, and stored counters would drift). What lives here are
    monotonic since-start tallies: how many trials started, and why the rest were
    turned away (cap / rate limit / provisioning failure / Captcha), plus how many
    instances the reaper reclaimed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._counters: dict[str, int] = defaultdict(int)

    def incr(self, key: str, n: int = 1) -> None:
        """Add ``n`` (default 1) to the named counter. Never raises."""
        if n <= 0:
            return
        with self._lock:
            self._counters[key] += n

    def snapshot(self) -> dict[str, int]:
        """Return a copy of the current counters (safe to read concurrently)."""
        with self._lock:
            return dict(self._counters)

    @property
    def started_at(self) -> float:
        """Wall-clock time the counters began (process start)."""
        return self._started_at


#: Cloudflare Turnstile server-side verification endpoint.
_TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def _verify_captcha(token: str, *, remote_ip: str | None = None) -> bool:
    """Verify the anti-abuse Captcha token (Cloudflare Turnstile).

    Captcha is **optional**. When ``CAPTCHA_SECRET`` is unset (the default), it is
    disabled and this returns True -- the broker relies on the rate limits and the
    authoritative concurrent cap instead. When the secret *is* set, POST the token
    to Turnstile's ``siteverify`` endpoint and return its ``success`` flag,
    **fail-closed**: any transport/parse error or missing token returns False, so
    a broken verifier never waves visitors through.

    Args:
        token: the Turnstile response token from the widget (empty when disabled).
        remote_ip: the visitor's IP, forwarded to Turnstile when available.
    """
    secret = os.environ.get("CAPTCHA_SECRET")
    if not secret:
        return True  # Captcha disabled -> allow; rate limits + active cap protect
    if not token:
        return False
    form = {"secret": secret, "response": token}
    if remote_ip:
        form["remoteip"] = remote_ip
    data = urllib.parse.urlencode(form).encode()
    try:
        req = urllib.request.Request(_TURNSTILE_VERIFY_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed host)
            body = json.loads(resp.read())
    except (urllib.error.URLError, ValueError):  # network error / bad JSON -> deny
        return False
    return bool(body.get("success") is True)


app = FastAPI(title="ProcWorks Demo Broker", summary="Mints one demo instance per visitor.")

# Cross-origin POSTs come from two places: the "Start test version" contact form
# on the marketing site (POST /trial) and the survey inside each demo instance
# (POST /feedback, served from https://trial-<id>.fly.dev). Pin the site
# origin(s) via BROKER_CORS_ORIGINS; allow the per-visitor demo subdomains via a
# regex (BROKER_CORS_ORIGIN_REGEX). Only POST is exposed, so this stays narrow.
_cors_origins = [
    o.strip()
    for o in os.environ.get("BROKER_CORS_ORIGINS", "https://procworks.de").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=os.environ.get(
        "BROKER_CORS_ORIGIN_REGEX", r"https://trial-[0-9a-f]+\.fly\.dev"
    ),
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["content-type"],
)

_provisioner: ProvisionPort = _select_provisioner()
#: Authoritative hard ceiling on concurrently live demo instances. Checked
#: against the platform's real count (``list_ids``) on every trial, so it cannot
#: be exceeded even across broker restarts.
_MAX_ACTIVE = int(os.environ.get("DEMO_MAX_ACTIVE", "20"))
#: Hard lifetime of a demo instance. The broker reaps anything older than this
#: opportunistically on each /trial (see :func:`_reap_expired`), so an abandoned
#: demo -- e.g. a visitor who just closed the tab -- is cleaned up when the next
#: visitor needs a slot. 0 disables reaping. Default two hours.
_TTL_SECONDS = int(os.environ.get("DEMO_TTL_SECONDS", str(2 * 3600)))
_guard = _AbuseGuard(
    max_per_day=int(os.environ.get("DEMO_MAX_PER_DAY", "500")),
    max_per_ip=int(os.environ.get("DEMO_MAX_PER_IP", "3")),
    ip_window_seconds=int(os.environ.get("DEMO_IP_WINDOW_SECONDS", "3600")),
)
#: Since-start observability counters (see :class:`_Metrics`), surfaced read-only
#: on ``GET /admin/metrics``. Non-persistent, best-effort, never affects handling.
_metrics = _Metrics()
#: Relays each lead + feedback to the operator inbox by e-mail and stores nothing
#: (data minimisation). ``configured is False`` on a broker without SMTP (local
#: dev) -> the relay is skipped, not enforced. See :mod:`mailer`.
_notifier: LeadNotifier = create_lead_notifier()

#: Length caps for the contact-gate fields (defensive against oversized payloads).
_MAX_NAME = 120
_MAX_COMPANY = 160
_MAX_EMAIL = 254
_MAX_COMMENT = 4000


def _valid_email(email: str) -> bool:
    """Lightweight e-mail plausibility check (no external validator dependency).

    Requires a single ``@`` with non-empty local part and a dotted domain. This
    is a *format* gate for the contact form, not full RFC validation -- the real
    confirmation is that the operator can reach the address.
    """
    email = email.strip()
    if email.count("@") != 1 or len(email) > _MAX_EMAIL:
        return False
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")
#: Shared secret guarding the scheduled-reaper poke (``POST /admin/reap``). When
#: unset (the default) the endpoint is *disabled* (404) so there is no open,
#: destructive trigger; set it to enable an external scheduler to sweep expired
#: demos by waking this scale-to-zero broker with one authenticated request.
_ADMIN_TOKEN = os.environ.get("DEMO_ADMIN_TOKEN", "")


def _reap_expired() -> list[str]:
    """Best-effort: destroy demo instances older than the hard TTL.

    Runs opportunistically at the start of every ``/trial`` (and on demand via
    ``POST /admin/reap``) so abandoned demos are reclaimed when a new visitor
    needs a slot -- the broker is itself scale-to-zero, so there is no always-on
    scheduler to rely on. Deliberately conservative: only instances whose age is
    *known* and past the TTL are destroyed (a transient age-lookup failure never
    nukes a fresh instance). Never raises -- a reap failure must not block a
    legitimate trial, and the authoritative active-cap still bounds the total.
    Returns the ids actually destroyed (empty when reaping is disabled).

    The standalone ``reaper/reaper.py`` runs the same policy over the same seam
    for schedulers that would rather invoke a CLI than poke the broker.
    """
    if _TTL_SECONDS <= 0:
        return []
    destroyed: list[str] = []
    try:
        ids = _provisioner.list_ids()
    except Exception:  # noqa: BLE001 - counting failed; skip, cap still protects
        return destroyed
    for iid in ids:
        try:
            age = _provisioner.instance_age_seconds(iid)
            if age is not None and age >= _TTL_SECONDS:
                _provisioner.destroy(iid)
                destroyed.append(iid)
        except Exception:  # noqa: BLE001 - skip this one, keep reaping the rest
            continue
    _metrics.incr("reaped", len(destroyed))
    return destroyed


def _require_admin(request: Request) -> None:
    """Guard the reaper-poke endpoint with the shared ``DEMO_ADMIN_TOKEN``.

    Off by default: with no token configured the endpoint must not exist (404),
    so a destructive sweep can never be triggered anonymously. When a token is
    set, require it as ``Authorization: Bearer <token>`` (or ``X-Admin-Token``)
    and compare in constant time. Raises the appropriate HTTPException; returns
    only when the caller is authorised.
    """
    if not _ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="not found")
    header = request.headers.get("authorization", "")
    presented = (
        header[7:].strip()
        if header[:7].lower() == "bearer "
        else request.headers.get("x-admin-token", "")
    )
    if not hmac.compare_digest(presented, _ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")


class TrialRequest(BaseModel):
    """Contact-gate payload for POST /trial (a demo requires a lead).

    The demo is gated behind a short contact form: ``name``, ``company`` and
    ``email`` are required and ``consent`` (the visitor agreeing that their data
    is processed to provide/support the test access) must be True -- otherwise no
    demo is minted. ``marketing_consent`` is a **separate, optional** opt-in for
    follow-up contact (kept apart to respect the DSGVO Kopplungsverbot: the demo
    must not hinge on marketing consent). ``captcha_token`` stays optional (only
    needed when a Turnstile secret is configured).
    """

    captcha_token: str | None = None
    name: str = ""
    company: str = ""
    email: str = ""
    consent: bool = False
    marketing_consent: bool = False


class TrialResponse(BaseModel):
    """The visitor's freshly provisioned demo."""

    trial_id: str
    url: str
    state: str


class FeedbackRequest(BaseModel):
    """Post-demo survey payload for POST /feedback (all fields optional).

    ``trial_id`` (the demo the visitor just used) lets the operator correlate the
    feedback with the earlier lead mail from the same session -- both carry it, so
    no server-side join or storage is needed. Everything else is a survey answer.
    """

    trial_id: str = ""
    role: str = ""
    satisfaction: int | None = None
    cbc_importance: int | None = None
    ease: int | None = None
    intent: str = ""
    comment: str = ""


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe (the broker is itself scale-to-zero)."""
    return {"status": "ok"}


@app.post("/admin/reap")
def admin_reap(request: Request) -> dict[str, object]:
    """Scheduled-reaper backstop: sweep every demo past the hard TTL, on demand.

    Runs the exact same policy as the opportunistic ``/trial`` reap, but on a
    scheduler's poke instead of a visitor's -- so abandoned demos are reclaimed
    even during zero-traffic periods, without a separate always-on reaper
    service. Guarded by ``DEMO_ADMIN_TOKEN`` (see :func:`_require_admin`);
    disabled (404) when no token is configured. Returns the destroyed ids so the
    scheduler's logs show what was reclaimed.
    """
    _require_admin(request)
    destroyed = _reap_expired()
    return {"destroyed": destroyed, "count": len(destroyed)}


@app.get("/admin/metrics")
def admin_metrics(request: Request) -> dict[str, object]:
    """Read-only observability snapshot for the demo broker (D4).

    Same guard as ``/admin/reap`` (``DEMO_ADMIN_TOKEN``; 404 when unset). Returns
    the **live** active-instance count (read authoritatively from the platform,
    the real cost driver) alongside the current caps and the since-start
    counters. Best-effort: if the platform count cannot be read, ``active`` is
    ``null`` rather than failing the probe. Numbers only -- no fabricated cost
    figure; ``active`` and ``trials_started`` are what a cost view is built from.
    """
    _require_admin(request)
    try:
        active: int | None = len(_provisioner.list_ids())
    except Exception:  # noqa: BLE001 - never fail the metrics read on a count error
        active = None
    snap = _metrics.snapshot()
    return {
        "active": active,
        "max_active": _MAX_ACTIVE,
        "ttl_seconds": _TTL_SECONDS,
        "uptime_seconds": int(time.time() - _metrics.started_at),
        "counters": {
            "trials_started": snap.get("trials_started", 0),
            "trials_rejected_cap": snap.get("trials_rejected_cap", 0),
            "trials_rejected_ratelimit": snap.get("trials_rejected_ratelimit", 0),
            "trials_rejected_gate": snap.get("trials_rejected_gate", 0),
            "trials_failed": snap.get("trials_failed", 0),
            "captcha_rejected": snap.get("captcha_rejected", 0),
            "leads_relayed": snap.get("leads_relayed", 0),
            "feedback_received": snap.get("feedback_received", 0),
            "reaped": snap.get("reaped", 0),
        },
    }


@app.post("/trial", response_model=TrialResponse)
def start_trial(req: TrialRequest, request: Request) -> TrialResponse:
    """Provision one isolated demo instance behind the contact gate.

    Order: optional Captcha -> **contact gate** (name/company/e-mail + consent)
    -> **authoritative concurrent cap** -> per-IP + daily rate limit -> **relay
    the lead** -> provision. The lead is relayed *before* provisioning so every
    demo that boots has a delivered lead; if the relay is configured but fails,
    the trial is refused (503, rate-limit refunded) rather than silently dropping
    the lead. On a local/dev broker without SMTP the relay is skipped. Limits
    return 429; a missing field/consent returns 422.
    """
    remote_ip = request.client.host if request.client else "unknown"
    if not _verify_captcha(req.captcha_token or "", remote_ip=remote_ip):
        _metrics.incr("captcha_rejected")
        raise HTTPException(status_code=400, detail="captcha verification failed")

    # Contact gate: a demo requires a lead. Validate BEFORE consuming any slot or
    # rate-limit quota, so a malformed form never costs the visitor a real try.
    name = req.name.strip()[:_MAX_NAME]
    company = req.company.strip()[:_MAX_COMPANY]
    email = req.email.strip()
    if not name or not company or not _valid_email(email) or not req.consent:
        _metrics.incr("trials_rejected_gate")
        raise HTTPException(
            status_code=422,
            detail="Bitte Name, Firma, eine gültige E-Mail angeben und der "
            "Verarbeitung für den Testzugang zustimmen.",
        )

    # First reclaim any expired demos (e.g. abandoned tabs), so the active count
    # below reflects only genuinely live instances and freed slots are reusable.
    _reap_expired()

    # Authoritative ceiling: how many demos are ACTUALLY live right now?
    try:
        active = len(_provisioner.list_ids())
    except Exception:  # noqa: BLE001 - if we cannot count, refuse rather than risk a flood
        _metrics.incr("trials_failed")
        raise HTTPException(status_code=503, detail="demo service unavailable, try again") from None
    if active >= _MAX_ACTIVE:
        _metrics.incr("trials_rejected_cap")
        raise HTTPException(
            status_code=429,
            detail="Gerade sind alle Test-Plätze belegt -- bitte in ein paar Minuten erneut.",
        )

    reason = _guard.admit(remote_ip)
    if reason is not None:
        _metrics.incr("trials_rejected_ratelimit")
        raise HTTPException(status_code=429, detail=reason)

    trial_id = new_trial_id()

    # Relay the lead first: a booted demo must always have a delivered lead. A
    # relay failure refunds the rate-limit hit and refuses the trial (no silent
    # data loss). Skipped when no SMTP is configured (local/dev).
    if _notifier.configured:
        try:
            _notifier.send_lead(
                name=name,
                company=company,
                email=email,
                marketing_consent=bool(req.marketing_consent),
                trial_id=trial_id,
            )
        except Exception:  # noqa: BLE001 - undeliverable lead -> refuse, do not drop it
            _guard.refund(remote_ip)
            _metrics.incr("trials_failed")
            raise HTTPException(
                status_code=503,
                detail="Testzugang konnte gerade nicht eingerichtet werden -- bitte erneut.",
            ) from None
        _metrics.incr("leads_relayed")

    try:
        instance: DemoInstance = _provisioner.create(trial_id=trial_id)
    except Exception:  # noqa: BLE001 - any provisioning failure refunds the rate-limit hit
        _guard.refund(remote_ip)
        _metrics.incr("trials_failed")
        raise HTTPException(status_code=503, detail="demo could not be started, try again") from None

    _metrics.incr("trials_started")
    return TrialResponse(trial_id=trial_id, url=instance.url, state=instance.state)


@app.post("/feedback")
def submit_feedback(req: FeedbackRequest) -> dict[str, str]:
    """Relay one post-demo survey submission to the operator (best-effort).

    Unlike the lead gate, feedback is a nice-to-have: it is **best-effort and
    never fails the visitor**. Counted always; relayed by e-mail when SMTP is
    configured, and a relay error is swallowed (the visitor still sees a thank
    you). Stores nothing -- the mail is the record, correlated to the lead via
    ``trial_id``.
    """
    _metrics.incr("feedback_received")
    if _notifier.configured:
        try:
            _notifier.send_feedback(
                trial_id=(req.trial_id.strip() or "unbekannt")[:64],
                answers={
                    "Rolle": req.role.strip()[:_MAX_NAME],
                    "Gesamteindruck (1-5)": req.satisfaction,
                    "CbC-Wichtigkeit (1-5)": req.cbc_importance,
                    "Bedienbarkeit (1-5)": req.ease,
                    "Einsatz-Absicht": req.intent.strip()[:_MAX_NAME],
                    "Freitext": req.comment.strip()[:_MAX_COMMENT],
                },
            )
        except Exception:  # noqa: BLE001 - feedback must never fail the visitor
            pass
    return {"status": "ok"}
