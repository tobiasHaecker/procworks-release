# SPDX-License-Identifier: BUSL-1.1
"""Which client address the login throttle sees, and whom it believes.

Measured first (tester's record of the bundled Compose stack under Docker
Desktop, and the Caddyfile): Caddy *replaces* ``X-Forwarded-For`` with the
address it saw itself -- a value the client sent is ignored -- and the API
takes that value because Caddy's container is a private peer. Under Docker
Desktop the port forwarding of the host hides the real client from Caddy
already: every request arrives as the network gateway (e.g. 172.21.0.1). No
setting of the API can recover an address Caddy never learned.

What the API decides is whom it believes. Until now that was "any private or
loopback peer" -- fine behind the bundled proxy, but a host on the internal
network that could reach the API port directly could name any address and
slip past the per-address lock. ``PROCWORKS_TRUSTED_PROXIES`` now names the
proxies the API trusts (addresses, networks, ``private``, or ``none``); only
their header counts, and in a chain of proxies the right-most address that is
not one of them is the client.
"""

from __future__ import annotations

from typing import Any

import pytest

import procworks.api as api_module
from procworks.api import TrustedProxies


class _Req:
    """The two attributes ``_client_address`` reads from a request."""

    def __init__(self, peer: str | None, forwarded: str = "") -> None:
        self.client = type("C", (), {"host": peer})() if peer is not None else None
        self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}


def _address(monkeypatch: pytest.MonkeyPatch, config: str | None, req: _Req) -> Any:
    monkeypatch.setattr(api_module, "_TRUSTED_PROXIES", TrustedProxies.parse(config))
    return api_module._client_address(req)  # type: ignore[arg-type]


# --- the default: the bundled proxy keeps working ------------------------------


def test_by_default_the_bundled_proxy_is_believed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caddy's container is a private peer; its header names the client."""

    assert _address(monkeypatch, None, _Req("172.18.0.5", "8.8.4.4")) == "8.8.4.4"
    assert _address(monkeypatch, "", _Req("172.18.0.5", "8.8.4.4")) == "8.8.4.4"


def test_a_public_peer_is_the_client_whatever_it_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _address(monkeypatch, None, _Req("8.8.8.8", "10.0.0.1")) == "8.8.8.8"


def test_without_header_the_peer_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _address(monkeypatch, None, _Req("172.18.0.5")) == "172.18.0.5"
    assert _address(monkeypatch, None, _Req(None)) is None


# --- only the configured proxies are believed ----------------------------------


def test_a_peer_outside_the_configured_proxies_cannot_name_another_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host in the internal network that reaches the API directly sends a
    made-up header -- with the proxy configured, it counts as itself."""

    config = "172.18.0.5"
    assert _address(monkeypatch, config, _Req("192.168.1.50", "8.8.4.4")) == "192.168.1.50"
    assert _address(monkeypatch, config, _Req("172.18.0.5", "8.8.4.4")) == "8.8.4.4"


def test_networks_can_be_named(monkeypatch: pytest.MonkeyPatch) -> None:
    config = "172.16.0.0/12, fd00::/8"
    assert _address(monkeypatch, config, _Req("172.21.0.9", "8.8.4.4")) == "8.8.4.4"
    assert _address(monkeypatch, config, _Req("fd00::7", "2001:4860::8888")) == "2001:4860::8888"
    assert _address(monkeypatch, config, _Req("10.0.0.3", "8.8.4.4")) == "10.0.0.3"


def test_none_believes_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _address(monkeypatch, "none", _Req("172.18.0.5", "8.8.4.4")) == "172.18.0.5"


def test_in_a_chain_the_rightmost_untrusted_address_is_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load balancer -> Caddy -> API: the client cannot push its own entry past
    the proxies, because only what the proxies appended is read from the right."""

    req = _Req("172.18.0.5", "6.6.6.6, 8.8.4.4, 10.1.2.3")
    assert _address(monkeypatch, "private", req) == "8.8.4.4"


def test_a_client_inside_the_trusted_network_is_named_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every entry is a trusted network (an office client behind the proxy):
    the left-most one is the client."""

    req = _Req("172.18.0.5", "192.168.1.20")
    assert _address(monkeypatch, "private", req) == "192.168.1.20"


def test_a_garbled_entry_is_not_taken_as_an_address(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _address(monkeypatch, None, _Req("172.18.0.5", "unknown")) == "172.18.0.5"


# --- the setting itself ---------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["proxy.local", "300.1.1.1", "10.0.0.0/33", "none, 10.0.0.1", "private,,"]
)
def test_a_wrong_setting_stops_the_start_with_a_clear_message(bad: str) -> None:
    with pytest.raises(ValueError, match="PROCWORKS_TRUSTED_PROXIES"):
        TrustedProxies.parse(bad)


def test_the_setting_is_read_once_at_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCWORKS_TRUSTED_PROXIES", "10.9.0.0/16")
    proxies = TrustedProxies.from_env()
    assert proxies.trusts("10.9.1.1") and not proxies.trusts("10.8.1.1")
    assert not proxies.trusts("not-an-address")
