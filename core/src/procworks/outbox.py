# SPDX-License-Identifier: BUSL-1.1
"""Webhook subscriptions and the transactional outbox dispatcher (roadmap E13).

The *event* side of the maximally open API (concept §6.3): outside tools
subscribe to domain events (``instance.started``, ``task.completed``, ...) and
ProcWorks delivers them as signed HTTP POSTs. Delivery is **robust by design**
(concept §6.3 "Robuste Zustellung"):

* **Transactional outbox** -- an emitted event is first written to the outbox in
  the same step as the triggering state, so nothing is lost on a crash; a
  dispatcher delivers it afterwards.
* **At-least-once + idempotency** -- each delivery carries a unique
  ``delivery_id`` so the receiver can de-duplicate.
* **HMAC signature** -- the raw body is signed with the subscription's secret
  (resolved from the server-side secret store, never stored inline).
* **Back-off retry + dead-letter** -- transient failures are retried with an
  exponential back-off; once the attempt budget is spent the entry becomes a
  ``DEAD`` dead-letter.
* **Circuit breaker** -- a target that keeps failing is skipped for a cool-down
  window so one bad endpoint cannot stall the queue.
* **SSRF allow-list (I6)** -- a subscription URL is checked against an allow-list
  / blocked from internal targets before it is ever stored or called.

The dispatcher is a boundary component: it never touches the pure engine, it
only observes domain events the API hands it.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import socket
import ssl
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from procworks.model import (
    OutboxEntry,
    OutboxState,
    WebhookDelivery,
    WebhookSubscription,
)
from procworks.store import WebhookStore

#: The domain events a tool may subscribe to (concept §6.3).
WEBHOOK_EVENTS = frozenset(
    {
        "instance.started",
        "instance.completed",
        "task.ready",
        "task.completed",
        "task.incident",
    }
)

#: Comma-separated host allow-list for webhook targets (SSRF guard, rule I6).
_ALLOWLIST_ENV = "PROCWORKS_WEBHOOK_ALLOWLIST"

#: Hard egress lockdown for the outbox HTTP layer (SSRF/egress hardening). When
#: truthy, EVERY webhook/push target is refused -- the instance makes no outbound
#: HTTP from the outbox at all, not even to allow-listed or server-configured
#: (``allow_internal``) targets. Default off. Set it on throw-away public demos so
#: a visitor cannot register a webhook that turns the instance into an egress
#: beacon or data-exfil channel; regular deployments leave it unset and keep the
#: normal allow-list / internal-only policy. Orthogonal to ``PROCWORKS_DEMO_MODE``
#: (a login convenience) -- a general hardening posture on its own env switch.
_EGRESS_DENY_ENV = "PROCWORKS_EGRESS_DENY"


def _egress_denied() -> bool:
    """Whether the hard outbound lockdown (``PROCWORKS_EGRESS_DENY``) is active."""

    return os.environ.get(_EGRESS_DENY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}

_BACKOFF_BASE_MS = 2000
_BACKOFF_CAP_MS = 300_000


class WebhookError(Exception):
    """A boundary error in the webhook layer, carrying an HTTP status."""

    def __init__(self, message: str, status: int = 422) -> None:
        self.message = message
        self.status = status
        super().__init__(message)


def _allowed_hosts() -> set[str]:
    raw = os.environ.get(_ALLOWLIST_ENV, "").strip()
    return {h.strip() for h in raw.split(",") if h.strip()}


def _lookup(host: str) -> list[str]:
    """Resolve ``host`` to IP address strings (monkeypatchable seam for tests).

    Literal addresses are returned as-is; names go through ``getaddrinfo``.
    Raises ``OSError`` when the name does not resolve.
    """

    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def _is_public(address: str) -> bool:
    """Whether ``address`` is a globally routable address (SSRF rule I6).

    An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is judged by the IPv4
    address it carries -- whether ``ipaddress`` does that on its own depends on
    the Python version, so it is unwrapped explicitly. ``is_global`` is False
    for every special-purpose range at once: private, loopback, link-local
    (cloud metadata ``169.254.169.254``), carrier-grade NAT ``100.64/10``,
    reserved, multicast and unspecified.
    """

    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


def _is_internal_host(host: str) -> bool:
    """Return whether ``host`` points at a non-public address (or does not resolve).

    A resolution failure counts as internal (deny), so an unknown target can
    never be called. Every resolved address must be public -- one internal
    record is enough to refuse.
    """

    try:
        addresses = _lookup(host)
    except OSError:
        return True
    return not addresses or not all(_is_public(a) for a in addresses)


@dataclass(frozen=True)
class PinnedTarget:
    """A delivery target whose address was checked *and* is used for the call.

    ``ip`` is the address the policy approved; the connection goes to exactly
    that address, while ``host`` stays the name for the ``Host`` header and for
    TLS (SNI + certificate check). Checking one resolution and connecting after
    a second one is the DNS-rebinding gap this closes.
    """

    scheme: str
    host: str
    port: int
    path: str
    ip: str


def resolve_target(
    url: str,
    *,
    allow_internal: bool = False,
    pin: bool = True,
    honour_lockdown: bool = True,
) -> PinnedTarget:
    """Validate a webhook/push target against the SSRF policy and pin its address.

    Rule I6. Order: hard egress lockdown (``PROCWORKS_EGRESS_DENY`` -> 403),
    scheme ``http``/``https``, a host. Then the name is resolved **once**:

    * ``allow_internal`` (trusted, server-configured push targets) or a host on
      ``PROCWORKS_WEBHOOK_ALLOWLIST``: any address is accepted -- the operator
      vouched for it -- but it is still pinned.
    * With an allow-list configured and the host not on it: refused.
    * Otherwise every resolved address must be public (:func:`_is_public`).

    Called when a subscription is created **and again at every delivery**
    (:meth:`OutboxDispatcher._deliver`): a name that resolved to a public
    address yesterday may resolve to an internal one today.

    ``pin=False`` (configuration time only) skips the lookup for a *trusted*
    host -- the verdict does not depend on it, and a transient DNS outage must
    not block configuring an allow-listed endpoint. The returned ``ip`` is then
    empty and must not be used to connect. Untrusted hosts are always resolved,
    because their verdict is the resolution.

    ``honour_lockdown=False`` is only for :func:`preview_delivery`, which sends
    nothing and reports the lockdown separately -- a real delivery always
    honours it.

    Raises :class:`WebhookError` (403 lockdown, 422 policy).
    """

    if honour_lockdown and _egress_denied():
        raise WebhookError("outbound webhook/push delivery is disabled on this instance", 403)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebhookError(f"webhook url scheme '{parsed.scheme}' is not allowed", 422)
    host = parsed.hostname
    if not host:
        raise WebhookError("webhook url has no host", 422)
    if parsed.username or parsed.password:
        raise WebhookError("webhook url must not carry credentials", 422)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise WebhookError(f"webhook url has an invalid port: {exc}", 422) from exc
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    allow = _allowed_hosts()
    trusted = allow_internal or host in allow
    if not trusted and allow:
        raise WebhookError(f"webhook host '{host}' is not in the allow-list", 422)
    if trusted and not pin:
        return PinnedTarget(scheme=parsed.scheme, host=host, port=port, path=path, ip="")
    try:
        addresses = _lookup(host)
    except OSError as exc:
        raise WebhookError(f"webhook host '{host}' does not resolve", 422) from exc
    if not addresses:
        raise WebhookError(f"webhook host '{host}' does not resolve", 422)
    if not trusted and not all(_is_public(a) for a in addresses):
        raise WebhookError(f"webhook host '{host}' resolves to an internal address", 422)
    return PinnedTarget(scheme=parsed.scheme, host=host, port=port, path=path, ip=addresses[0])


def assert_url_allowed(url: str, *, allow_internal: bool = False) -> None:
    """Validate a webhook/push target against the SSRF policy (rule I6).

    Thin wrapper around :func:`resolve_target` for callers that only need the
    verdict (subscription creation, push-endpoint registration).
    """

    resolve_target(url, allow_internal=allow_internal, pin=False)


def build_request(
    delivery_id: str,
    event_type: str,
    payload: dict[str, object],
    timestamp: float,
    secret: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Body and headers of one delivery -- the ONE place that shapes them.

    Shared by the real delivery and :func:`preview_delivery`, so a preview
    shows byte-for-byte what a receiver would get (and can verify the
    signature against). The body is canonical JSON (sorted keys); the
    ``X-ProcWorks-Signature`` header is present only when a secret resolves.
    """

    body = json.dumps(
        {
            "delivery_id": delivery_id,
            "event": event_type,
            "data": payload,
            "timestamp": timestamp,
        },
        sort_keys=True,
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-ProcWorks-Event": event_type,
        "X-ProcWorks-Delivery": delivery_id,
    }
    if secret:
        headers["X-ProcWorks-Signature"] = sign_body(secret, body)
    return body, headers


@dataclass(frozen=True)
class DeliveryPreview:
    """What a delivery to ``url`` would look like -- nothing is sent.

    ``allowed``/``reason`` is the SSRF verdict (rule I6) exactly as a real
    delivery would reach it, ``resolved_address`` the address it would pin.
    ``egress_locked`` reports ``PROCWORKS_EGRESS_DENY`` separately: in a locked
    instance (public demo) the verdict is still shown, but ``would_send`` is
    False. ``signed`` tells whether the secret reference resolved.
    """

    allowed: bool
    reason: str
    resolved_address: str
    egress_locked: bool
    would_send: bool
    signed: bool
    headers: dict[str, str]
    body: str


#: Example payloads per event for the preview (same shape as the real events).
_PREVIEW_PAYLOADS: dict[str, dict[str, object]] = {
    "instance.started": {"instance_id": "instance_42", "schema_id": "schema_7",
                         "schema_version": 1, "state": "RUNNING"},
    "instance.completed": {"instance_id": "instance_42", "schema_id": "schema_7",
                           "schema_version": 1, "state": "COMPLETED"},
    "task.ready": {"instance_id": "instance_42", "node_id": "act_pruefen",
                   "label": "Antrag prüfen"},
    "task.completed": {"instance_id": "instance_42", "node_id": "act_pruefen",
                       "label": "Antrag prüfen"},
    "task.incident": {"instance_id": "instance_42", "node_id": "act_export",
                      "message": "ERP nicht erreichbar"},
}


def preview_delivery(
    url: str, event_type: str, secret_ref: str, *, now: float | None = None
) -> DeliveryPreview:
    """Dry run of a webhook delivery: verdict, headers, signed body. Sends nothing.

    A diagnostic for integrators (does my receiver verify the signature?) and
    the way to *show* the SSRF rule where outbound traffic is locked: an
    internal target is refused with the same message a real delivery would
    produce. Only the policy check runs (it may resolve the host name); no
    connection is opened.
    """

    try:
        target = resolve_target(url, honour_lockdown=False)
        allowed, reason, address = True, "", target.ip
    except WebhookError as exc:
        allowed, reason, address = False, exc.message, ""
    locked = _egress_denied()
    secret = _resolve_secret(secret_ref)
    payload = _PREVIEW_PAYLOADS.get(event_type, {"message": "ProcWorks webhook test"})
    body, headers = build_request(
        "preview", event_type, payload, time.time() if now is None else now, secret
    )
    return DeliveryPreview(
        allowed=allowed,
        reason=reason,
        resolved_address=address,
        egress_locked=locked,
        would_send=allowed and not locked,
        signed=bool(secret),
        headers=headers,
        body=body.decode("utf-8"),
    )


def sign_body(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` HMAC signature of ``body`` with ``secret``."""

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _resolve_secret(secret_ref: str) -> str | None:
    """Resolve a subscription's secret reference from the environment."""

    if not secret_ref:
        return None
    return os.environ.get(secret_ref)


def _backoff_ms(attempts: int) -> float:
    return float(min(_BACKOFF_BASE_MS * (2 ** max(0, attempts - 1)), _BACKOFF_CAP_MS))


#: Environment variable naming the push-endpoint source (a JSON file path or
#: inline JSON object mapping ``endpoint_ref`` -> ``{"url": ..., "secret_ref"?}``).
_PUSH_ENDPOINTS_ENV = "PROCWORKS_PUSH_ENDPOINTS"


class PushEndpointError(Exception):
    """A boundary error resolving an ``HTTP_PUSH`` endpoint reference."""

    def __init__(self, message: str, status: int = 404) -> None:
        self.message = message
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class PushTarget:
    """A resolved push destination for an ``HTTP_PUSH`` activity binding.

    ``url`` is the concrete tool endpoint; ``secret_ref`` (optional) names the
    HMAC signing secret in the environment. Both live server-side only -- the
    schema's ``ServiceBinding.endpoint_ref`` is just the logical reference, so
    no URL or secret is ever stored in the model (rules I4/I6).
    """

    url: str
    secret_ref: str = ""


class PushEndpointRegistry:
    """Maps a logical ``endpoint_ref`` to a concrete, trusted push target.

    The registry is populated server-side (from ``PROCWORKS_PUSH_ENDPOINTS`` or
    programmatically). Keeping the mapping out of the schema lets the same model
    run against different tool endpoints per environment without edits.
    """

    def __init__(self) -> None:
        self._targets: dict[str, PushTarget] = {}

    def register(self, endpoint_ref: str, url: str, secret_ref: str = "") -> None:
        """Add or replace the target for ``endpoint_ref``."""

        self._targets[endpoint_ref] = PushTarget(url=url, secret_ref=secret_ref)

    def has(self, endpoint_ref: str) -> bool:
        return endpoint_ref in self._targets

    def refs(self) -> list[str]:
        """Return the configured endpoint references (never any URL/secret)."""

        return sorted(self._targets)

    def resolve(self, endpoint_ref: str) -> PushTarget:
        """Return the target for ``endpoint_ref`` or raise a 404 boundary error."""

        target = self._targets.get(endpoint_ref)
        if target is None:
            raise PushEndpointError(
                f"push endpoint '{endpoint_ref}' is not configured", 404
            )
        return target


def _load_source(raw: str) -> str:
    """Return the push-endpoint JSON, from a file path or inline JSON."""

    if os.path.isfile(raw):
        with open(raw, encoding="utf-8") as handle:
            return handle.read()
    return raw


def build_push_endpoint_registry() -> PushEndpointRegistry:
    """Build the registry from ``PROCWORKS_PUSH_ENDPOINTS`` (file path or JSON).

    Returns an empty registry when the variable is unset, so ``HTTP_PUSH`` is
    simply inactive by default. The source is a JSON object mapping each
    ``endpoint_ref`` to ``{"url": ..., "secret_ref"?: ...}``.
    """

    registry = PushEndpointRegistry()
    raw = os.environ.get(_PUSH_ENDPOINTS_ENV, "").strip()
    if not raw:
        return registry
    data = json.loads(_load_source(raw))
    if not isinstance(data, dict):
        raise PushEndpointError(
            f"{_PUSH_ENDPOINTS_ENV} must be a JSON object of endpoint references", 422
        )
    for endpoint_ref, spec in data.items():
        if not isinstance(spec, dict) or "url" not in spec:
            raise PushEndpointError(
                f"push endpoint '{endpoint_ref}' must be an object with a 'url'", 422
            )
        registry.register(
            str(endpoint_ref), str(spec["url"]), str(spec.get("secret_ref", ""))
        )
    return registry


class Transport(Protocol):
    """Minimal HTTP POST transport so the dispatcher is testable without I/O."""

    def post(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> int:
        """Deliver ``body`` to ``url`` and return the HTTP status code."""
        ...


#: Upper bound for reading a receiver's response body (the status is all we use).
_MAX_RESPONSE_BYTES = 64 * 1024


class UrllibTransport:
    """Default stdlib transport (no extra runtime dependency), SSRF-hardened.

    Two properties the former ``urllib.urlopen`` implementation lacked:

    * **Pinned address.** It connects to the address the policy approved
      (:class:`PinnedTarget`), never to a fresh resolution of the name.
    * **No redirects.** ``http.client`` does not follow them; a 3xx is returned
      as the delivery status (and counts as a failed delivery). A redirect from
      a public endpoint to an internal one therefore goes nowhere.

    TLS verifies the certificate against the *host name* (SNI), not the IP, so
    pinning does not weaken HTTPS. The name is kept for compatibility; the
    class is exported as the default transport.
    """

    def post_pinned(
        self, target: PinnedTarget, body: bytes, headers: dict[str, str], timeout: float
    ) -> int:
        """POST ``body`` to the pinned address of ``target``; return the status."""

        raw = socket.create_connection((target.ip, target.port), timeout=timeout)
        conn: http.client.HTTPConnection
        if target.scheme == "https":
            context = ssl.create_default_context()
            sock: socket.socket = context.wrap_socket(raw, server_hostname=target.host)
            conn = http.client.HTTPSConnection(target.host, target.port, timeout=timeout)
        else:
            sock = raw
            conn = http.client.HTTPConnection(target.host, target.port, timeout=timeout)
        conn.sock = sock  # already connected to the pinned address
        try:
            default_port = 443 if target.scheme == "https" else 80
            name = f"[{target.host}]" if ":" in target.host else target.host  # IPv6 literal
            port_suffix = "" if target.port == default_port else f":{target.port}"
            host_header = name + port_suffix
            conn.putrequest("POST", target.path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host_header)
            for name, value in headers.items():
                conn.putheader(name, value)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            # Only the status matters. Read at most a small, bounded amount so a
            # hostile receiver cannot tie up memory with an endless body; the
            # connection is closed right after (no keep-alive reuse).
            resp.read(_MAX_RESPONSE_BYTES)
            return int(resp.status)
        finally:
            conn.close()

    def post(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> int:
        """Compatibility entry point: resolve with the strict policy, then pin."""

        return self.post_pinned(resolve_target(url), body, headers, timeout)


class OutboxDispatcher:
    """Owns subscriptions and the durable delivery of domain events."""

    def __init__(
        self,
        store: WebhookStore,
        *,
        transport: Transport | None = None,
        now: Callable[[], float] | None = None,
        max_attempts: int = 5,
        timeout_s: float = 5.0,
        circuit_threshold: int = 5,
        circuit_cooldown_s: float = 60.0,
    ) -> None:
        self._store = store
        self._transport: Transport = transport or UrllibTransport()
        self._now = now or time.time
        self._max_attempts = max_attempts
        self._timeout_s = timeout_s
        self._circuit_threshold = circuit_threshold
        self._circuit_cooldown_s = circuit_cooldown_s
        #: host -> (consecutive failures, open-until timestamp)
        self._circuit: dict[str, tuple[int, float]] = {}

    # -- subscriptions -----------------------------------------------------

    def subscribe(
        self, url: str, events: list[str], secret_ref: str
    ) -> WebhookSubscription:
        """Register a webhook subscription after validating events and URL."""

        unknown = sorted(set(events) - WEBHOOK_EVENTS)
        if not events or unknown:
            raise WebhookError(f"unknown webhook events: {unknown or 'none given'}", 422)
        assert_url_allowed(url)
        subscription = WebhookSubscription(
            id=f"wh_{uuid.uuid4().hex}", url=url, events=list(events), secret_ref=secret_ref
        )
        return self._store.put_subscription(subscription)

    def list_subscriptions(self) -> list[WebhookSubscription]:
        return self._store.list_subscriptions()

    def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        return self._store.get_subscription(subscription_id)

    def unsubscribe(self, subscription_id: str) -> None:
        if self._store.get_subscription(subscription_id) is None:
            raise WebhookError(f"subscription '{subscription_id}' not found", 404)
        self._store.delete_subscription(subscription_id)

    def deliveries(self, subscription_id: str) -> list[WebhookDelivery]:
        return self._store.list_deliveries(subscription_id)

    # -- emit / dispatch ---------------------------------------------------

    def emit(self, event_type: str, payload: dict[str, object]) -> list[OutboxEntry]:
        """Enqueue ``event_type`` for every active matching subscription."""

        now = self._now()
        created: list[OutboxEntry] = []
        for sub in self._store.list_subscriptions():
            if not sub.active or event_type not in sub.events:
                continue
            entry = OutboxEntry(
                id=f"ob_{uuid.uuid4().hex}",
                subscription_id=sub.id,
                event_type=event_type,
                delivery_id=uuid.uuid4().hex,
                url=sub.url,
                payload=payload,
                max_attempts=self._max_attempts,
                next_attempt_at=now,
                created_at=now,
            )
            self._store.put_entry(entry)
            created.append(entry)
        return created

    def dispatch_pending(self) -> int:
        """Attempt delivery of all due outbox entries; return how many were sent."""

        now = self._now()
        sent = 0
        for entry in self._store.list_entries():
            if entry.state not in (OutboxState.PENDING, OutboxState.FAILED):
                continue
            if entry.next_attempt_at > now:
                continue
            if self._circuit_open(entry.url, now):
                continue
            self._deliver(entry, now)
            sent += 1
        return sent

    def test_delivery(self, subscription_id: str) -> WebhookDelivery:
        """Deliver a synthetic ping to one subscription and return the result."""

        sub = self._store.get_subscription(subscription_id)
        if sub is None:
            raise WebhookError(f"subscription '{subscription_id}' not found", 404)
        now = self._now()
        entry = OutboxEntry(
            id=f"ob_{uuid.uuid4().hex}",
            subscription_id=sub.id,
            event_type="webhook.test",
            delivery_id=uuid.uuid4().hex,
            url=sub.url,
            payload={"message": "ProcWorks webhook test"},
            max_attempts=1,
            next_attempt_at=now,
            created_at=now,
        )
        self._store.put_entry(entry)
        return self._deliver(entry, now)

    def push(
        self,
        url: str,
        secret_ref: str,
        event_type: str,
        payload: dict[str, object],
        *,
        max_attempts: int = 5,
    ) -> OutboxEntry:
        """Enqueue a subscription-less push to a trusted, server-configured URL.

        Used for the ``HTTP_PUSH`` activity pattern (concept §6.3): when an
        automatic step is activated the boundary pushes its input package to the
        bound tool endpoint. Delivery reuses the full outbox machinery (durable
        queue, HMAC signature, back-off retry, circuit breaker, delivery log).
        The target is admin-configured, so the relaxed SSRF check applies
        (scheme + host enforced, private network allowed).
        """

        assert_url_allowed(url, allow_internal=True)
        now = self._now()
        entry = OutboxEntry(
            id=f"ob_{uuid.uuid4().hex}",
            subscription_id="",
            event_type=event_type,
            delivery_id=uuid.uuid4().hex,
            url=url,
            payload=payload,
            max_attempts=max_attempts,
            next_attempt_at=now,
            created_at=now,
            secret_ref=secret_ref,
        )
        return self._store.put_entry(entry)

    # -- internal ----------------------------------------------------------

    def _circuit_open(self, url: str, now: float) -> bool:
        host = urlparse(url).hostname or url
        state = self._circuit.get(host)
        return state is not None and state[1] > now

    def _record_circuit(self, url: str, *, ok: bool, now: float) -> None:
        host = urlparse(url).hostname or url
        if ok:
            self._circuit.pop(host, None)
            return
        failures = self._circuit.get(host, (0, 0.0))[0] + 1
        open_until = (
            now + self._circuit_cooldown_s
            if failures >= self._circuit_threshold
            else 0.0
        )
        self._circuit[host] = (failures, open_until)

    def _deliver(self, entry: OutboxEntry, now: float) -> WebhookDelivery:
        sub = self._store.get_subscription(entry.subscription_id)
        secret = _resolve_secret(sub.secret_ref) if sub is not None else _resolve_secret(
            entry.secret_ref
        )
        body, headers = build_request(
            entry.delivery_id, entry.event_type, entry.payload, now, secret
        )

        attempt = entry.attempts + 1
        status: int | None = None
        error: str | None = None
        try:
            # The policy is checked again at EVERY delivery, not only when the
            # subscription was created: DNS may have changed since (rebinding),
            # and an allow-list or lockdown may have been tightened. A push
            # entry (no subscription) targets an admin-configured endpoint and
            # keeps its relaxed policy. The approved address is then the one
            # the transport connects to.
            target = resolve_target(entry.url, allow_internal=not entry.subscription_id)
            post_pinned = getattr(self._transport, "post_pinned", None)
            if post_pinned is not None:
                status = int(post_pinned(target, body, headers, self._timeout_s))
            else:
                status = self._transport.post(entry.url, body, headers, self._timeout_s)
            ok = 200 <= status < 300
            if not ok:
                error = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 -- transport failures are reported, not raised
            ok = False
            error = str(exc)

        entry.attempts = attempt
        entry.last_status = status
        entry.last_error = error
        if ok:
            entry.state = OutboxState.DELIVERED
            entry.next_attempt_at = now
        elif attempt >= entry.max_attempts:
            entry.state = OutboxState.DEAD
            entry.next_attempt_at = now
        else:
            entry.state = OutboxState.FAILED
            entry.next_attempt_at = now + _backoff_ms(attempt) / 1000.0
        self._store.put_entry(entry)
        self._record_circuit(entry.url, ok=ok, now=now)

        delivery = WebhookDelivery(
            id=f"dl_{uuid.uuid4().hex}",
            outbox_id=entry.id,
            subscription_id=entry.subscription_id,
            event_type=entry.event_type,
            attempt=attempt,
            at=now,
            ok=ok,
            status_code=status,
            error=error,
        )
        return self._store.put_delivery(delivery)
