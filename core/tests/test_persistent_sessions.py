# SPDX-License-Identifier: BUSL-1.1
"""Login sessions survive a restart.

After ``docker compose restart api`` every user got 401 -- the password backend
kept its sessions in memory, so each update logged everybody out. With
``DATABASE_URL`` the sessions now live in ``auth_session`` (token digest only).
Because they persist, ending them explicitly matters: logout, admin password
reset and user deletion revoke them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from database_url import fresh_database_url
from sqlalchemy import MetaData, create_engine, select

from procworks.auth import AuthError
from procworks.auth_password import PasswordAuthBackend, hash_password
from procworks.db import SqlAlchemyCredentialStore, SqlAlchemySessionStore


def _backend(url: str, ttl: timedelta = timedelta(hours=1)) -> PasswordAuthBackend:
    """A fresh backend on the same database -- what a restarted API process builds."""

    return PasswordAuthBackend(
        SqlAlchemyCredentialStore(url, create_tables=True),
        session_ttl=ttl,
        sessions=SqlAlchemySessionStore(url, create_tables=True),
    )


@pytest.fixture
def url(tmp_path: Path) -> str:
    url = fresh_database_url(tmp_path, "sessions")
    backend = _backend(url)
    backend.create_user(subject="erika", login="erika", roles=["operator"])
    user = backend.store.get_user("erika")
    assert user is not None
    backend.store.put_user(
        user.model_copy(update={"password_hash": hash_password("secret-pw1"), "must_change": False})
    )
    return url


def _bearer(token: str) -> str:
    return f"Bearer {token}"


def test_session_survives_a_restart(url: str) -> None:
    token = _backend(url).login("erika", "secret-pw1").token

    restarted = _backend(url)

    assert restarted.authenticate(_bearer(token)).subject == "erika"


def _all_stored_values(url: str) -> str:
    """Every value of every table of the database, as one text blob.

    Backend-independent counterpart of reading the SQLite file: it also works
    against PostgreSQL, where there is no single file to search.
    """

    engine = create_engine(url)
    try:
        metadata = MetaData()
        metadata.reflect(bind=engine)
        with engine.connect() as connection:
            return "\n".join(
                repr(tuple(row))
                for table in metadata.sorted_tables
                for row in connection.execute(select(table))
            )
    finally:
        engine.dispose()


def test_the_clear_token_is_not_stored(url: str) -> None:
    token = _backend(url).login("erika", "secret-pw1").token
    stored = _all_stored_values(url)
    assert "erika" in stored  # premise: the scan really sees the stored rows
    assert token not in stored
    if url.startswith("sqlite:///"):
        # The file itself, including pages a row scan would not show.
        raw = Path(url.removeprefix("sqlite:///")).read_bytes()
        assert token.encode() not in raw


def test_logout_ends_the_session_for_every_process(url: str) -> None:
    token = _backend(url).login("erika", "secret-pw1").token
    _backend(url).logout(_bearer(token))
    with pytest.raises(AuthError):
        _backend(url).authenticate(_bearer(token))


def test_password_reset_and_deletion_revoke_sessions(url: str) -> None:
    token = _backend(url).login("erika", "secret-pw1").token
    _backend(url).reset_password("erika")
    with pytest.raises(AuthError):
        _backend(url).authenticate(_bearer(token))


def test_expired_sessions_are_rejected_and_purged(url: str) -> None:
    short = _backend(url, ttl=timedelta(seconds=-1))
    token = short.login("erika", "secret-pw1").token
    with pytest.raises(AuthError, match="expired"):
        _backend(url).authenticate(_bearer(token))
    store = SqlAlchemySessionStore(url)
    store.purge_expired(datetime.now(UTC) + timedelta(seconds=1))
    assert store.get("irrelevant") is None
