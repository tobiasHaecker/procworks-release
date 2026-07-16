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
import ipaddress
import json
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request
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


def _is_internal_host(host: str) -> bool:
    """Return whether ``host`` points at a private/loopback/reserved address.

    Used only when no explicit allow-list is configured; a resolution failure is
    treated as internal (deny) so an unknown target can never be called.
    """

    addresses: list[str] = []
    try:
        addresses.append(str(ipaddress.ip_address(host)))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return True
        addresses = [str(info[4][0]) for info in infos]
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def assert_url_allowed(url: str, *, allow_internal: bool = False) -> None:
    """Validate a webhook/push target against the SSRF policy (rule I6).

    Only ``http``/``https`` are allowed. With an allow-list configured the host
    must be listed; without one, obviously-internal targets are blocked.
    ``allow_internal`` relaxes the internal/allow-list checks for *trusted*,
    server-configured push targets (resolved from ``PROCWORKS_PUSH_ENDPOINTS``),
    which may legitimately live on a private network -- the scheme and host are
    still enforced. User-supplied webhook URLs always use the strict policy.

    When the hard egress lockdown (``PROCWORKS_EGRESS_DENY``) is active, *every*
    target is refused up front -- even ``allow_internal`` push targets -- so a
    locked-down instance (e.g. a public demo) makes no outbound HTTP at all.
    """

    if _egress_denied():
        raise WebhookError("outbound webhook/push delivery is disabled on this instance", 403)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebhookError(f"webhook url scheme '{parsed.scheme}' is not allowed", 422)
    host = parsed.hostname
    if not host:
        raise WebhookError("webhook url has no host", 422)
    if allow_internal:
        return
    allow = _allowed_hosts()
    if host in allow:
        return
    if allow:
        raise WebhookError(f"webhook host '{host}' is not in the allow-list", 422)
    if _is_internal_host(host):
        raise WebhookError(f"webhook host '{host}' resolves to an internal address", 422)


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


class UrllibTransport:
    """Default stdlib transport (no extra runtime dependency)."""

    def post(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> int:
        req = urllib_request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return int(resp.status)
        except urllib_error.HTTPError as err:
            return int(err.code)


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
        body = json.dumps(
            {
                "delivery_id": entry.delivery_id,
                "event": entry.event_type,
                "data": entry.payload,
                "timestamp": now,
            },
            sort_keys=True,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-ProcWorks-Event": entry.event_type,
            "X-ProcWorks-Delivery": entry.delivery_id,
        }
        secret = _resolve_secret(sub.secret_ref) if sub is not None else _resolve_secret(
            entry.secret_ref
        )
        if secret:
            headers["X-ProcWorks-Signature"] = sign_body(secret, body)

        attempt = entry.attempts + 1
        status: int | None = None
        error: str | None = None
        try:
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
