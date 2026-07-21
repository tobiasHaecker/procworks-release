# SPDX-License-Identifier: BUSL-1.1
"""Local tests for the demo broker (run: ``pytest deploy/demo/broker``).

Not part of the API package's CI suite (that runs only in ``core/``); these
cover the ops broker in isolation with mocked platform/Captcha calls -- no
network, no cloud. Requires ``pytest`` and the broker's own deps (fastapi).
"""

from __future__ import annotations

import ast
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import app as broker
import mailer
import provision as p
import pytest
from fastapi.testclient import TestClient

# The reaper lives in a sibling package; make it importable for the TTL tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reaper"))
import reaper as reaper_mod  # noqa: E402

#: A valid contact-gate payload for POST /trial (name/company/email + consent).
#: The demo is gated behind this form, so every trial test must supply it.
_LEAD = {
    "name": "Max Muster",
    "company": "Muster AG",
    "email": "max@muster.de",
    "consent": True,
}


class _Resp:
    """Minimal urlopen() context-manager stub returning a JSON body."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


# --- Turnstile Captcha verification (optional) -----------------------------


def test_captcha_disabled_allows_any(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPTCHA_SECRET", raising=False)
    assert broker._verify_captcha("") is True  # no token needed when disabled
    assert broker._verify_captcha("anything") is True


def test_captcha_enabled_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPTCHA_SECRET", "s3cr3t")
    assert broker._verify_captcha("") is False  # missing token -> deny
    monkeypatch.setattr(broker.urllib.request, "urlopen", lambda *a, **k: _Resp({"success": True}))
    assert broker._verify_captcha("tok") is True
    monkeypatch.setattr(broker.urllib.request, "urlopen", lambda *a, **k: _Resp({"success": False}))
    assert broker._verify_captcha("tok") is False


def test_captcha_fail_closed_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPTCHA_SECRET", "s3cr3t")

    def _boom(*_a: object, **_k: object) -> object:
        raise broker.urllib.error.URLError("down")

    monkeypatch.setattr(broker.urllib.request, "urlopen", _boom)
    assert broker._verify_captcha("tok") is False  # broken verifier denies


# --- Abuse guard: per-IP window + daily cap --------------------------------


def test_guard_per_ip_window() -> None:
    g = broker._AbuseGuard(max_per_day=100, max_per_ip=2, ip_window_seconds=3600)
    assert g.admit("1.1.1.1") is None
    assert g.admit("1.1.1.1") is None
    assert g.admit("1.1.1.1") is not None  # 3rd from same IP blocked
    assert g.admit("2.2.2.2") is None  # a different IP is unaffected


def test_guard_daily_cap_and_refund() -> None:
    g = broker._AbuseGuard(max_per_day=2, max_per_ip=100, ip_window_seconds=3600)
    assert g.admit("a") is None
    assert g.admit("b") is None
    assert g.admit("c") is not None  # daily cap hit across all IPs
    g.refund("b")  # a failed provision frees one
    assert g.admit("d") is None


# --- Fly app-per-visitor provisioner ---------------------------------------


def test_fly_create_makes_app_and_machine() -> None:
    calls: list[tuple[str, str, dict | None]] = []
    fly = p.FlyProvisioner(org_slug="acme", image_ref="registry.fly.io/x:demo")

    def fake(method: str, path: str, body: dict | None = None) -> object:
        calls.append((method, path, body))
        if method == "POST" and path.endswith("/machines"):
            return {"id": "m1", "state": "starting"}
        if method == "GET" and path.endswith("/machines"):
            return [{"id": "m1", "state": "started"}]
        return {}

    fly._request = fake  # type: ignore[assignment]
    fly._allocate_ips = lambda _app: None  # type: ignore[assignment]  # no live GraphQL
    inst = fly.create(trial_id="abc123")
    assert inst.instance_id == "trial-abc123"
    assert inst.url == "https://trial-abc123.fly.dev"
    assert ("POST", "/apps", {"app_name": "trial-abc123", "org_slug": "acme"}) in calls
    # The machine must be created from the demo image, never the broker's own.
    machine_body = next(b for m, path, b in calls if m == "POST" and path.endswith("/machines"))
    assert machine_body is not None
    assert machine_body["config"]["image"] == "registry.fly.io/x:demo"
    assert fly.status("trial-abc123").state == "started"
    fly.destroy("trial-abc123")
    assert ("DELETE", "/apps/trial-abc123", None) in calls


def test_image_ref_prefers_demo_over_fly_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fly injects FLY_IMAGE_REF at runtime (the machine's own image); the
    provisioner must read DEMO_IMAGE_REF so it never clones the broker itself."""
    monkeypatch.setenv("FLY_IMAGE_REF", "registry.fly.io/procworks-demo-broker:self")
    monkeypatch.setenv("DEMO_IMAGE_REF", "registry.fly.io/procworks-demo:demo")
    assert p.FlyProvisioner().image_ref == "registry.fly.io/procworks-demo:demo"


def test_image_ref_falls_back_to_fly_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy fallback: with only FLY_IMAGE_REF set, use it (non-Fly callers)."""
    monkeypatch.delenv("DEMO_IMAGE_REF", raising=False)
    monkeypatch.setenv("FLY_IMAGE_REF", "registry.fly.io/x:demo")
    assert p.FlyProvisioner().image_ref == "registry.fly.io/x:demo"


def test_fly_list_ids_filters_by_prefix() -> None:
    fly = p.FlyProvisioner(org_slug="acme")
    fly._request = lambda *a, **k: {"apps": [{"name": "trial-x"}, {"name": "prod"}]}  # type: ignore[assignment]
    assert fly.list_ids() == ["trial-x"]


# --- Reaper hard TTL --------------------------------------------------------


def test_reaper_respects_ttl() -> None:
    prov = p.InMemoryProvisioner()
    prov.create(trial_id="young")
    # ttl in the future -> nothing is old enough yet
    assert reaper_mod.reap(prov, ttl_seconds=10_000) == []
    assert prov.list_ids()  # still there
    # ttl=0 -> everything is expired
    destroyed = reaper_mod.reap(prov, ttl_seconds=0)
    assert len(destroyed) == 1
    assert prov.list_ids() == []


# --- HTTP flow: active cap + rate limit, Captcha off -----------------------


def _fresh_client(monkeypatch: pytest.MonkeyPatch, *, max_active: int = 5) -> TestClient:
    monkeypatch.delenv("CAPTCHA_SECRET", raising=False)  # Captcha off
    broker._provisioner = p.InMemoryProvisioner()
    broker._MAX_ACTIVE = max_active
    broker._guard = broker._AbuseGuard(max_per_day=100, max_per_ip=100, ip_window_seconds=3600)
    broker._metrics = broker._Metrics()  # isolate observability counters per test
    broker._notifier = broker.create_lead_notifier()  # unconfigured w/o SMTP env
    broker._END_FREES_SLOT = True  # production default; a test may override it
    return TestClient(broker.app)


def test_trial_endpoint_no_captcha_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fresh_client(monkeypatch)
    assert client.get("/health").json()["status"] == "ok"
    # A valid contact gate + Captcha disabled -> a trial is minted.
    ok = client.post("/trial", json=_LEAD)
    assert ok.status_code == 200
    body = ok.json()
    assert body["url"].startswith("https://trial-") and body["trial_id"] in body["url"]
    # CORS preflight for the marketing-site origin is allowed.
    pf = client.options(
        "/trial",
        headers={"Origin": "https://procworks.de", "Access-Control-Request-Method": "POST"},
    )
    assert pf.headers.get("access-control-allow-origin") == "https://procworks.de"


def test_trial_active_cap_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fresh_client(monkeypatch, max_active=1)
    assert client.post("/trial", json=_LEAD).status_code == 200
    # Second one: one demo is already live -> authoritative cap -> 429.
    assert client.post("/trial", json=_LEAD).status_code == 429


def test_trial_per_ip_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fresh_client(monkeypatch, max_active=100)
    broker._guard = broker._AbuseGuard(max_per_day=100, max_per_ip=1, ip_window_seconds=3600)
    assert client.post("/trial", json=_LEAD).status_code == 200
    assert client.post("/trial", json=_LEAD).status_code == 429  # same IP, 2nd blocked


def test_trial_reaps_expired_before_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """An abandoned (expired) demo is reaped on /trial, freeing its slot so a new
    visitor gets in even at a full active cap."""
    client = _fresh_client(monkeypatch, max_active=1)
    broker._TTL_SECONDS = 7200
    old = broker._provisioner.create(trial_id="stale")
    # Age it well past the TTL (abandoned tab).
    broker._provisioner._created_at[old.instance_id] = time.time() - 10_000
    # Without reaping this would 429 (cap=1); the reap frees the slot first.
    resp = client.post("/trial", json=_LEAD)
    assert resp.status_code == 200
    assert old.instance_id not in broker._provisioner.list_ids()  # reaped


def test_trial_keeps_young_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    """A still-young demo is NOT reaped: it keeps its slot, so a new trial at a
    full cap is correctly refused (the reaper only reclaims expired instances)."""
    client = _fresh_client(monkeypatch, max_active=1)
    broker._TTL_SECONDS = 7200
    young = broker._provisioner.create(trial_id="fresh")  # age ~0
    assert client.post("/trial", json=_LEAD).status_code == 429
    assert young.instance_id in broker._provisioner.list_ids()  # untouched


def test_reap_disabled_when_ttl_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEMO_TTL_SECONDS=0 disables reaping entirely (no instance is destroyed)."""
    _fresh_client(monkeypatch, max_active=5)
    broker._TTL_SECONDS = 0
    old = broker._provisioner.create(trial_id="ancient")
    broker._provisioner._created_at[old.instance_id] = time.time() - 10_000
    assert broker._reap_expired() == []  # reaping disabled
    assert old.instance_id in broker._provisioner.list_ids()  # not reaped


# --- Scheduled-reaper backstop: POST /admin/reap ---------------------------


def test_admin_reap_disabled_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DEMO_ADMIN_TOKEN -> the destructive poke endpoint does not exist (404)."""
    client = _fresh_client(monkeypatch)
    broker._ADMIN_TOKEN = ""
    assert client.post("/admin/reap").status_code == 404


def test_admin_reap_rejects_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a token configured, a missing/wrong credential is refused (401)."""
    client = _fresh_client(monkeypatch)
    broker._ADMIN_TOKEN = "s3cr3t"
    try:
        assert client.post("/admin/reap").status_code == 401  # no header
        bad = client.post("/admin/reap", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
    finally:
        broker._ADMIN_TOKEN = ""


def test_admin_reap_destroys_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """An authorised poke sweeps demos past the TTL and reports what it reaped."""
    client = _fresh_client(monkeypatch)
    broker._TTL_SECONDS = 7200
    broker._ADMIN_TOKEN = "s3cr3t"
    try:
        old = broker._provisioner.create(trial_id="stale")
        broker._provisioner._created_at[old.instance_id] = time.time() - 10_000
        young = broker._provisioner.create(trial_id="fresh")  # age ~0

        resp = client.post("/admin/reap", headers={"Authorization": "Bearer s3cr3t"})
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"destroyed": [old.instance_id], "count": 1}
        # Expired one gone, young one untouched.
        assert old.instance_id not in broker._provisioner.list_ids()
        assert young.instance_id in broker._provisioner.list_ids()
    finally:
        broker._ADMIN_TOKEN = ""


def test_admin_reap_accepts_x_admin_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The X-Admin-Token header is an accepted alternative to Bearer auth."""
    client = _fresh_client(monkeypatch)
    broker._TTL_SECONDS = 7200
    broker._ADMIN_TOKEN = "s3cr3t"
    try:
        resp = client.post("/admin/reap", headers={"X-Admin-Token": "s3cr3t"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 0  # nothing expired -> nothing reaped
    finally:
        broker._ADMIN_TOKEN = ""


# --- Observability: GET /admin/metrics (D4) --------------------------------


def test_metrics_disabled_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DEMO_ADMIN_TOKEN -> the metrics endpoint does not exist (404)."""
    client = _fresh_client(monkeypatch)
    broker._ADMIN_TOKEN = ""
    assert client.get("/admin/metrics").status_code == 404


def test_metrics_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a token set, a missing/wrong credential is refused (401)."""
    client = _fresh_client(monkeypatch)
    broker._ADMIN_TOKEN = "s3cr3t"
    try:
        assert client.get("/admin/metrics").status_code == 401
        bad = client.get("/admin/metrics", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
    finally:
        broker._ADMIN_TOKEN = ""


def test_metrics_reports_active_config_and_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trial and a rejection are reflected in the live count and counters."""
    client = _fresh_client(monkeypatch, max_active=1)
    broker._TTL_SECONDS = 7200
    broker._ADMIN_TOKEN = "s3cr3t"
    hdr = {"Authorization": "Bearer s3cr3t"}
    try:
        # One successful trial, then a second that hits the active cap (=1).
        assert client.post("/trial", json=_LEAD).status_code == 200
        assert client.post("/trial", json=_LEAD).status_code == 429

        body = client.get("/admin/metrics", headers=hdr).json()
        assert body["active"] == 1  # one demo actually live (from the provisioner)
        assert body["max_active"] == 1
        assert body["ttl_seconds"] == 7200
        assert body["uptime_seconds"] >= 0
        assert body["counters"]["trials_started"] == 1
        assert body["counters"]["trials_rejected_cap"] == 1
        assert body["counters"]["trials_rejected_ratelimit"] == 0
    finally:
        broker._ADMIN_TOKEN = ""


def test_metrics_counts_ratelimit_and_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rate-limit rejection and reaped instances are tallied too."""
    client = _fresh_client(monkeypatch, max_active=100)
    broker._TTL_SECONDS = 7200
    broker._ADMIN_TOKEN = "s3cr3t"
    broker._guard = broker._AbuseGuard(max_per_day=100, max_per_ip=1, ip_window_seconds=3600)
    hdr = {"Authorization": "Bearer s3cr3t"}
    try:
        assert client.post("/trial", json=_LEAD).status_code == 200  # started=1
        assert client.post("/trial", json=_LEAD).status_code == 429  # same IP -> rate limit

        # Age the live instance past the TTL and reap it via the admin poke.
        (live_id,) = broker._provisioner.list_ids()
        broker._provisioner._created_at[live_id] = time.time() - 10_000
        assert client.post("/admin/reap", headers=hdr).json()["count"] == 1

        counters = client.get("/admin/metrics", headers=hdr).json()["counters"]
        assert counters["trials_started"] == 1
        assert counters["trials_rejected_ratelimit"] == 1
        assert counters["reaped"] == 1
    finally:
        broker._ADMIN_TOKEN = ""


# --- Contact gate + lead/feedback relay ------------------------------------


class _FakeNotifier:
    """Records relayed leads/feedback (or raises) in place of real SMTP."""

    def __init__(
        self, *, configured: bool = True, boom: bool = False, welcome_boom: bool = False
    ) -> None:
        self._configured = configured
        self._boom = boom
        self._welcome_boom = welcome_boom
        self.leads: list[dict] = []
        self.feedback: list[dict] = []
        self.welcomes: list[dict] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def send_lead(self, **kw: object) -> None:
        if self._boom:
            raise RuntimeError("smtp down")
        self.leads.append(kw)

    def send_feedback(self, **kw: object) -> None:
        if self._boom:
            raise RuntimeError("smtp down")
        self.feedback.append(kw)

    def send_welcome(self, **kw: object) -> None:
        if self._boom or self._welcome_boom:
            raise RuntimeError("smtp down")
        self.welcomes.append(kw)


def test_trial_gate_rejects_missing_or_invalid_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    """A demo requires name + company + valid e-mail + consent; else 422, no demo."""
    client = _fresh_client(monkeypatch)
    assert client.post("/trial", json={}).status_code == 422  # nothing
    assert client.post("/trial", json={**_LEAD, "consent": False}).status_code == 422
    assert client.post("/trial", json={**_LEAD, "email": "not-an-email"}).status_code == 422
    assert client.post("/trial", json={**_LEAD, "company": ""}).status_code == 422
    # A rejected gate provisions nothing and is tallied.
    assert broker._provisioner.list_ids() == []
    assert broker._metrics.snapshot().get("trials_rejected_gate") == 4


def test_trial_relays_lead_before_provision(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid gate relays the lead (name/company/email/trial_id) then provisions."""
    client = _fresh_client(monkeypatch)
    fake = _FakeNotifier()
    broker._notifier = fake
    resp = client.post("/trial", json=_LEAD)
    assert resp.status_code == 200
    assert len(fake.leads) == 1
    lead = fake.leads[0]
    assert lead["email"] == "max@muster.de"
    assert lead["company"] == "Muster AG"
    assert lead["marketing_consent"] is False
    assert lead["trial_id"] in resp.json()["url"]
    assert broker._metrics.snapshot().get("leads_relayed") == 1


def test_trial_marketing_consent_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """The optional marketing opt-in reaches the relayed lead when ticked."""
    client = _fresh_client(monkeypatch)
    fake = _FakeNotifier()
    broker._notifier = fake
    client.post("/trial", json={**_LEAD, "marketing_consent": True})
    assert fake.leads[0]["marketing_consent"] is True


def test_trial_refused_when_lead_relay_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """An undeliverable lead must refuse the trial (503), never drop it silently."""
    client = _fresh_client(monkeypatch)
    broker._notifier = _FakeNotifier(boom=True)
    resp = client.post("/trial", json=_LEAD)
    assert resp.status_code == 503
    assert broker._provisioner.list_ids() == []  # no demo without a delivered lead


def test_trial_skips_relay_without_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local/dev broker with no SMTP still mints demos (relay simply skipped)."""
    client = _fresh_client(monkeypatch)
    broker._notifier = _FakeNotifier(configured=False)
    resp = client.post("/trial", json=_LEAD)
    assert resp.status_code == 200
    assert broker._metrics.snapshot().get("leads_relayed", 0) == 0


def test_feedback_relays_and_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feedback is counted + relayed, and a relay failure never fails the visitor."""
    client = _fresh_client(monkeypatch)
    fake = _FakeNotifier()
    broker._notifier = fake
    resp = client.post(
        "/feedback",
        json={"trial_id": "abc", "role": "IT", "satisfaction": 5, "comment": "top"},
    )
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    assert len(fake.feedback) == 1 and fake.feedback[0]["trial_id"] == "abc"
    assert broker._metrics.snapshot().get("feedback_received") == 1
    assert broker._metrics.snapshot().get("feedback_relayed") == 1
    # A relay failure must NOT surface to the visitor.
    broker._notifier = _FakeNotifier(boom=True)
    assert client.post("/feedback", json={"trial_id": "xyz"}).status_code == 200


def test_feedback_relay_failure_is_logged_and_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A swallowed relay error must still be visible -- log + counter, no answers.

    Feedback is best-effort (the visitor always sees a thank you) and nothing is
    stored, so a broken SMTP channel would discard every submission unnoticed.
    The lead path cannot hide a failure (it answers 503); this one only surfaces
    via the log line and ``feedback_relay_failed``.
    """
    client = _fresh_client(monkeypatch)
    broker._notifier = _FakeNotifier(boom=True)
    with caplog.at_level("ERROR", logger="broker"):
        resp = client.post(
            "/feedback",
            json={"trial_id": "t42", "role": "IT", "satisfaction": 5, "comment": "geheim"},
        )
    assert resp.status_code == 200  # visitor is never bothered
    snap = broker._metrics.snapshot()
    assert snap.get("feedback_received") == 1
    assert snap.get("feedback_relay_failed") == 1
    assert snap.get("feedback_relayed") is None

    text = caplog.text
    assert "t42" in text  # the trial id makes the loss traceable
    assert "geheim" not in text  # ... but never the visitor's answers


def test_ending_a_demo_frees_its_slot_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ending the demo (survey submit) hands the slot back immediately.

    With ``DEMO_END_FREES_SLOT`` on, a visitor who explicitly ends need not wait
    for the hard-TTL reaper: their instance is destroyed on ``/feedback`` and the
    freed slot lets the next visitor in at a cap that was full a moment ago.
    """
    client = _fresh_client(monkeypatch, max_active=1)
    broker._END_FREES_SLOT = True
    started = client.post("/trial", json=_LEAD)
    assert started.status_code == 200
    trial_id = started.json()["trial_id"]
    # Cap is 1 and one demo is live -> a second trial would be refused right now.
    assert client.post("/trial", json=_LEAD).status_code == 429
    # The first visitor ends their demo -> slot returned.
    ended = client.post("/feedback", json={"trial_id": trial_id, "satisfaction": 5})
    assert ended.status_code == 200 and ended.json()["status"] == "ok"
    assert f"mem-{trial_id}" not in broker._provisioner.list_ids()  # slot freed
    assert broker._metrics.snapshot().get("slots_freed_on_end") == 1
    # And the freed slot is immediately reusable.
    assert client.post("/trial", json=_LEAD).status_code == 200


def test_ending_a_demo_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the switch off the slot is kept until the reaper (pure old behaviour)."""
    client = _fresh_client(monkeypatch, max_active=1)
    broker._END_FREES_SLOT = False
    trial_id = client.post("/trial", json=_LEAD).json()["trial_id"]
    ended = client.post("/feedback", json={"trial_id": trial_id, "satisfaction": 5})
    assert ended.status_code == 200
    assert f"mem-{trial_id}" in broker._provisioner.list_ids()  # slot NOT freed
    assert broker._metrics.snapshot().get("slots_freed_on_end") is None


def test_ending_a_demo_ignores_a_malformed_trial_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crafted (non-hex) trial id never reaches the provisioner.

    The id becomes part of a platform app name / URL path, so only the known hex
    shape may drive a destroy. A malformed id is thanked like any feedback but
    frees nothing -- and cannot touch another (or a path-escaped) instance.
    """
    client = _fresh_client(monkeypatch, max_active=2)
    broker._END_FREES_SLOT = True
    trial_id = client.post("/trial", json=_LEAD).json()["trial_id"]
    live_before = set(broker._provisioner.list_ids())
    # Path-traversal-flavoured and too-short ids are both rejected by the guard.
    for bogus in ["../evil", "not-hex!", "abc", ""]:
        resp = client.post("/feedback", json={"trial_id": bogus, "satisfaction": 3})
        assert resp.status_code == 200
    assert set(broker._provisioner.list_ids()) == live_before  # nothing destroyed
    assert broker._metrics.snapshot().get("slots_freed_on_end") is None
    # The genuine trial is still endable.
    assert client.post("/feedback", json={"trial_id": trial_id}).status_code == 200
    assert f"mem-{trial_id}" not in broker._provisioner.list_ids()


def test_ending_a_demo_survives_a_destroy_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeing a slot is best-effort -- a destroy error never fails the feedback."""
    client = _fresh_client(monkeypatch)
    broker._END_FREES_SLOT = True
    trial_id = client.post("/trial", json=_LEAD).json()["trial_id"]

    def _boom(_trial: str) -> None:
        raise RuntimeError("platform hiccup")

    monkeypatch.setattr(broker._provisioner, "destroy_by_trial", _boom)
    resp = client.post("/feedback", json={"trial_id": trial_id, "satisfaction": 4})
    assert resp.status_code == 200 and resp.json()["status"] == "ok"  # visitor unaffected
    assert broker._metrics.snapshot().get("slots_freed_on_end") is None  # not counted on error


def test_lead_relay_failure_is_logged_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """An undeliverable lead is counted separately from other trial failures."""
    client = _fresh_client(monkeypatch)
    broker._notifier = _FakeNotifier(boom=True)
    assert client.post("/trial", json=_LEAD).status_code == 503
    snap = broker._metrics.snapshot()
    assert snap.get("lead_relay_failed") == 1
    assert snap.get("trials_started") is None


# --- mailer: transport + formatting ----------------------------------------


def test_notifier_unconfigured_without_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SMTP host / recipient -> notifier reports unconfigured (relay skipped)."""
    monkeypatch.delenv("LEAD_SMTP_HOST", raising=False)
    monkeypatch.delenv("LEAD_MAIL_TO", raising=False)
    assert broker.create_lead_notifier().configured is False


def test_notifier_configured_with_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a host + recipient the notifier is configured and relays for real."""
    monkeypatch.setenv("LEAD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("LEAD_MAIL_TO", "ops@example.com")
    assert broker.create_lead_notifier().configured is True


def test_notifier_send_lead_builds_consent_record() -> None:
    """send_lead builds one plain-text message doubling as the consent record."""
    captured: list[object] = []

    class _T:
        def send(self, message: object) -> None:
            captured.append(message)

    notifier = mailer.LeadNotifier(sender="from@x", recipient="ops@x", transport=_T())
    notifier.send_lead(
        name="Max Muster", company="ACME", email="a@b.de", marketing_consent=True, trial_id="t1"
    )
    assert len(captured) == 1
    msg = captured[0]
    assert msg["To"] == "ops@x"  # type: ignore[index]
    body = msg.get_content()  # type: ignore[attr-defined]
    assert "a@b.de" in body and "ACME" in body and "t1" in body
    assert "Marketing-Kontakt (optional) eingewilligt: ja" in body


def test_notifier_send_feedback_lists_answers() -> None:
    """send_feedback renders every answer and carries the trial id for matching."""
    captured: list[object] = []

    class _T:
        def send(self, message: object) -> None:
            captured.append(message)

    notifier = mailer.LeadNotifier(sender="from@x", recipient="ops@x", transport=_T())
    notifier.send_feedback(trial_id="t9", answers={"Rolle": "IT", "Gesamteindruck (1-5)": 5})
    body = captured[0].get_content()  # type: ignore[attr-defined]
    assert "t9" in body and "Rolle: IT" in body and "Gesamteindruck (1-5): 5" in body


# --- Willkommens-Mail an den Besucher --------------------------------------


def _welcome_notifier() -> tuple[mailer.LeadNotifier, list]:
    """A notifier writing into a list instead of SMTP; returns (notifier, sent)."""
    sent: list = []

    class _T:
        def send(self, message: object) -> None:
            sent.append(message)

    return mailer.LeadNotifier(sender="hallo@procworks.de", recipient="ops@x", transport=_T()), sent


def _welcome_parts(message) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """Return (plain, html) bodies of a multipart/alternative welcome mail."""
    plain = message.get_body(preferencelist=("plain",)).get_content()
    rich = message.get_body(preferencelist=("html",)).get_content()
    return plain, rich


def test_welcome_mail_goes_to_visitor_with_link_and_validity() -> None:
    """The visitor gets their own link plus when the instance disappears."""
    notifier, sent = _welcome_notifier()
    start = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    notifier.send_welcome(
        name="Max Muster",
        email="max@muster.de",
        url="https://procworks-demo-t1.fly.dev",
        ttl_seconds=7200,
        marketing_consent=False,
        started_at=start,
    )
    msg = sent[0]
    assert msg["To"] == "max@muster.de"          # visitor, not the operator inbox
    assert msg["Reply-To"] == "ops@x"            # "just reply" reaches a human
    assert "Max" in msg["Subject"]
    plain, rich = _welcome_parts(msg)
    for body in (plain, rich):
        assert "https://procworks-demo-t1.fly.dev" in body
        assert "19.07.2026" in body and "2 Stunden" in body
    assert msg.get_content_type() == "multipart/alternative"


def test_welcome_mail_is_branded_html_without_remote_assets() -> None:
    """The HTML part carries the ProcWorks look and loads nothing from outside.

    Remote images are blocked by default in most clients, so an image-based
    header would arrive as a grey box -- the design must survive without one.
    """
    notifier, sent = _welcome_notifier()
    notifier.send_welcome(
        name="Max", email="a@b.de", url="https://x.fly.dev", ttl_seconds=3600,
        marketing_consent=False,
    )
    _, rich = _welcome_parts(sent[0])
    assert mailer.BRAND["accent"] in rich and mailer.BRAND["bg"] in rich
    assert "<img" not in rich                     # no remote assets, no tracking pixel
    assert "<style" not in rich                   # every rule inlined (Gmail strips <style>)
    assert 'role="presentation"' in rich          # table layout (Outlook has no flex/grid)


def test_welcome_mail_html_is_well_formed() -> None:
    """Guard: no inlined value may break out of a ``style="..."`` attribute.

    Caught a real bug: the font stack carried double quotes ("Segoe UI"), which
    terminated every style attribute it landed in and shredded the layout. The
    parser check is the cheap way to keep that from coming back.
    """
    from html.parser import HTMLParser

    notifier, sent = _welcome_notifier()
    notifier.send_welcome(
        name="Max", email="a@b.de", url="https://x.fly.dev", ttl_seconds=7200,
        marketing_consent=True,
    )
    _, rich = _welcome_parts(sent[0])
    assert '"Segoe UI"' not in rich          # would end the enclosing attribute
    assert "font:" in rich                   # ...and the stack is actually inlined

    seen: list[str] = []

    class _P(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list) -> None:
            seen.append(tag)
            for key, value in attrs:
                # A shredded attribute shows up as junk keys like `system-ui`.
                assert key.isascii() and " " not in key, f"broken attribute: {key}={value!r}"

    _P().feed(rich)
    assert seen.count("table") >= 5 and "a" in seen


def test_welcome_mail_promo_block_requires_marketing_consent() -> None:
    """Advertising rides on the OPTIONAL opt-in, never on the mandatory consent.

    The contact form promises not to couple the demo to marketing consent
    (Kopplungsverbot), so the soliciting block must be absent without it -- while
    the link + validity, which the visitor asked for, are always there.
    """
    notifier, sent = _welcome_notifier()
    for consent in (False, True):
        notifier.send_welcome(
            name="Max", email="a@b.de", url="https://x.fly.dev", ttl_seconds=3600,
            marketing_consent=consent,
        )
    plain_off, html_off = _welcome_parts(sent[0])
    plain_on, html_on = _welcome_parts(sent[1])
    assert "30 Minuten" not in plain_off and "30 Minuten" not in html_off
    assert "30 Minuten" in plain_on and "30 Minuten" in html_on
    assert "Widerspruch" in plain_on and "Widerspruch" in html_on  # objection route
    for body in (plain_off, html_off):            # the transactional core stays
        assert "https://x.fly.dev" in body


def test_welcome_mail_ttl_phrases() -> None:
    """The lifetime reads as a human phrase, including the no-reaping case."""
    assert mailer._humanize_duration(3600) == "1 Stunde"
    assert mailer._humanize_duration(7200) == "2 Stunden"
    assert mailer._humanize_duration(5400) == "90 Minuten"
    assert mailer._humanize_duration(0) == "unbegrenzt"


def test_welcome_mail_escapes_visitor_input() -> None:
    """A name from the form is untrusted input and must not inject markup."""
    notifier, sent = _welcome_notifier()
    notifier.send_welcome(
        name="<script>alert(1)</script> Max", email="a@b.de",
        url="https://x.fly.dev", ttl_seconds=3600, marketing_consent=False,
    )
    _, rich = _welcome_parts(sent[0])
    assert "<script>" not in rich
    assert "&lt;script&gt;" in rich


def test_trial_sends_welcome_mail_after_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful trial mails the visitor their link with the configured TTL."""
    client = _fresh_client(monkeypatch)
    fake = _FakeNotifier()
    broker._notifier = fake
    resp = client.post("/trial", json=_LEAD)
    assert resp.status_code == 200
    assert len(fake.welcomes) == 1
    welcome = fake.welcomes[0]
    assert welcome["email"] == "max@muster.de"
    assert welcome["url"] == resp.json()["url"]
    assert welcome["ttl_seconds"] == broker._TTL_SECONDS
    assert broker._metrics.snapshot().get("welcome_mails_sent") == 1


def test_trial_survives_failing_welcome_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort: an undeliverable welcome mail never breaks a running demo.

    The opposite of the lead relay (fail-closed) -- here the demo already exists
    and the browser is being redirected into it.
    """
    client = _fresh_client(monkeypatch)
    broker._notifier = _FakeNotifier(welcome_boom=True)
    resp = client.post("/trial", json=_LEAD)
    assert resp.status_code == 200
    assert len(broker._provisioner.list_ids()) == 1   # demo lives on
    assert broker._metrics.snapshot().get("welcome_mail_failed") == 1


def test_dockerfile_copies_every_runtime_module_app_imports() -> None:
    """Guard: jedes lokale Modul, das ``app.py`` importiert, muss im Image liegen.

    Regressionsschutz fuer einen echten Ausfall: ``mailer.py`` wurde ergaenzt, aber
    nicht ins ``COPY`` des Dockerfiles aufgenommen -- der Container startete dann gar
    nicht mehr (``ImportError`` beim Boot), was die komplette Demo lahmlegte. Die
    Testsuite laeuft gegen das Dateisystem und haette das nie bemerkt, weil dort
    ohnehin alle Module nebeneinander liegen. Deshalb pruefen wir hier direkt das
    Dockerfile.
    """
    here = Path(__file__).parent
    dockerfile = (here / "Dockerfile").read_text(encoding="utf-8")

    # Lokale Module = *.py neben app.py, ohne die Tests selbst.
    local = {p.stem for p in here.glob("*.py") if not p.name.startswith("test_")}

    # Welche davon importiert app.py tatsaechlich?
    tree = ast.parse((here / "app.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    needed = (imported & local) | {"app"}

    copied: set[str] = set()
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("COPY "):
            for token in stripped.split()[1:-1]:
                if token.endswith(".py"):
                    copied.add(token[:-3])

    missing = needed - copied
    assert not missing, f"Dockerfile kopiert nicht: {sorted(missing)} (COPY in Dockerfile ergaenzen)"


def test_welcome_mail_bounds_the_link_promise_by_the_real_ttl() -> None:
    """Die Mail verspricht den Link genau so lange, wie er wirklich lebt.

    Der Link stirbt noch am selben Nachmittag. Stuende in der Mail ein
    unbefristetes "kommen Sie jederzeit zurueck", waere das die eine Zusage,
    die der Besucher garantiert einloest -- und dann auf eine geloeschte
    Umgebung trifft. Die Dauer muss deshalb an drei Stellen stehen und ueberall
    aus ``ttl_seconds`` abgeleitet sein, nicht hartkodiert: Betreff (der Teil,
    den man im Postfach ueberfliegt), Einstieg und Gueltigkeitsblock.
    """
    notifier, sent = _welcome_notifier()
    notifier.send_welcome(
        name="Max", email="a@b.de", url="https://x.fly.dev",
        ttl_seconds=7200, marketing_consent=False,
    )
    plain, rich = _welcome_parts(sent[0])

    assert "2 Stunden" in str(sent[0]["Subject"]), "Der Betreff nennt die Laufzeit nicht"
    assert "2 Stunden" in plain and "2 Stunden" in rich

    # Das Versprechen darf nirgends unbefristet stehen: jedes "jederzeit" muss
    # in Sichtweite einer Befristung stehen.
    for body in (plain, rich):
        for pos in _all_positions(body, "jederzeit"):
            window = body[max(0, pos - 220):pos + 220]
            assert "Stunden" in window or "Minuten" in window, (
                "Ein unbefristetes „jederzeit\" verspricht mehr, als die Demo haelt"
            )


def test_welcome_subject_follows_a_changed_ttl() -> None:
    """Eine geaenderte Laufzeit schlaegt bis in den Betreff durch."""
    assert "90 Minuten" in mailer._welcome_subject("Max", "90 Minuten")
    assert "unbegrenzt" in mailer._welcome_subject("", "unbegrenzt")
    # Reines ASCII -- sonst steht mitten im Betreff ein RFC-2047-Wortstueck.
    assert mailer._welcome_subject("Max", "2 Stunden").isascii()


def _all_positions(haystack: str, needle: str) -> list[int]:
    """Alle Fundstellen von ``needle`` (der Waechter prueft jede einzeln)."""
    out, start = [], 0
    while (pos := haystack.find(needle, start)) != -1:
        out.append(pos)
        start = pos + 1
    return out
