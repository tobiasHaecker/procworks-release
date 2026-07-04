# SPDX-License-Identifier: BUSL-1.1
"""Static bearer-token auth backend (first production-grade backend).

Tokens and their identity/roles are configured out-of-band in a JSON file
referenced by ``PROCWORKS_TOKENS``; this keeps secrets out of the code and the
image. The format is a flat mapping of *token -> identity*::

    {
      "s3cr3t-admin": {
        "subject": "alice",
        "roles": ["admin", "modeler"],
        "display_name": "Alice (Admin)"
      },
      "s3cr3t-anna": {
        "subject": "anna",
        "agent_id": "a1",
        "roles": ["operator"],
        "display_name": "Anna Beispiel"
      }
    }

``agent_id`` binds the token to a concrete ProcWorks agent (required for
operators who complete activities). Tokens are never kept in memory in clear
text: only their SHA-256 digest is stored and compared, so the resolved
identities cannot be read back out of the running process.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from procworks.auth import ALL_SCOPES, KNOWN_ROLES, AuthError, Principal, bearer_token


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenAuthBackend:
    """Authenticate callers against a static set of bearer tokens."""

    def __init__(self, tokens: dict[str, dict[str, object]]) -> None:
        # Index by token digest so the clear-text tokens are not retained.
        self._by_digest: dict[str, Principal] = {}
        for token, entry in tokens.items():
            self._by_digest[_digest(token)] = self._principal_from(entry)

    @staticmethod
    def _principal_from(entry: dict[str, object]) -> Principal:
        subject = entry.get("subject")
        if not isinstance(subject, str) or not subject:
            raise ValueError("each token entry needs a non-empty 'subject'")
        raw_roles = entry.get("roles", [])
        if not isinstance(raw_roles, (list, tuple)):
            raise ValueError(f"token entry '{subject}' has non-list 'roles'")
        roles = frozenset(str(r) for r in raw_roles)
        unknown = roles - KNOWN_ROLES
        if unknown:
            raise ValueError(
                f"token entry '{subject}' has unknown role(s): {sorted(unknown)}"
            )
        raw_scopes = entry.get("scopes", [])
        if not isinstance(raw_scopes, (list, tuple)):
            raise ValueError(f"token entry '{subject}' has non-list 'scopes'")
        scopes = frozenset(str(s) for s in raw_scopes)
        unknown_scopes = scopes - ALL_SCOPES
        if unknown_scopes:
            raise ValueError(
                f"token entry '{subject}' has unknown scope(s): "
                f"{sorted(unknown_scopes)}"
            )
        agent_id = entry.get("agent_id")
        if agent_id is not None and not isinstance(agent_id, str):
            raise ValueError(f"token entry '{subject}' has non-string 'agent_id'")
        display_name = entry.get("display_name")
        if display_name is not None and not isinstance(display_name, str):
            raise ValueError(f"token entry '{subject}' has non-string 'display_name'")
        return Principal(
            subject=subject,
            agent_id=agent_id,
            roles=roles,
            scopes=scopes,
            display_name=display_name,
        )

    @classmethod
    def from_env(cls) -> TokenAuthBackend:
        """Build the backend from the ``PROCWORKS_TOKENS`` JSON file."""

        path = os.environ.get("PROCWORKS_TOKENS")
        if not path:
            raise ValueError(
                "PROCWORKS_AUTH=token requires PROCWORKS_TOKENS to point at a "
                "JSON token file"
            )
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("token file must contain a JSON object {token: {...}}")
        return cls(data)

    def authenticate(self, authorization: str | None) -> Principal:
        token = bearer_token(authorization)
        if token is None:
            raise AuthError("missing bearer token")
        principal = self._by_digest.get(_digest(token))
        if principal is None:
            raise AuthError("invalid token")
        return principal
