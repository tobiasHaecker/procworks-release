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
