# SPDX-License-Identifier: BUSL-1.1
"""Password-based auth backend: self-contained login for standalone deployments.

This is the second concrete :class:`procworks.auth.AuthBackend`. It exists so a
deployment can offer a real login screen (username + password, self-service
password change) without an external identity provider.

Design decisions (see ``docs/Auth-Konzept.md`` section 11):

* **Credentials are not part of the org model.** An :class:`procworks.model.Agent`
  is a *modelling* artefact (CbC-validated, persisted, shareable across models);
  a :class:`User` is *operational security state*. They are linked only by
  ``agent_id``. Users live in a dedicated :class:`CredentialStore` (in-memory or
  SQLAlchemy), never inside a schema.
* **Passwords are hashed with the standard library** (:func:`hashlib.scrypt`),
  so no extra runtime dependency is pulled in. Each user gets a random salt;
  verification is constant-time.
* **Sessions are opaque bearer tokens** issued on login and kept *in memory*
  (only their SHA-256 digest). Losing them on restart simply forces a re-login;
  the durable part (the users) lives in the credential store.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from procworks.auth import ALL_ROLES, AuthError, Principal, bearer_token

#: Login used for the auto-provisioned initial admin when none is configured.
DEFAULT_ADMIN_LOGIN = "admin"

_logger = logging.getLogger("procworks.auth")

#: Minimum length enforced for a self-chosen password.
MIN_PASSWORD_LENGTH = 8

# scrypt cost parameters (interactive login defaults). 128*N*r*p bytes of memory
# => ~16 MiB here, comfortably under scrypt's default 32 MiB ceiling.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16


class PasswordPolicyError(ValueError):
    """Raised when a chosen password violates the password policy (400)."""


def hash_password(password: str) -> str:
    """Hash a clear-text password with a random salt (stdlib scrypt)."""

    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a clear-text password against a stored hash."""

    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def generate_initial_password() -> str:
    """A high-entropy initial password shown once to the admin on provisioning."""

    return secrets.token_urlsafe(9)


_UMLAUTS = str.maketrans(
    {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
        "Ä": "ae", "Ö": "oe", "Ü": "ue",
    }
)


def suggest_login(name: str, existing: Iterable[str] = ()) -> str:
    """Derive a stable login like ``erika.musterfrau`` from a display name.

    Umlauts/diacritics are transliterated, the result is lowercased and joined
    with dots. Collisions get a numeric suffix (``...2``, ``...3``). The returned
    login is only a *suggestion*; once stored it is independent of the name.
    """

    transliterated = name.translate(_UMLAUTS)
    ascii_only = (
        unicodedata.normalize("NFKD", transliterated)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", ascii_only.lower()) if p]
    base = ".".join(parts) or "user"
    taken = {e.lower() for e in existing}
    if base not in taken:
        return base
    suffix = 2
    while f"{base}{suffix}" in taken:
        suffix += 1
    return f"{base}{suffix}"


class User(BaseModel):
    """A login identity with a hashed password, linked to an agent.

    ``password_hash`` never leaves the server; use :class:`UserView` for any
    client-facing response.
    """

    login: str
    password_hash: str
    subject: str
    agent_id: str | None = None
    roles: frozenset[str] = Field(default_factory=frozenset)
    display_name: str | None = None
    must_change: bool = True


class UserView(BaseModel):
    """Client-safe projection of a :class:`User` (no password hash)."""

    login: str
    subject: str
    agent_id: str | None = None
    roles: frozenset[str] = Field(default_factory=frozenset)
    display_name: str | None = None
    must_change: bool = True


def user_view(user: User) -> UserView:
    return UserView(
        login=user.login,
        subject=user.subject,
        agent_id=user.agent_id,
        roles=user.roles,
        display_name=user.display_name,
        must_change=user.must_change,
    )


def _principal_of(user: User) -> Principal:
    # ``subject`` is the stable login, so /auth/change-password can find the user.
    return Principal(
        subject=user.login,
        agent_id=user.agent_id,
        roles=user.roles,
        display_name=user.display_name or user.login,
    )


@runtime_checkable
class CredentialStore(Protocol):
    """Persistence interface for login users (durable part of the backend)."""

    def get_user(self, login: str) -> User | None: ...

    def put_user(self, user: User) -> User: ...

    def list_users(self) -> list[User]: ...

    def delete_user(self, login: str) -> None: ...


class InMemoryCredentialStore:
    """A trivial dict-backed user store (default without ``DATABASE_URL``)."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    def get_user(self, login: str) -> User | None:
        return self._users.get(login)

    def put_user(self, user: User) -> User:
        self._users[user.login] = user
        return user

    def list_users(self) -> list[User]:
        return list(self._users.values())

    def delete_user(self, login: str) -> None:
        self._users.pop(login, None)


def create_credential_store() -> CredentialStore:
    """Build the credential store from the environment (mirrors ``create_store``)."""

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyCredentialStore

        return SqlAlchemyCredentialStore(url, create_tables=True)
    return InMemoryCredentialStore()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _SessionInfo:
    __slots__ = ("login", "expires_at")

    def __init__(self, login: str, expires_at: datetime) -> None:
        self.login = login
        self.expires_at = expires_at


class LoginResult(BaseModel):
    """Outcome of a successful login (the SPA stores ``token`` as a bearer)."""

    token: str
    principal: Principal
    must_change: bool


class PasswordAuthBackend:
    """Authenticate via opaque session tokens issued by username+password login.

    The richer operations (``login``/``logout``/``change_password``) are called
    directly by the dedicated ``/auth/*`` endpoints; the generic
    :meth:`authenticate` only validates an existing session bearer token.
    """

    def __init__(
        self,
        store: CredentialStore,
        *,
        session_ttl: timedelta = timedelta(hours=12),
    ) -> None:
        self._store = store
        self._ttl = session_ttl
        self._sessions: dict[str, _SessionInfo] = {}

    @property
    def store(self) -> CredentialStore:
        return self._store

    @classmethod
    def from_env(cls) -> PasswordAuthBackend:
        minutes = os.environ.get("PROCWORKS_SESSION_TTL_MINUTES")
        ttl = timedelta(minutes=int(minutes)) if minutes else timedelta(hours=12)
        backend = cls(create_credential_store(), session_ttl=ttl)
        backend._bootstrap_admin()
        return backend

    def _bootstrap_admin(self) -> None:
        """Seed an initial admin so password mode is usable on a fresh store.

        Without this, a brand-new credential store has no user and nobody could
        log in to create the first one. Two paths:

        * ``PROCWORKS_ADMIN_LOGIN`` / ``PROCWORKS_ADMIN_PASSWORD`` provision
          exactly one admin with a known password (idempotent across restarts).
        * Otherwise, when the store holds *no users at all*, a default admin
          (login ``admin``) is created with a freshly generated password that is
          written to the server log once -- the operator reads it from the log
          and must change it on first login.
        """

        login = os.environ.get("PROCWORKS_ADMIN_LOGIN")
        password = os.environ.get("PROCWORKS_ADMIN_PASSWORD")
        display_name = os.environ.get("PROCWORKS_ADMIN_NAME")

        if login and password:
            if self._store.get_user(login) is not None:
                return
            self._store.put_user(
                User(
                    login=login,
                    password_hash=hash_password(password),
                    subject=login,
                    roles=frozenset({"admin"}),
                    display_name=display_name,
                    must_change=True,
                )
            )
            return

        # No explicit admin password configured: only seed when the store is
        # completely fresh, so we never resurrect a deleted admin or clobber an
        # existing deployment.
        if self._store.list_users():
            return
        login = login or DEFAULT_ADMIN_LOGIN
        password = generate_initial_password()
        self._store.put_user(
            User(
                login=login,
                password_hash=hash_password(password),
                subject=login,
                roles=frozenset({"admin"}),
                display_name=display_name,
                must_change=True,
            )
        )
        _logger.warning(
            "Initial admin account created (login=%r, temporary password=%r). "
            "Log in and change this password immediately; it will not be shown "
            "again.",
            login,
            password,
        )

    # -- AuthBackend protocol ----------------------------------------------

    def authenticate(self, authorization: str | None) -> Principal:
        token = bearer_token(authorization)
        if token is None:
            raise AuthError("missing session token")
        session = self._sessions.get(_digest(token))
        if session is None:
            raise AuthError("invalid session")
        if session.expires_at <= datetime.now(UTC):
            self._sessions.pop(_digest(token), None)
            raise AuthError("session expired")
        user = self._store.get_user(session.login)
        if user is None:
            self._sessions.pop(_digest(token), None)
            raise AuthError("unknown user")
        return _principal_of(user)

    # -- password operations -----------------------------------------------

    def login(self, login: str, password: str) -> LoginResult:
        user = self._store.get_user(login)
        # Verify even when the user is unknown to avoid a timing oracle.
        reference = user.password_hash if user else hash_password(secrets.token_hex())
        ok = verify_password(password, reference)
        if user is None or not ok:
            raise AuthError("invalid credentials")
        token = secrets.token_urlsafe(32)
        self._sessions[_digest(token)] = _SessionInfo(
            login=user.login, expires_at=datetime.now(UTC) + self._ttl
        )
        return LoginResult(
            token=token, principal=_principal_of(user), must_change=user.must_change
        )

    def logout(self, authorization: str | None) -> None:
        token = bearer_token(authorization)
        if token is not None:
            self._sessions.pop(_digest(token), None)

    def change_password(self, login: str, current: str, new: str) -> None:
        user = self._store.get_user(login)
        if user is None or not verify_password(current, user.password_hash):
            raise AuthError("invalid credentials")
        if len(new) < MIN_PASSWORD_LENGTH:
            raise PasswordPolicyError(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        if new == current:
            raise PasswordPolicyError("new password must differ from the current one")
        self._store.put_user(
            user.model_copy(
                update={"password_hash": hash_password(new), "must_change": False}
            )
        )

    # -- provisioning ------------------------------------------------------

    def create_user(
        self,
        *,
        subject: str,
        roles: Iterable[str],
        agent_id: str | None = None,
        login: str | None = None,
        display_name: str | None = None,
    ) -> tuple[User, str]:
        """Create a user and return it together with the one-off initial password."""

        role_set = frozenset(str(r) for r in roles)
        unknown = role_set - ALL_ROLES
        if unknown:
            raise PasswordPolicyError(f"unknown role(s): {sorted(unknown)}")
        chosen = login or suggest_login(
            display_name or subject, {u.login for u in self._store.list_users()}
        )
        if self._store.get_user(chosen) is not None:
            raise PasswordPolicyError(f"login '{chosen}' already exists")
        initial = generate_initial_password()
        user = User(
            login=chosen,
            password_hash=hash_password(initial),
            subject=subject,
            agent_id=agent_id,
            roles=role_set,
            display_name=display_name,
            must_change=True,
        )
        self._store.put_user(user)
        return user, initial

    def reset_password(self, login: str) -> str:
        """Set a fresh initial password (forces change) and return it once."""

        user = self._store.get_user(login)
        if user is None:
            raise KeyError(login)
        initial = generate_initial_password()
        self._store.put_user(
            user.model_copy(
                update={"password_hash": hash_password(initial), "must_change": True}
            )
        )
        return initial
