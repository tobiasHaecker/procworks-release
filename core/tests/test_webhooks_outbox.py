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

import ipaddress
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
import procworks.outbox as outbox_module
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


#: A public address the test host "resolves" to. Delivery resolves and pins the
#: target, so the fictitious allow-listed host needs an answer (no real DNS).
_PUBLIC_IP = "93.184.216.34"


def _fake_dns(monkeypatch: pytest.MonkeyPatch, answer: str = _PUBLIC_IP) -> None:
    """Resolve every name to ``answer`` (literal addresses stay themselves)."""

    def lookup(host: str) -> list[str]:
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            return [answer]

    monkeypatch.setattr(outbox_module, "_lookup", lookup)


@pytest.fixture
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ALLOWLIST, "hooks.example.com")
    _fake_dns(monkeypatch)


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
    _fake_dns(monkeypatch)
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


# --- SSRF hardening: checked at delivery, pinned, no redirects -------------


class _PinnedFake:
    """Transport that records the pinned target it was asked to call."""

    def __init__(self) -> None:
        self.targets: list[object] = []

    def post_pinned(
        self, target: object, body: bytes, headers: dict[str, str], timeout: float
    ) -> int:
        self.targets.append(target)
        return 200

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        raise AssertionError("the dispatcher must use the pinned path")


def test_delivery_rechecks_the_target_and_blocks_dns_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public when subscribed, internal when delivered: nothing may be sent.

    Before the hardening the policy ran only at ``subscribe``; the transport then
    resolved the name again on its own -- a rebinding name passed the check and
    the POST went to the internal address.
    """

    answers = iter(["93.184.216.34", "127.0.0.1"])
    monkeypatch.setattr(outbox_module, "_lookup", lambda host: [next(answers)])
    transport = _PinnedFake()
    disp = _dispatcher(transport, _Clock())  # type: ignore[arg-type]
    disp.subscribe("https://rebind.example.net/hook", ["task.completed"], "WH_SECRET")
    disp.emit("task.completed", {"task_id": "et1"})
    disp.dispatch_pending()

    assert transport.targets == []
    delivery = disp.deliveries(disp.list_subscriptions()[0].id)[-1]
    assert delivery.ok is False
    assert "internal address" in (delivery.error or "")


def test_delivery_connects_to_the_approved_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    transport = _PinnedFake()
    disp = _dispatcher(transport, _Clock())  # type: ignore[arg-type]
    disp.subscribe("https://hooks.example.net:8443/in?x=1", ["task.completed"], "WH_SECRET")
    disp.emit("task.completed", {"task_id": "et1"})
    disp.dispatch_pending()

    [target] = transport.targets
    assert (target.ip, target.host, target.port, target.path) == (  # type: ignore[attr-defined]
        _PUBLIC_IP, "hooks.example.net", 8443, "/in?x=1"
    )


def test_egress_lockdown_also_stops_already_queued_deliveries(
    allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _PinnedFake()
    disp = _dispatcher(transport, _Clock())  # type: ignore[arg-type]
    disp.subscribe(_URL, ["task.completed"], "WH_SECRET")
    disp.emit("task.completed", {"task_id": "et1"})
    monkeypatch.setenv("PROCWORKS_EGRESS_DENY", "1")
    disp.dispatch_pending()

    assert transport.targets == []
    delivery = disp.deliveries(disp.list_subscriptions()[0].id)[-1]
    assert "disabled" in (delivery.error or "")


def test_push_targets_keep_their_relaxed_policy_but_are_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin-configured push endpoints may live on the private network."""

    _fake_dns(monkeypatch, answer="10.0.0.7")
    transport = _PinnedFake()
    disp = _dispatcher(transport, _Clock())  # type: ignore[arg-type]
    disp.push("http://erp.intern/hook", "WH_SECRET", "task.ready", {"x": 1})
    disp.dispatch_pending()

    [target] = transport.targets
    assert target.ip == "10.0.0.7"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::ffff:169.254.169.254]/",  # the same, IPv4-mapped
        "http://[::ffff:127.0.0.1]/",
        "http://100.100.100.200/",  # carrier-grade NAT (some clouds' metadata)
        "http://2130706433/",  # 127.0.0.1 written as one number
        "http://0x7f000001/",  # ... and in hex
        "http://[::1]/",
        "http://0.0.0.0/",
    ],
)
def test_special_addresses_are_refused_whatever_the_notation(url: str) -> None:
    with pytest.raises(WebhookError, match="internal address|does not resolve"):
        assert_url_allowed(url)


def test_credentials_in_the_url_are_refused(allowlist: None) -> None:
    with pytest.raises(WebhookError, match="credentials"):
        assert_url_allowed("https://user:pw@hooks.example.com/x")


def test_transport_does_not_follow_redirects_and_sends_the_real_host() -> None:
    """A public endpoint answering 302 -> internal URL must lead nowhere.

    The former ``urlopen`` transport followed the redirect (POST became GET) to
    wherever the ``Location`` pointed. The pinned transport returns the 3xx as
    the delivery status. It also connects to the pinned IP while announcing the
    real name in ``Host``.
    """

    import http.server
    import threading

    from procworks.outbox import PinnedTarget, UrllibTransport

    seen: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            seen.append((self.path, self.headers.get("Host")))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(302)
            self.send_header("Location", "/intern")
            self.end_headers()

        do_GET = do_POST  # noqa: N815

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_port
        target = PinnedTarget("http", "hooks.example.test", port, "/start", "127.0.0.1")
        status = UrllibTransport().post_pinned(target, b"{}", {"X-T": "1"}, 5)
    finally:
        server.shutdown()

    assert status == 302
    assert seen == [("/start", f"hooks.example.test:{port}")]


# --- delivery preview (nothing is sent) -----------------------------------


def test_preview_shows_the_exact_signed_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from procworks.outbox import preview_delivery

    monkeypatch.setenv("WH_SECRET", "topsecret")
    p = preview_delivery("https://hooks.example.net/in", "task.completed", "WH_SECRET", now=1.0)
    assert (p.allowed, p.would_send, p.egress_locked, p.signed) == (True, True, False, True)
    assert p.resolved_address == "93.184.216.34"
    # A receiver can verify the shown signature against the shown body.
    assert p.headers["X-ProcWorks-Signature"] == sign_body("topsecret", p.body.encode("utf-8"))
    assert '"event": "task.completed"' in p.body


def test_preview_refuses_internal_targets_with_the_delivery_message() -> None:
    from procworks.outbox import preview_delivery

    p = preview_delivery("http://169.254.169.254/latest/meta-data/", "task.completed", "")
    assert p.allowed is False and p.would_send is False
    assert "internal address" in p.reason
    assert p.signed is False and "X-ProcWorks-Signature" not in p.headers


def test_preview_shows_the_verdict_even_under_egress_lockdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public demo locks all egress; the SSRF rule must stay demonstrable."""

    from procworks.outbox import preview_delivery

    monkeypatch.setenv("PROCWORKS_EGRESS_DENY", "1")
    public = preview_delivery("https://hooks.example.net/in", "task.ready", "")
    internal = preview_delivery("http://127.0.0.1:8080/admin", "task.ready", "")
    assert (public.allowed, public.egress_locked, public.would_send) == (True, True, False)
    assert internal.allowed is False and "internal address" in internal.reason


def test_api_preview_sends_and_stores_nothing(webhook_api: None) -> None:
    fake = api_module._outbox._transport
    resp = client.post(
        "/v1/webhooks/preview",
        json={"url": "http://10.1.2.3/hook", "event": "instance.completed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # The fixture configures an allow-list, so that rule refuses first.
    assert body["allowed"] is False and "allow-list" in body["reason"]
    assert api_module._outbox._store.list_entries() == []
    assert getattr(fake, "calls", []) == []

    bad = client.post("/v1/webhooks/preview", json={"url": _URL, "event": "bogus"})
    assert bad.status_code == 422


def test_transport_reads_only_a_bounded_part_of_the_response() -> None:
    """A receiver answering with an endless body must not tie up memory."""

    import socket
    import threading

    from procworks.outbox import _MAX_RESPONSE_BYTES, PinnedTarget, UrllibTransport

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    sent = {"bytes": 0}

    def serve() -> None:
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000000\r\n\r\n")
        chunk = b"x" * 65536
        try:
            while sent["bytes"] < 50 * 1024 * 1024:
                conn.sendall(chunk)
                sent["bytes"] += len(chunk)
        except OSError:
            pass  # client closed after its bounded read -- expected
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        port = listener.getsockname()[1]
        target = PinnedTarget("http", "big.example.test", port, "/", "127.0.0.1")
        assert UrllibTransport().post_pinned(target, b"{}", {}, 5) == 200
    finally:
        listener.close()
    assert sent["bytes"] < 50 * 1024 * 1024, "the whole body was consumed"
    assert _MAX_RESPONSE_BYTES <= 1024 * 1024
