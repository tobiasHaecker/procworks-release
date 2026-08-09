# SPDX-License-Identifier: BUSL-1.1
"""JWT/OIDC bearer auth backend (Auth-Konzept §3.2, the ``JwtAuthBackend``).

Third production backend besides token/password: the API trusts an external
identity provider (Entra ID, Keycloak, ...) and validates the bearer JWT it
issued -- signature, issuer, audience, expiry -- then maps its claims onto the
boundary :class:`~procworks.auth.Principal`. The core stays untouched: fine-
grained BZR eligibility keeps living in the kernel, this is the coarse layer.

Two mutually exclusive verification paths (chosen by configuration, never by
the token -- that closes the classic algorithm-confusion attack where an
attacker re-signs an RS256 token as HS256 with the public key as secret):

* **RS256 via JWKS** (``PROCWORKS_JWT_JWKS_URL``): the OIDC-standard path.
  Keys are fetched lazily and cached (5 min; an unknown ``kid`` refreshes once
  -- key rotation). Needs the optional ``cryptography`` package (same lazy
  dependency policy as the licensing layer: absent -> clear startup error).
* **HS256 via shared secret** (``PROCWORKS_JWT_HS256_SECRET``): stdlib-only,
  for gateways/tests that mint tokens themselves.

Claims mapping (all configurable): ``sub`` -> subject, the roles claim
(default ``roles``, dotted paths like Keycloaks ``realm_access.roles``
supported) is filtered against the known ProcWorks roles -- the IdP assigns
the role *names* ``admin``/``modeler``/``operator``/``viewer``/
``integration`` --, the agent claim (default ``agent_id``) binds the caller to
a concrete agent (a bound principal can only act as itself), the scopes claim
(default ``scope``, space-separated or list) feeds the ``/v1`` integration
scopes. Unknown roles/scopes are ignored, never an error -- an IdP may carry
app-foreign roles.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from procworks.auth import (
    ALL_SCOPES,
    KNOWN_ROLES,
    AuthError,
    Principal,
    bearer_token,
)

#: Clock leeway for exp/nbf comparisons (seconds) -- absorbs small skews
#: between the IdP and this host without weakening expiry meaningfully.
LEEWAY_SECONDS = 60.0
#: JWKS cache lifetime; an unknown ``kid`` additionally forces one refresh.
JWKS_TTL_SECONDS = 300.0


def _b64url(segment: str) -> bytes:
    """Decode one base64url JWT segment (padding restored)."""

    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise AuthError("malformed token encoding") from exc


def _default_fetcher(url: str) -> dict[str, Any]:
    """Fetch and parse a JWKS document (5s timeout, boundary-only I/O)."""

    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        parsed = json.load(response)
    if not isinstance(parsed, dict):
        raise ValueError("JWKS document must be a JSON object")
    return parsed


class JwtAuthBackend:
    """Validate IdP-issued bearer JWTs and map their claims to a Principal."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        hs256_secret: str | None = None,
        roles_claim: str = "roles",
        agent_claim: str = "agent_id",
        scopes_claim: str = "scope",
        fetcher: Callable[[str], dict[str, Any]] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        if bool(jwks_url) == bool(hs256_secret):
            raise ValueError(
                "configure exactly one of PROCWORKS_JWT_JWKS_URL (RS256) and "
                "PROCWORKS_JWT_HS256_SECRET (HS256) -- never both, never none"
            )
        if jwks_url is not None:
            # Fail at startup, not at the first request: the RS256 path needs
            # the optional cryptography package (lazy policy like licensing).
            self._import_crypto()
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._secret = hs256_secret.encode("utf-8") if hs256_secret else None
        self._roles_claim = roles_claim
        self._agent_claim = agent_claim
        self._scopes_claim = scopes_claim
        self._fetcher = fetcher or _default_fetcher
        self._now = now or time.time
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0

    @classmethod
    def from_env(cls) -> JwtAuthBackend:
        """Build the backend from ``PROCWORKS_JWT_*`` (issuer/audience required)."""

        issuer = os.environ.get("PROCWORKS_JWT_ISSUER", "").strip()
        audience = os.environ.get("PROCWORKS_JWT_AUDIENCE", "").strip()
        if not issuer or not audience:
            raise ValueError(
                "PROCWORKS_AUTH=jwt requires PROCWORKS_JWT_ISSUER and "
                "PROCWORKS_JWT_AUDIENCE"
            )
        return cls(
            issuer=issuer,
            audience=audience,
            jwks_url=os.environ.get("PROCWORKS_JWT_JWKS_URL", "").strip() or None,
            hs256_secret=os.environ.get("PROCWORKS_JWT_HS256_SECRET", "").strip()
            or None,
            roles_claim=os.environ.get("PROCWORKS_JWT_ROLES_CLAIM", "roles").strip(),
            agent_claim=os.environ.get("PROCWORKS_JWT_AGENT_CLAIM", "agent_id").strip(),
            scopes_claim=os.environ.get("PROCWORKS_JWT_SCOPES_CLAIM", "scope").strip(),
        )

    # -- AuthBackend protocol ----------------------------------------------

    def authenticate(self, authorization: str | None) -> Principal:
        token = bearer_token(authorization)
        if token is None:
            raise AuthError("missing bearer token")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("malformed token")
        header_b64, payload_b64, signature_b64 = parts
        try:
            header = json.loads(_b64url(header_b64))
            payload = json.loads(_b64url(payload_b64))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AuthError("malformed token payload") from exc
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise AuthError("malformed token payload")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        signature = _b64url(signature_b64)
        self._verify_signature(header, signing_input, signature)
        self._verify_claims(payload)
        return self._principal_from(payload)

    # -- signature ----------------------------------------------------------

    def _verify_signature(
        self, header: dict[str, Any], signing_input: bytes, signature: bytes
    ) -> None:
        """Verify per the *configured* path; the token never picks the alg."""

        alg = header.get("alg")
        if self._secret is not None:
            if alg != "HS256":
                raise AuthError("token algorithm must be HS256")
            expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, signature):
                raise AuthError("invalid token signature")
            return
        if alg != "RS256":
            raise AuthError("token algorithm must be RS256")
        key = self._key_for(header.get("kid"))
        padding, hashes = self._import_crypto()
        try:
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except Exception as exc:  # noqa: BLE001 -- any crypto failure is a 401
            raise AuthError("invalid token signature") from exc

    @staticmethod
    def _import_crypto() -> tuple[Any, Any]:
        """Lazy cryptography import with a clear error when it is missing."""

        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ValueError(
                "the RS256/JWKS path needs the optional 'cryptography' package "
                "(pip install 'procworks[jwt]')"
            ) from exc
        return padding, hashes

    def _key_for(self, kid: object) -> Any:
        """Resolve the verification key for ``kid`` from the cached JWKS.

        A stale cache or an unknown ``kid`` triggers one refresh (key
        rotation); a still-unknown ``kid`` is a 401, and a JWKS fetch failure
        fails **closed** (401) -- auth never degrades to unauthenticated.
        """

        now = self._now()
        stale = now - self._fetched_at > JWKS_TTL_SECONDS
        wanted = kid if isinstance(kid, str) else None
        if stale or (wanted is not None and wanted not in self._keys):
            self._refresh_keys()
        if wanted is not None:
            key = self._keys.get(wanted)
            if key is None:
                raise AuthError("unknown token key id")
            return key
        if len(self._keys) == 1:  # kid-less token against a single-key JWKS
            return next(iter(self._keys.values()))
        raise AuthError("token names no key id")

    def _refresh_keys(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        assert self._jwks_url is not None
        try:
            document = self._fetcher(self._jwks_url)
        except Exception as exc:  # noqa: BLE001 -- fail closed on fetch errors
            raise AuthError("could not fetch the token signing keys") from exc
        keys: dict[str, Any] = {}
        for jwk in document.get("keys", []):
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
                continue
            if jwk.get("use") not in (None, "sig"):
                continue
            try:
                modulus = int.from_bytes(_b64url(str(jwk["n"])), "big")
                exponent = int.from_bytes(_b64url(str(jwk["e"])), "big")
            except (KeyError, AuthError):
                continue
            public = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            keys[str(jwk.get("kid", ""))] = public
        if not keys:
            raise AuthError("the JWKS document contains no usable RSA key")
        self._keys = keys
        self._fetched_at = self._now()

    # -- claims --------------------------------------------------------------

    def _verify_claims(self, payload: dict[str, Any]) -> None:
        now = self._now()
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            raise AuthError("token has no expiry")
        if now > float(exp) + LEEWAY_SECONDS:
            raise AuthError("token expired")
        nbf = payload.get("nbf")
        if isinstance(nbf, (int, float)) and now < float(nbf) - LEEWAY_SECONDS:
            raise AuthError("token not yet valid")
        if payload.get("iss") != self._issuer:
            raise AuthError("wrong token issuer")
        aud = payload.get("aud")
        audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if self._audience not in audiences:
            raise AuthError("wrong token audience")

    def _principal_from(self, payload: dict[str, Any]) -> Principal:
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthError("token has no subject")
        roles = frozenset(
            r for r in self._string_list(self._claim(payload, self._roles_claim))
        ) & KNOWN_ROLES
        scopes = frozenset(
            s for s in self._string_list(self._claim(payload, self._scopes_claim))
        ) & ALL_SCOPES
        agent = payload.get(self._agent_claim)
        display = payload.get("name") or payload.get("preferred_username")
        return Principal(
            subject=subject,
            agent_id=agent if isinstance(agent, str) and agent else None,
            roles=roles,
            scopes=scopes,
            display_name=display if isinstance(display, str) else None,
        )

    @staticmethod
    def _claim(payload: dict[str, Any], path: str) -> object:
        """Resolve a possibly dotted claim path (Keycloak: realm_access.roles)."""

        current: object = payload
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _string_list(value: object) -> list[str]:
        """Normalise a claim to a string list (list, or space-separated str)."""

        if isinstance(value, str):
            return value.split()
        if isinstance(value, list):
            return [str(v) for v in value]
        return []
