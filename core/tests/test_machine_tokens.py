# SPDX-License-Identifier: BUSL-1.1
"""Machine tokens next to password logins.

The delivered stack runs in password mode, and the auth mode is exclusive: the
integration tokens the Integrations-Leitfaden describes only existed in token
mode, and the ``integration`` role cannot be given to a password user. A worker
had to use a personal operator account. Now the password backend additionally
accepts static tokens -- but only ones whose sole role is ``integration``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks.api import app
from procworks.auth import AuthError
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    hash_password,
    machine_tokens_from_env,
)
from procworks.auth_token import TokenAuthBackend, load_token_config

client = TestClient(app)

WORKER = {"worker-geheim": {"subject": "erp-worker", "roles": ["integration"],
                            "scopes": ["tasks:fetch", "tasks:complete"]}}


@pytest.fixture
def password_with_tokens() -> Iterator[PasswordAuthBackend]:
    backend = PasswordAuthBackend(
        InMemoryCredentialStore(), machine_tokens=TokenAuthBackend(WORKER)
    )
    backend.create_user(subject="erika", login="erika", roles=["operator"], agent_id="a1")
    user = backend.store.get_user("erika")
    assert user is not None
    backend.store.put_user(
        user.model_copy(update={"password_hash": hash_password("secret-pw1"), "must_change": False})
    )
    original = api_module._auth_backend
    api_module._auth_backend = backend
    try:
        yield backend
    finally:
        api_module._auth_backend = original


def test_machine_token_works_in_password_mode(password_with_tokens: PasswordAuthBackend) -> None:
    headers = {"Authorization": "Bearer worker-geheim"}
    resp = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w-1", "topics": ["gibtsnicht"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    principal = password_with_tokens.authenticate("Bearer worker-geheim")
    assert principal.roles == frozenset({"integration"})
    assert principal.scopes == frozenset({"tasks:fetch", "tasks:complete"})


def test_machine_token_stays_within_its_role(password_with_tokens: PasswordAuthBackend) -> None:
    headers = {"Authorization": "Bearer worker-geheim"}
    assert client.get("/users", headers=headers).status_code == 403
    assert client.post("/schemas", json={"name": "x"}, headers=headers).status_code == 403


def test_people_still_log_in_and_unknown_tokens_are_refused(
    password_with_tokens: PasswordAuthBackend,
) -> None:
    session = password_with_tokens.login("erika", "secret-pw1").token
    assert password_with_tokens.authenticate(f"Bearer {session}").subject == "erika"
    with pytest.raises(AuthError):
        password_with_tokens.authenticate("Bearer geraten")
    assert client.get("/auth/me", headers={"Authorization": "Bearer geraten"}).status_code == 401


def test_only_integration_tokens_are_accepted_next_to_passwords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token is a secret without a password behind it -- never a person or admin."""

    monkeypatch.delenv("PROCWORKS_TOKENS", raising=False)
    monkeypatch.setenv(
        "PROCWORKS_TOKENS_JSON",
        json.dumps({"t": {"subject": "root-bot", "roles": ["admin"], "scopes": ["*"]}}),
    )
    with pytest.raises(ValueError, match="only machine tokens"):
        machine_tokens_from_env()
    monkeypatch.setenv("PROCWORKS_TOKENS_JSON", json.dumps(WORKER))
    backend = machine_tokens_from_env()
    assert backend is not None and backend.principals[0].subject == "erp-worker"


def test_token_configuration_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PROCWORKS_TOKENS", raising=False)
    monkeypatch.delenv("PROCWORKS_TOKENS_JSON", raising=False)
    assert load_token_config() is None
    monkeypatch.setenv("PROCWORKS_TOKENS_JSON", json.dumps(WORKER))
    assert load_token_config() == WORKER
    file = tmp_path / "tokens.json"
    file.write_text(json.dumps({"datei": WORKER["worker-geheim"]}), encoding="utf-8")
    monkeypatch.setenv("PROCWORKS_TOKENS", str(file))
    assert load_token_config() == {"datei": WORKER["worker-geheim"]}  # file wins
    monkeypatch.setenv("PROCWORKS_TOKENS_JSON", "[1, 2]")
    monkeypatch.delenv("PROCWORKS_TOKENS")
    with pytest.raises(ValueError):
        load_token_config()
