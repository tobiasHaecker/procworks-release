# SPDX-License-Identifier: BUSL-1.1
"""Suite-wide fixtures.

No test may resolve host names over the network. Since the webhook/push
delivery re-checks and pins its target on every delivery
(``procworks.outbox.resolve_target``), a test that delivers to a fictitious
host would otherwise ask the system resolver -- slow, dependent on the machine
it runs on, and on macOS it even triggers the "find devices on your local
network" permission prompt (multicast DNS for unknown names).
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

import procworks.outbox as outbox_module

#: Address every non-literal test host "resolves" to: public, so the policy
#: treats it like a real external endpoint. Tests that need another answer
#: patch ``procworks.outbox._lookup`` themselves.
TEST_PUBLIC_IP = "93.184.216.34"


def offline_lookup(host: str) -> list[str]:
    """Resolve without the network: literals as themselves, names to a public IP.

    ``inet_aton`` covers the classic IPv4 spellings a resolver would accept as
    numbers (``2130706433``, ``0x7f000001``, ``0177.0.0.1``) -- purely local, no
    lookup -- so the SSRF tests for those notations keep their meaning.
    ``localhost`` stays loopback, as it would on any system.
    """

    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    if host == "localhost":
        return ["127.0.0.1"]
    try:
        return [socket.inet_ntoa(socket.inet_aton(host))]
    except OSError:
        return [TEST_PUBLIC_IP]


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox_module, "_lookup", offline_lookup)


def _no_fqdn_lookup(*_args: object, **_kwargs: object) -> str:
    """Stand-in for ``socket.getfqdn``: any call fails the test loudly."""

    raise AssertionError(
        "socket.getfqdn() was called: it asks the system resolver for this "
        "host's name and blocks (30 s on a slow resolver) beyond every socket "
        "timeout -- give smtplib an explicit local_hostname (a test "
        "http.server.HTTPServer: override server_bind) instead."
    )


@pytest.fixture(autouse=True)
def _no_local_name_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """No code under test may look up the name of the machine it runs on.

    ``smtplib`` does so for its greeting unless given a name; the lookup can
    not be cut off by a time limit, so tests that send mail depended on the
    resolver of the machine. Tests that check the greeting itself forbid the
    further fallbacks (``gethostbyname`` and co.) on their own.
    """

    monkeypatch.setattr(socket, "getfqdn", _no_fqdn_lookup)


@pytest.fixture(autouse=True)
def _fresh_login_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with an empty login throttle.

    The throttle is module state of the API and counts failures per login and
    per client address -- and every TestClient request comes from the same
    address. Without a reset, the failed logins of one test would lock out the
    next. Tests of the throttle itself install their own instance.
    """

    import procworks.api as api_module
    from procworks.auth_password import LoginThrottle

    monkeypatch.setattr(api_module, "_login_throttle", LoginThrottle())


@pytest.fixture(autouse=True)
def _no_background_escalation_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test runs the background escalation timer unless it asks for one.

    The API starts the timer in its lifespan (every ``with TestClient(app)``).
    A pass firing at a random moment inside another test would add events and
    mails that test does not expect. Tests of the timer set
    ``PROCWORKS_ESCALATION_SWEEP_SECONDS`` themselves; the sweep itself is
    exercised directly through ``api._escalation_sweep``.
    """

    monkeypatch.setenv("PROCWORKS_ESCALATION_SWEEP_SECONDS", "0")
