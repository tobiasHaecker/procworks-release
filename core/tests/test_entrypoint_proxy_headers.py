# SPDX-License-Identifier: BUSL-1.1
"""Only ``PROCWORKS_TRUSTED_PROXIES`` decides whose ``X-Forwarded-For`` counts.

ProcWorks reads the client address for the login lock itself, believing the
header only from the proxies named in ``PROCWORKS_TRUSTED_PROXIES``. uvicorn,
however, evaluates ``X-Forwarded-For`` on its own by default -- for every
peer in ``FORWARDED_ALLOW_IPS`` (local addresses) it replaces the client
address with the header before the application sees it. ``none`` was then
without effect for such peers, and any text in the header (not even an
address) became the key of the lock.

The container's entrypoint therefore starts uvicorn without its own proxy
evaluation (``UVICORN_PROXY_HEADERS=false``, unless the operator set it).
Pinned here: the entrypoint sets the variable and keeps an explicit one,
uvicorn reads it, and a uvicorn started through the entrypoint hands the
real peer to the application although the request names another address.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from uvicorn.main import main as uvicorn_main

_ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"


def _clean_env() -> dict[str, str]:
    """The test's environment without a database and without the variable."""

    unwanted = ("DATABASE_URL", "UVICORN_PROXY_HEADERS")
    return {k: v for k, v in os.environ.items() if k not in unwanted}


def _environment_after_entrypoint(extra: dict[str, str]) -> dict[str, str]:
    env = _clean_env()
    env.update(extra)
    done = subprocess.run(
        ["sh", str(_ENTRYPOINT), "env"], env=env, capture_output=True, text=True, timeout=30,
        check=True,
    )
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def test_the_entrypoint_switches_uvicorns_own_proxy_evaluation_off() -> None:
    assert _environment_after_entrypoint({})["UVICORN_PROXY_HEADERS"] == "false"


def test_an_explicit_setting_of_the_operator_is_kept() -> None:
    env = _environment_after_entrypoint({"UVICORN_PROXY_HEADERS": "true"})

    assert env["UVICORN_PROXY_HEADERS"] == "true"


@pytest.mark.parametrize(("value", "expected"), [(None, True), ("false", False)])
def test_uvicorn_reads_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    monkeypatch.delenv("UVICORN_PROXY_HEADERS", raising=False)
    if value is not None:
        monkeypatch.setenv("UVICORN_PROXY_HEADERS", value)

    context = uvicorn_main.make_context("uvicorn", ["procworks.api:app"])

    assert context.params["proxy_headers"] is expected


_ECHO_APP = '''
import json

async def app(scope, receive, send):
    body = json.dumps({"client": scope["client"][0]}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
'''


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.parametrize(("through_entrypoint", "expected"), [(True, "127.0.0.1"),
                                                               (False, "203.0.113.9")])
def test_the_application_sees_the_peer_not_the_header(
    tmp_path: Path, through_entrypoint: bool, expected: str
) -> None:
    """Started like the container: the header does not replace the peer.

    The second case is the counter-check -- uvicorn started directly does
    replace it (local peer), which is what the entrypoint switches off.
    """

    (tmp_path / "echo_app.py").write_text(_ECHO_APP, encoding="utf-8")
    port = _free_port()
    command = [sys.executable, "-m", "uvicorn", "echo_app:app", "--app-dir", str(tmp_path),
               "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    if through_entrypoint:
        command = ["sh", str(_ENTRYPOINT), *command]
    server = subprocess.Popen(
        command, env=_clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/", headers={"X-Forwarded-For": "203.0.113.9"}
        )
        deadline = time.monotonic() + 15
        while True:
            try:
                with urllib.request.urlopen(request, timeout=2) as answer:
                    seen = json.loads(answer.read())["client"]
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
    finally:
        server.terminate()
        server.wait(10)

    assert seen == expected
