# SPDX-License-Identifier: BUSL-1.1
"""Webhook subscriptions and the transactional outbox dispatcher (P4, §6.3).

Covers four layers:

* the pure helpers -- HMAC signing and the SSRF allow-list guard (rule I6);
* the :class:`procworks.outbox.OutboxDispatcher` against an in-memory store with
  an injected fake transport and a controllable clock -- emit/dispatch, retry
  with back-off, dead-lettering, the per-host circuit breaker and the delivery
  log;
* the :class:`procworks.db.SqlAlchemyWebhookStore` round-trip on SQLite; and
* the ``/v1/webhooks`` API (CRUD, test delivery, SSRF/validation, role gate).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks import (
    InMemoryWebhookStore,
    OutboxDispatcher,
    OutboxState,
    WebhookError,
    assert_url_allowed,
    sign_body,
)
from procworks.api import app
from procworks.auth_token import TokenAuthBackend
from procworks.db import SqlAlchemyWebhookStore

_ALLOWLIST = "PROCWORKS_WEBHOOK_ALLOWLIST"
_URL = "https://hooks.example.com/procworks"


class _Clock:
    """A mutable, deterministic clock for back-off / circuit timing."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _FakeTransport:
    """Records POSTs and returns scripted status codes (no real network)."""

    def __init__(
        self, status: int = 200, fail_times: int = 0, raise_exc: Exception | None = None
    ) -> None:
        self.status = status
        self.fail_times = fail_times
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def post(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> int:
        self.calls.append((url, body, dict(headers)))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.fail_times > 0:
            self.fail_times -= 1
            return 500
        return self.status


@pytest.fixture
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ALLOWLIST, "hooks.example.com")


# --- pure helpers: HMAC + SSRF guard --------------------------------------


def test_sign_body_is_deterministic_sha256() -> None:
    sig = sign_body("s3cr3t", b"hello")
    assert sig.startswith("sha256=")
    assert sig == sign_body("s3cr3t", b"hello")
    assert sig != sign_body("other", b"hello")


def test_assert_url_allowed_accepts_listed_host(allowlist: None) -> None:
    assert_url_allowed(_URL)  # no raise


def test_assert_url_allowed_rejects_unlisted_host(allowlist: None) -> None:
    with pytest.raises(WebhookError) as err:
        assert_url_allowed("https://evil.example.org/x")
    assert err.value.status == 422


def test_assert_url_allowed_rejects_non_http_scheme(allowlist: None) -> None:
    with pytest.raises(WebhookError):
        assert_url_allowed("ftp://hooks.example.com/x")


def test_assert_url_allowed_blocks_internal_without_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_ALLOWLIST, raising=False)
    with pytest.raises(WebhookError):
        assert_url_allowed("http://localhost/hook")


# --- Hard egress lockdown (PROCWORKS_EGRESS_DENY) --------------------------

_EGRESS = "PROCWORKS_EGRESS_DENY"


def test_egress_deny_refuses_even_allowlisted_host(
    allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the lockdown on, an otherwise allow-listed host is still refused (403).

    This is the demo posture: a visitor cannot make the instance dial *any*
    external host, so no webhook can turn it into an egress beacon.
    """
    monkeypatch.setenv(_EGRESS, "1")
    with pytest.raises(WebhookError) as err:
        assert_url_allowed(_URL)  # normally allowed by the allow-list
    assert err.value.status == 403


def test_egress_deny_refuses_internal_push_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lockdown overrides ``allow_internal`` too -- no outbound at all."""
    monkeypatch.setenv(_EGRESS, "1")
    with pytest.raises(WebhookError) as err:
        assert_url_allowed("https://tool.internal/push", allow_internal=True)
    assert err.value.status == 403


@pytest.mark.parametrize("value", ["0", "false", "off", "", "  "])
def test_egress_deny_off_by_default_permits_allowlisted(
    allowlist: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Unset/false lockdown leaves the normal allow-list policy intact."""
    monkeypatch.setenv(_EGRESS, value)
    assert_url_allowed(_URL)  # no raise


# --- OutboxDispatcher: subscribe / validation -----------------------------


def _dispatcher(
    transport: _FakeTransport, clock: _Clock, **kw: object
) -> OutboxDispatcher:
    return OutboxDispatcher(
        InMemoryWebhookStore(), transport=transport, now=clock, **kw  # type: ignore[arg-type]
    )


def test_subscribe_rejects_unknown_events(allowlist: None) -> None:
    disp = _dispatcher(_FakeTransport(), _Clock())
    with pytest.raises(WebhookError) as err:
        disp.subscribe(_URL, ["bogus.event"], "WH_SECRET")
    assert err.value.status == 422


def test_subscribe_rejects_empty_events(allowlist: None) -> None:
    disp = _dispatcher(_FakeTransport(), _Clock())
    with pytest.raises(WebhookError):
        disp.subscribe(_URL, [], "WH_SECRET")


def test_subscribe_enforces_ssrf_policy() -> None:
    disp = _dispatcher(_FakeTransport(), _Clock())
    with pytest.raises(WebhookError):
        disp.subscribe("http://localhost/x", ["task.ready"], "WH_SECRET")


# --- OutboxDispatcher: emit / dispatch ------------------------------------


def test_emit_enqueues_one_entry_per_matching_active_subscription(
    allowlist: None,
) -> None:
    disp = _dispatcher(_FakeTransport(), _Clock())
    disp.subscribe(_URL, ["task.completed"], "WH_SECRET")
    disp.subscribe(_URL, ["task.ready"], "WH_SECRET")  # does not match
    entries = disp.emit("task.completed", {"task_id": "et1"})
    assert len(entries) == 1
    assert entries[0].state is OutboxState.PENDING


def test_dispatch_delivers_and_signs(
    allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WH_SECRET", "topsecret")
    transport = _FakeTransport(status=200)
    disp = _dispatcher(transport, _Clock())
    disp.subscribe(_URL, ["task.completed"], "WH_SECRET")
    disp.emit("task.completed", {"task_id": "et1"})
    assert disp.dispatch_pending() == 1

    url, body, headers = transport.calls[0]
    assert url == _URL
    assert headers["X-ProcWorks-Event"] == "task.completed"
    assert headers["X-ProcWorks-Signature"] == sign_body("topsecret", body)

    entries = disp._store.list_entries()
    assert entries[0].state is OutboxState.DELIVERED
    log = disp.deliveries(entries[0].subscription_id)
    assert len(log) == 1 and log[0].ok is True


def test_dispatch_without_secret_omits_signature(allowlist: None) -> None:
    transport = _FakeTransport(status=200)
    disp = _dispatcher(transport, _Clock())
    disp.subscribe(_URL, ["task.ready"], "")
    disp.emit("task.ready", {"task_id": "et1"})
    disp.dispatch_pending()
    _url, _body, headers = transport.calls[0]
    assert "X-ProcWorks-Signature" not in headers


def test_delivered_entry_is_not_redelivered(allowlist: None) -> None:
    transport = _FakeTransport(status=200)
    disp = _dispatcher(transport, _Clock())
    disp.subscribe(_URL, ["task.completed"], "")
    disp.emit("task.completed", {"task_id": "et1"})
    assert disp.dispatch_pending() == 1
    assert disp.dispatch_pending() == 0
    assert len(transport.calls) == 1


def test_retry_with_backoff_then_success(allowlist: None) -> None:
    clock = _Clock()
    transport = _FakeTransport(fail_times=1)  # first 500, then 200
    disp = _dispatcher(transport, clock)
    disp.subscribe(_URL, ["task.completed"], "")
    disp.emit("task.completed", {"task_id": "et1"})

    disp.dispatch_pending()  # attempt 1 -> FAILED
    entry = disp._store.list_entries()[0]
    assert entry.state is OutboxState.FAILED
    assert entry.next_attempt_at > clock.t  # backed off

    # not yet due -> skipped
    assert disp.dispatch_pending() == 0
    clock.t = entry.next_attempt_at + 1
    assert disp.dispatch_pending() == 1
    assert disp._store.list_entries()[0].state is OutboxState.DELIVERED


def test_retries_exhausted_become_dead_letter(allowlist: None) -> None:
    clock = _Clock()
    transport = _FakeTransport(status=500)
    disp = _dispatcher(transport, clock, max_attempts=2)
    disp.subscribe(_URL, ["task.incident"], "")
    disp.emit("task.incident", {"task_id": "et1"})

    disp.dispatch_pending()  # attempt 1 -> FAILED
    entry = disp._store.list_entries()[0]
    clock.t = entry.next_attempt_at + 1
    disp.dispatch_pending()  # attempt 2 -> DEAD
    entry = disp._store.list_entries()[0]
    assert entry.state is OutboxState.DEAD
    assert entry.attempts == 2
    assert all(d.ok is False for d in disp.deliveries(entry.subscription_id))


def test_transport_exception_is_recorded_not_raised(allowlist: None) -> None:
    transport = _FakeTransport(raise_exc=RuntimeError("connection refused"))
    disp = _dispatcher(transport, _Clock(), max_attempts=1)
    disp.subscribe(_URL, ["task.ready"], "")
    disp.emit("task.ready", {"task_id": "et1"})
    disp.dispatch_pending()
    entry = disp._store.list_entries()[0]
    assert entry.state is OutboxState.DEAD
    assert entry.last_error is not None and "refused" in entry.last_error


def test_circuit_breaker_skips_failing_host(allowlist: None) -> None:
    clock = _Clock()
    transport = _FakeTransport(status=500)
    disp = _dispatcher(
        transport, clock, max_attempts=10, circuit_threshold=2, circuit_cooldown_s=100
    )
    disp.subscribe(_URL, ["task.completed"], "")
    disp.emit("task.completed", {"task_id": "et1"})

    disp.dispatch_pending()  # failure 1
    entry = disp._store.list_entries()[0]
    clock.t = entry.next_attempt_at + 1
    disp.dispatch_pending()  # failure 2 -> circuit opens
    calls_after_open = len(transport.calls)

    entry = disp._store.list_entries()[0]
    clock.t = entry.next_attempt_at + 1  # entry due, but circuit open
    assert disp.dispatch_pending() == 0
    assert len(transport.calls) == calls_after_open  # no new POST

    clock.t += 200  # cooldown elapsed -> circuit closed again
    assert disp.dispatch_pending() == 1


def test_test_delivery_pings_one_subscription(
    allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WH_SECRET", "k")
    transport = _FakeTransport(status=200)
    disp = _dispatcher(transport, _Clock())
    sub = disp.subscribe(_URL, ["task.completed"], "WH_SECRET")
    delivery = disp.test_delivery(sub.id)
    assert delivery.ok is True
    assert transport.calls[0][2]["X-ProcWorks-Event"] == "webhook.test"


def test_test_delivery_unknown_subscription_raises(allowlist: None) -> None:
    disp = _dispatcher(_FakeTransport(), _Clock())
    with pytest.raises(WebhookError) as err:
        disp.test_delivery("nope")
    assert err.value.status == 404


def test_unsubscribe_removes_subscription(allowlist: None) -> None:
    disp = _dispatcher(_FakeTransport(), _Clock())
    sub = disp.subscribe(_URL, ["task.ready"], "")
    disp.unsubscribe(sub.id)
    assert disp.list_subscriptions() == []
    with pytest.raises(WebhookError):
        disp.unsubscribe(sub.id)


# --- SqlAlchemyWebhookStore round-trip ------------------------------------


def test_sqlalchemy_webhook_store_roundtrip(
    allowlist: None, tmp_path: Path
) -> None:
    url = f"sqlite:///{tmp_path / 'webhooks.db'}"
    store = SqlAlchemyWebhookStore(url, create_tables=True)
    clock = _Clock()
    transport = _FakeTransport(status=200)
    disp = OutboxDispatcher(store, transport=transport, now=clock)

    sub = disp.subscribe(_URL, ["instance.completed"], "")
    assert store.get_subscription(sub.id) is not None
    disp.emit("instance.completed", {"instance_id": "i1"})
    assert disp.dispatch_pending() == 1

    reopened = SqlAlchemyWebhookStore(url)
    entries = reopened.list_entries()
    assert len(entries) == 1 and entries[0].state is OutboxState.DELIVERED
    assert len(reopened.list_deliveries(sub.id)) == 1


# --- /v1/webhooks API -----------------------------------------------------


client = TestClient(app)


@pytest.fixture
def webhook_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(_ALLOWLIST, "hooks.example.com")
    monkeypatch.setenv("WH_SECRET", "api-secret")
    fake = _FakeTransport(status=200)
    monkeypatch.setattr(api_module._outbox, "_transport", fake)
    api_module._outbox._circuit.clear()
    try:
        yield
    finally:
        api_module._outbox._store.clear()
        api_module._outbox._circuit.clear()


def _create(events: list[str]) -> str:
    res = client.post(
        "/v1/webhooks",
        json={"url": _URL, "events": events, "secret_ref": "WH_SECRET"},
    )
    assert res.status_code == 201, res.text
    return str(res.json()["id"])


def test_api_create_and_list_webhook(webhook_api: None) -> None:
    sub_id = _create(["instance.completed"])
    body = client.get("/v1/webhooks").json()
    assert any(s["id"] == sub_id for s in body)


def test_api_create_rejects_unknown_event(webhook_api: None) -> None:
    res = client.post(
        "/v1/webhooks",
        json={"url": _URL, "events": ["nope"], "secret_ref": "WH_SECRET"},
    )
    assert res.status_code == 422


def test_api_create_rejects_ssrf_target(webhook_api: None) -> None:
    res = client.post(
        "/v1/webhooks",
        json={"url": "http://localhost/x", "events": ["task.ready"]},
    )
    assert res.status_code == 422


def test_api_test_delivery_and_log(webhook_api: None) -> None:
    sub_id = _create(["task.completed"])
    res = client.post(f"/v1/webhooks/{sub_id}/test")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    log = client.get(f"/v1/webhooks/{sub_id}/deliveries").json()
    assert len(log) == 1


def test_api_delete_webhook(webhook_api: None) -> None:
    sub_id = _create(["task.ready"])
    assert client.delete(f"/v1/webhooks/{sub_id}").status_code == 204
    assert client.get("/v1/webhooks").json() == []


def test_api_deliveries_unknown_is_404(webhook_api: None) -> None:
    assert client.get("/v1/webhooks/ghost/deliveries").status_code == 404


# --- role gate ------------------------------------------------------------

_TOKENS = {
    "viewer-token": {"subject": "leo", "roles": ["viewer"]},
    "modeler-token": {"subject": "mona", "roles": ["modeler"]},
}


def test_api_viewer_may_not_subscribe(
    monkeypatch: pytest.MonkeyPatch, webhook_api: None
) -> None:
    monkeypatch.setattr(api_module, "_auth_backend", TokenAuthBackend(_TOKENS))
    res = client.post(
        "/v1/webhooks",
        headers={"Authorization": "Bearer viewer-token"},
        json={"url": _URL, "events": ["task.ready"], "secret_ref": "WH_SECRET"},
    )
    assert res.status_code == 403
