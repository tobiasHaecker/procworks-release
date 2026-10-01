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
    """SHA-256 hex digest of a bearer token -- the only form that is kept.

    Also used by :mod:`procworks.auth_password` for its session keys, so both
    backends index tokens identically.
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _known_names(
    entry: dict[str, object],
    key: str,
    noun: str,
    known: frozenset[str],
    subject: str,
) -> frozenset[str]:
    """Read a list-valued field of a token entry and check it against ``known``.

    Shared by the ``roles`` and ``scopes`` fields, which follow the same rules:
    a missing field means "none", anything other than a list/tuple is a
    configuration error, and every name must be one ProcWorks knows.

    :param entry: one token entry from the configuration.
    :param key: the field to read (``"roles"`` or ``"scopes"``).
    :param noun: singular for the error message (``"role"`` or ``"scope"``).
    :param known: the admissible names.
    :param subject: the entry's subject, named in every error message.
    :returns: the names as a frozenset (each coerced to ``str``).
    :raises ValueError: at start-up for a non-list field or an unknown name.
    """

    raw = entry.get(key, [])
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"token entry '{subject}' has non-list '{key}'")
    names = frozenset(str(item) for item in raw)
    unknown = names - known
    if unknown:
        raise ValueError(
            f"token entry '{subject}' has unknown {noun}(s): {sorted(unknown)}"
        )
    return names


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
        roles = _known_names(entry, "roles", "role", KNOWN_ROLES, subject)
        scopes = _known_names(entry, "scopes", "scope", ALL_SCOPES, subject)
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
        """Build the backend from ``PROCWORKS_TOKENS`` / ``PROCWORKS_TOKENS_JSON``."""

        data = load_token_config()
        if data is None:
            raise ValueError(
                "PROCWORKS_AUTH=token requires PROCWORKS_TOKENS (path to a JSON "
                "token file) or PROCWORKS_TOKENS_JSON (the JSON itself)"
            )
        return cls(data)

    @property
    def principals(self) -> list[Principal]:
        """All configured identities (for start-up checks of the caller)."""

        return list(self._by_digest.values())

    def authenticate(self, authorization: str | None) -> Principal:
        token = bearer_token(authorization)
        if token is None:
            raise AuthError("missing bearer token")
        principal = self._by_digest.get(_digest(token))
        if principal is None:
            raise AuthError("invalid token")
        return principal


def load_token_config() -> dict[str, dict[str, object]] | None:
    """Read the static token configuration from the environment, if any.

    Two equivalent sources, the file taking precedence:

    * ``PROCWORKS_TOKENS`` -- path to a JSON file ``{token: {subject, roles, scopes}}``;
    * ``PROCWORKS_TOKENS_JSON`` -- the same JSON as a string. Added for the
      Compose stack: a variable in ``deploy/.env``
      reaches the container without mounting a file into it.

    :returns: the parsed mapping, or ``None`` when neither variable is set.
    :raises ValueError: when the content is not a JSON object.
    """

    path = os.environ.get("PROCWORKS_TOKENS", "").strip()
    inline = os.environ.get("PROCWORKS_TOKENS_JSON", "").strip()
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    elif inline:
        data = json.loads(inline)
    else:
        return None
    if not isinstance(data, dict):
        raise ValueError("token configuration must be a JSON object {token: {...}}")
    return data
