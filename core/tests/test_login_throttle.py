# SPDX-License-Identifier: BUSL-1.1
"""Password guessing is slowed down (Validierung 2026-09-25, VAL-06).

26 wrong passwords against the admin account in a row were answered at once
with 401. ``LoginThrottle`` now locks a login -- and, with a higher allowance,
a client address -- after a few failures, with a doubling lock time. The API
answers 429 with ``Retry-After`` *before* checking the password.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks.api import app
from procworks.auth_password import (
    InMemoryCredentialStore,
    LoginThrottle,
    PasswordAuthBackend,
    hash_password,
)

client = TestClient(app)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


# --- the policy ----------------------------------------------------------------


def test_free_attempts_then_doubling_lock() -> None:
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    for _ in range(5):
        assert throttle.retry_after("admin", None) == 0
        throttle.failure("admin", None)
    assert throttle.retry_after("admin", None) == 0  # five typos are free

    throttle.failure("admin", None)  # 6th
    assert throttle.retry_after("admin", None) == 30
    clock.now += timedelta(seconds=31)
    throttle.failure("admin", None)  # 7th
    assert throttle.retry_after("admin", None) == 60


def test_lock_is_capped_and_expires() -> None:
    clock = _Clock()
    throttle = LoginThrottle(now=clock)
    for _ in range(40):
        throttle.failure("admin", None)
    assert throttle.retry_after("admin", None) == 15 * 60
    clock.now += timedelta(minutes=16)
    assert throttle.retry_after("admin", None) == 0


def test_case_variants_share_one_counter() -> None:
    throttle = LoginThrottle(now=_Clock())
    for login in ("admin", "Admin", "ADMIN", " admin", "aDmin", "admiN"):
        throttle.failure(login, None)
    assert throttle.retry_after("admin", None) > 0


def test_address_is_throttled_across_logins_with_a_higher_allowance() -> None:
    throttle = LoginThrottle(now=_Clock())
    for i in range(20):
        throttle.failure(f"user{i}", "203.0.113.7")
    assert throttle.retry_after("someone-new", "203.0.113.7") == 0
    throttle.failure("user20", "203.0.113.7")  # 21st from that address
    assert throttle.retry_after("someone-new", "203.0.113.7") > 0
    assert throttle.retry_after("someone-new", "198.51.100.1") == 0


def test_success_clears_the_login_but_not_the_address() -> None:
    throttle = LoginThrottle(now=_Clock(), free_attempts=1, free_attempts_per_address=1)
    throttle.failure("mara", "203.0.113.7")
    throttle.failure("mara", "203.0.113.7")
    throttle.success("mara")
    assert throttle.retry_after("mara", None) == 0
    assert throttle.retry_after("mara", "203.0.113.7") > 0


# --- the endpoint ----------------------------------------------------------------


@pytest.fixture
def password_admin() -> Iterator[Any]:
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    backend.create_user(subject="admin", login="admin", roles=["admin"])
    user = backend.store.get_user("admin")
    assert user is not None
    backend.store.put_user(
        user.model_copy(
            update={"password_hash": hash_password("richtig-123"), "must_change": False}
        )
    )
    original = api_module._auth_backend
    api_module._auth_backend = backend
    try:
        yield backend
    finally:
        api_module._auth_backend = original


def _login(password: str, **headers: str) -> Any:
    return client.post(
        "/auth/login", json={"login": "admin", "password": password}, headers=headers
    )


def test_repeated_wrong_passwords_get_429_even_with_the_right_one(password_admin: Any) -> None:
    statuses = [_login("falsch").status_code for _ in range(6)]
    assert statuses == [401] * 6

    blocked = _login("richtig-123")

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert blocked.json()["detail"].startswith("Zu viele fehlgeschlagene Anmeldeversuche")


def test_a_successful_login_resets_the_counter(password_admin: Any) -> None:
    for _ in range(5):
        assert _login("falsch").status_code == 401
    assert _login("richtig-123").status_code == 200
    for _ in range(5):
        assert _login("falsch").status_code == 401  # free again after the success


def test_forwarded_for_counts_only_behind_an_internal_proxy() -> None:
    class _Req:
        def __init__(self, peer: str, forwarded: str) -> None:
            self.client = type("C", (), {"host": peer})()
            self.headers = {"X-Forwarded-For": forwarded}

    behind_caddy = _Req("172.18.0.5", "203.0.113.7")
    # A public peer (documentation ranges count as private in Python, hence 8.8.8.8).
    spoofed = _Req("8.8.8.8", "10.0.0.1")

    assert api_module._client_address(behind_caddy) == "203.0.113.7"  # type: ignore[arg-type]
    assert api_module._client_address(spoofed) == "8.8.8.8"  # type: ignore[arg-type]
