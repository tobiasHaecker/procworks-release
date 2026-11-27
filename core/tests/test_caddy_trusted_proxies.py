# SPDX-License-Identifier: BUSL-1.1
"""The bundled web server (Caddy) can be told which proxy in front of it to believe.

Caddy replaces ``X-Forwarded-For`` with the address it saw itself, unless
that address belongs to a trusted proxy. Behind a proxy of the customer's
own (a load balancer, a reverse proxy on the host), every request then
arrived as that proxy -- the per-address login lock hit all users at once,
and a custom Caddy configuration was the only way out.

``CADDY_TRUSTED_PROXIES`` now names the proxies Caddy believes (ranges
separated by spaces, or ``private_ranges``); empty -- the default -- trusts
nobody, exactly as before. Compose passes it through. The browser headers
of the Caddyfile stay untouched (their equality with the API's own copy is
pinned in ``test_demo_hosting.py``).

Checked as text: there is no Caddy in the test environment (the syntax was
checked with ``caddy validate`` for the empty default, two ranges and
``private_ranges``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

pytestmark = pytest.mark.skipif(not (_DEPLOY / "Caddyfile").is_file(), reason="no deploy/")


def _global_block(caddyfile: str) -> str:
    """The global options block (the first ``{ ... }`` at the start of a line)."""

    start = caddyfile.index("\n{\n") + 1
    depth = 0
    for pos in range(start, len(caddyfile)):
        if caddyfile[pos] == "{":
            depth += 1
        elif caddyfile[pos] == "}":
            depth -= 1
            if depth == 0:
                return caddyfile[start : pos + 1]
    raise AssertionError("global options block not closed")


def test_caddy_takes_its_trusted_proxies_from_the_environment_defaulting_to_nobody() -> None:
    caddyfile = (_DEPLOY / "Caddyfile").read_text(encoding="utf-8")
    block = _global_block(caddyfile)

    assert re.search(r"servers\s*\{[^}]*trusted_proxies static \{\$CADDY_TRUSTED_PROXIES:\}", block)
    # documented next to the other variables of the file
    assert "CADDY_TRUSTED_PROXIES" in caddyfile.split("\n{\n", 1)[0]


def test_compose_passes_the_setting_to_the_web_server_empty_by_default() -> None:
    compose = (_DEPLOY / "docker-compose.full.yml").read_text(encoding="utf-8")
    web = compose[compose.index("\n  web:\n") :]
    web = web[: web.index("\n  backup:\n")] if "\n  backup:\n" in web else web

    assert 'CADDY_TRUSTED_PROXIES: "${CADDY_TRUSTED_PROXIES:-}"' in web
