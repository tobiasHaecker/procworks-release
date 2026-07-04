# SPDX-License-Identifier: BUSL-1.1
"""Pluggable authentication for the API boundary (Auth concept, Variant C).

The domain core stays headless and correctness-only: fine-grained eligibility
(BZR/Z-rules in :mod:`procworks.assignment`) is *not* moved here. Auth is an
additional, coarse protection layer at the single API boundary
(:mod:`procworks.api`):

* it establishes a server-side :class:`Principal` for every request (identity
  comes from a verified token, never from the request body), which structurally
  closes the impersonation gap on ``/complete``; and
* it offers coarse role-based access control (RBAC) that *complements* the
  core's eligibility checks.

The backend is swappable behind the :class:`AuthBackend` protocol -- the same
protocol/factory pattern the persistence layer already uses
(``create_store`` reading ``DATABASE_URL``). The default is an open dev mode so
the quickstart, local tests and the prototype keep working unchanged.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

#: Coarse RBAC roles at the boundary. The user-facing wording is German
#: (Administrator / Modellierer / Bearbeiter / Leser); the technical identifiers
#: stay English to match the rest of the codebase.
ADMIN = "admin"
MODELER = "modeler"
OPERATOR = "operator"
VIEWER = "viewer"

#: Machine role for external tools (integration concept, roadmap E10). A service
#: token carrying this role may use the versioned ``/v1`` integration endpoints
#: according to its :data:`scopes`; it is *not* granted to human/open principals.
INTEGRATION = "integration"

#: Every human role -- the open dev backend grants all of them (unchanged).
ALL_ROLES: frozenset[str] = frozenset({ADMIN, MODELER, OPERATOR, VIEWER})

#: Every role a configured token may legitimately carry (humans + integration).
KNOWN_ROLES: frozenset[str] = ALL_ROLES | {INTEGRATION}

#: Fine-grained integration scopes a service token may hold (additive). They
#: only restrict :data:`INTEGRATION` identities; human roles are unaffected. The
#: wildcard ``"*"`` grants every scope.
SCOPE_INSTANCES_START = "instances:start"
SCOPE_TASKS_COMPLETE = "tasks:complete"
SCOPE_TASKS_FETCH = "tasks:fetch"
SCOPE_DATA_READ = "data:read"
SCOPE_DATA_WRITE = "data:write"
SCOPE_EVENTS_SUBSCRIBE = "events:subscribe"
SCOPE_WILDCARD = "*"
ALL_SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_INSTANCES_START,
        SCOPE_TASKS_COMPLETE,
        SCOPE_TASKS_FETCH,
        SCOPE_DATA_READ,
        SCOPE_DATA_WRITE,
        SCOPE_EVENTS_SUBSCRIBE,
        SCOPE_WILDCARD,
    }
)


class AuthError(Exception):
    """Raised by a backend when no valid identity can be established (401)."""

    def __init__(self, message: str = "authentication required") -> None:
        super().__init__(message)
        self.message = message


class Principal(BaseModel):
    """The verified, server-derived identity behind a single request.

    ``agent_id`` binds the caller to a concrete ProcWorks agent. Only this id is
    handed to :func:`procworks.execution.complete_activity`; it is never taken
    from the body. ``roles`` drives the coarse boundary RBAC, orthogonal to the
    core's fine-grained BZR eligibility.
    """

    subject: str = Field(..., examples=["anna"])
    agent_id: str | None = Field(default=None, examples=["a1"])
    roles: frozenset[str] = Field(default_factory=frozenset)
    scopes: frozenset[str] = Field(default_factory=frozenset)
    display_name: str | None = Field(default=None, examples=["Anna Beispiel"])

    @property
    def is_bound(self) -> bool:
        """Whether this principal is tied to a concrete agent (token/JWT)."""

        return self.agent_id is not None


@runtime_checkable
class AuthBackend(Protocol):
    """Strategy that derives a verified :class:`Principal` from a bearer token.

    Implementations receive the raw ``Authorization`` header value (or ``None``)
    rather than the framework request object, so they stay free of any web
    framework dependency and are trivial to unit-test.
    """

    def authenticate(self, authorization: str | None) -> Principal:
        """Return a verified principal or raise :class:`AuthError` (401)."""
        ...


class OpenAuthBackend:
    """Default dev/test backend: an anonymous principal with every role.

    No identity is required and ``agent_id`` is left unbound, so callers of
    ``/complete`` may still name an ``agent_id`` (the core BZR
    check keeps correctness intact). This preserves today's behaviour when no
    auth is configured.
    """

    def authenticate(self, authorization: str | None) -> Principal:
        return Principal(
            subject="anonymous",
            agent_id=None,
            roles=ALL_ROLES,
            display_name="Entwicklung (offen)",
        )


def bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""

    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def create_auth_backend() -> AuthBackend:
    """Select the auth backend from the environment (default: open dev mode).

    ``PROCWORKS_AUTH=token`` enables the static :class:`TokenAuthBackend`; any
    other value (or unset) keeps the open backend. The token backend is imported
    lazily so the in-memory/dev path stays free of its file I/O.
    """

    mode = os.environ.get("PROCWORKS_AUTH", "open").lower()
    if mode == "token":
        from procworks.auth_token import TokenAuthBackend

        return TokenAuthBackend.from_env()
    if mode == "password":
        from procworks.auth_password import PasswordAuthBackend

        return PasswordAuthBackend.from_env()
    return OpenAuthBackend()
