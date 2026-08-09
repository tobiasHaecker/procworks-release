# SPDX-License-Identifier: BUSL-1.1
"""JwtAuthBackend (Auth-Konzept §3.2): IdP-issued bearer JWTs at the boundary.

HS256 (stdlib shared-secret path) carries the full claim/expiry/issuer/
audience semantics and runs everywhere; RS256/JWKS adds the OIDC key handling
(kid selection, rotation refresh, fail-closed fetch) and needs the optional
``cryptography`` package -- those tests skip cleanly where it is absent, the
same policy the licensing tests follow. The two paths are mutually exclusive
by construction (algorithm-confusion guard).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from procworks import api as api_module
from procworks.auth import AuthError
from procworks.auth_jwt import JwtAuthBackend

client = TestClient(api_module.app)

NOW = 1_754_700_000.0
ISS = "https://idp.example.org/realm"
AUD = "procworks"
SECRET = "test-secret"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hs256(
    payload: dict[str, Any],
    secret: str = SECRET,
    header: dict[str, Any] | None = None,
) -> str:
    head = _b64(json.dumps(header or {"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def _claims(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "iss": ISS,
        "aud": AUD,
        "exp": NOW + 600,
        "sub": "anna",
        "roles": ["operator", "fremde-idp-rolle"],
        "agent_id": "a1",
        "scope": "tasks:complete data:read unbekannt",
        "name": "Anna Beispiel",
    }
    base.update(overrides)
    return base


def _hs_backend(**kwargs: Any) -> JwtAuthBackend:
    return JwtAuthBackend(
        issuer=ISS, audience=AUD, hs256_secret=SECRET, now=lambda: NOW, **kwargs
    )


# --- HS256: claims, expiry, mapping ----------------------------------------


def test_valid_token_maps_claims_to_a_bound_principal() -> None:
    principal = _hs_backend().authenticate("Bearer " + _hs256(_claims()))
    assert principal.subject == "anna"
    assert principal.agent_id == "a1" and principal.is_bound
    assert principal.roles == frozenset({"operator"})  # unknown roles ignored
    assert principal.scopes == frozenset({"tasks:complete", "data:read"})
    assert principal.display_name == "Anna Beispiel"


def test_expiry_issuer_audience_and_signature_are_enforced() -> None:
    backend = _hs_backend()
    for broken in (
        _hs256(_claims(exp=NOW - 120)),  # expired (beyond leeway)
        _hs256({k: v for k, v in _claims().items() if k != "exp"}),  # no expiry
        _hs256(_claims(nbf=NOW + 600)),  # not yet valid
        _hs256(_claims(iss="https://anderer")),
        _hs256(_claims(aud="anderes-publikum")),
        _hs256(_claims(), secret="wrong-secret"),
    ):
        with pytest.raises(AuthError):
            backend.authenticate("Bearer " + broken)
    # Audience may be a list -- ours just has to be in it.
    ok = _hs256(_claims(aud=["x", AUD]))
    assert backend.authenticate("Bearer " + ok).subject == "anna"


def test_algorithm_is_pinned_by_configuration_not_by_the_token() -> None:
    backend = _hs_backend()
    with pytest.raises(AuthError):  # alg none never passes
        backend.authenticate("Bearer " + _hs256(_claims(), header={"alg": "none"}))
    with pytest.raises(AuthError):  # RS256 token against the HS256 path
        backend.authenticate("Bearer " + _hs256(_claims(), header={"alg": "RS256"}))
    with pytest.raises(ValueError):  # both paths configured -> refused at build
        JwtAuthBackend(issuer=ISS, audience=AUD, hs256_secret=SECRET, jwks_url="https://x")
    with pytest.raises(ValueError):  # neither path -> refused as well
        JwtAuthBackend(issuer=ISS, audience=AUD)


def test_dotted_roles_claim_supports_keycloak_shape() -> None:
    backend = _hs_backend(roles_claim="realm_access.roles")
    token = _hs256(_claims(realm_access={"roles": ["modeler"]}))
    assert backend.authenticate("Bearer " + token).roles == frozenset({"modeler"})


def test_missing_or_malformed_tokens_are_rejected() -> None:
    backend = _hs_backend()
    for bad in (None, "Basic abc", "Bearer nicht.ein.jwt", "Bearer x"):
        with pytest.raises(AuthError):
            backend.authenticate(bad)


# --- RS256 / JWKS (skips without the optional cryptography package) --------


def _rs_setup() -> tuple[Any, dict[str, Any]]:
    crypto = pytest.importorskip("cryptography")  # noqa: F841 -- skip gate
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()

    def be(i: int) -> str:
        return _b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))

    jwk = {"kty": "RSA", "kid": "k1", "use": "sig", "n": be(numbers.n), "e": be(numbers.e)}
    return key, {"keys": [jwk]}


def _rs256(payload: dict[str, Any], key: Any, kid: str = "k1") -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode())
    body = _b64(json.dumps(payload).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


def test_rs256_validates_against_the_jwks() -> None:
    key, jwks = _rs_setup()
    fetches: list[str] = []

    def fetcher(url: str) -> dict[str, Any]:
        fetches.append(url)
        return jwks

    backend = JwtAuthBackend(
        issuer=ISS, audience=AUD, jwks_url="https://idp/jwks", fetcher=fetcher,
        now=lambda: NOW,
    )
    principal = backend.authenticate("Bearer " + _rs256(_claims(), key))
    assert principal.subject == "anna" and principal.roles == frozenset({"operator"})
    # The JWKS is cached: a second token does not refetch.
    backend.authenticate("Bearer " + _rs256(_claims(), key))
    assert len(fetches) == 1

    # Tampered payload -> signature failure.
    head, body, sig = _rs256(_claims(), key).split(".")
    forged_body = _b64(json.dumps(_claims(sub="boese")).encode())
    with pytest.raises(AuthError):
        backend.authenticate(f"Bearer {head}.{forged_body}.{sig}")

    # Unknown kid refreshes once, then fails closed.
    with pytest.raises(AuthError):
        backend.authenticate("Bearer " + _rs256(_claims(), key, kid="anders"))
    assert len(fetches) == 2


def test_rs256_fetch_failure_fails_closed() -> None:
    key, _jwks = _rs_setup()

    def broken(_url: str) -> dict[str, Any]:
        raise OSError("idp unreachable")

    backend = JwtAuthBackend(
        issuer=ISS, audience=AUD, jwks_url="https://idp/jwks", fetcher=broken,
        now=lambda: NOW,
    )
    with pytest.raises(AuthError):
        backend.authenticate("Bearer " + _rs256(_claims(), key))


# --- boundary wiring --------------------------------------------------------


@contextmanager
def _installed(backend: JwtAuthBackend):  # type: ignore[no-untyped-def]
    original = api_module._auth_backend
    api_module._auth_backend = backend
    try:
        yield
    finally:
        api_module._auth_backend = original


def test_api_uses_the_jwt_identity() -> None:
    with _installed(_hs_backend()):
        me = client.get(
            "/auth/me", headers={"Authorization": "Bearer " + _hs256(_claims())}
        )
        assert me.status_code == 200
        body = me.json()
        assert body["subject"] == "anna" and body["agent_id"] == "a1"
        assert client.get("/auth/me").status_code == 401  # no token -> 401
        # A bound non-supervisor sees only their own worklist (same guard rail
        # as token/password identities -- the backend is swappable, not the rules).
        resp = client.get(
            "/agents/jemand-anderes/tasks",
            headers={"Authorization": "Bearer " + _hs256(_claims())},
        )
        assert resp.status_code == 403


def test_create_auth_backend_selects_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    from procworks.auth import create_auth_backend

    monkeypatch.setenv("PROCWORKS_AUTH", "jwt")
    monkeypatch.setenv("PROCWORKS_JWT_ISSUER", ISS)
    monkeypatch.setenv("PROCWORKS_JWT_AUDIENCE", AUD)
    monkeypatch.setenv("PROCWORKS_JWT_HS256_SECRET", SECRET)
    backend = create_auth_backend()
    assert isinstance(backend, JwtAuthBackend)

    monkeypatch.delenv("PROCWORKS_JWT_HS256_SECRET")
    with pytest.raises(ValueError):  # neither JWKS nor secret
        create_auth_backend()


def test_auth_config_reports_jwt_and_optional_oidc_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/auth/config meldet den JWT-Modus und -- nur mit Opt-in -- die
    OIDC-Redirect-Endpunkte (Auth-Konzept §12.4).

    Ohne die drei Variablen bleibt das Token-Feld der dokumentierte Weg
    (alle oidc_*-Felder null); mit ihnen bietet die SPA den Firmen-Login
    (Authorization Code + PKCE) an. Kein Geheimnis: ein öffentlicher
    SPA-Client nutzt PKCE statt Client-Secret.
    """

    with _installed(_hs_backend()):
        cfg = client.get("/auth/config").json()
        assert cfg["mode"] == "jwt"
        assert cfg["password_login"] is False
        assert cfg["oidc_authorize_url"] is None
        assert cfg["oidc_token_url"] is None
        assert cfg["oidc_client_id"] is None

        monkeypatch.setenv(
            "PROCWORKS_JWT_AUTHORIZE_URL", "https://idp.example.org/authorize"
        )
        monkeypatch.setenv("PROCWORKS_JWT_TOKEN_URL", "https://idp.example.org/token")
        monkeypatch.setenv("PROCWORKS_JWT_CLIENT_ID", "procworks-spa")
        cfg = client.get("/auth/config").json()
        assert cfg["oidc_authorize_url"] == "https://idp.example.org/authorize"
        assert cfg["oidc_token_url"] == "https://idp.example.org/token"
        assert cfg["oidc_client_id"] == "procworks-spa"
        assert cfg["oidc_scopes"] == "openid profile email"

        monkeypatch.setenv("PROCWORKS_JWT_OIDC_SCOPES", "openid procworks")
        assert client.get("/auth/config").json()["oidc_scopes"] == "openid procworks"

        # Unvollstaendige Konfiguration (Token-Endpunkt fehlt) -> kein Angebot.
        monkeypatch.delenv("PROCWORKS_JWT_TOKEN_URL")
        cfg = client.get("/auth/config").json()
        assert cfg["oidc_authorize_url"] is None
        assert cfg["oidc_client_id"] is None

    # Ausserhalb des JWT-Modus nie -- auch mit gesetzten Variablen.
    monkeypatch.setenv("PROCWORKS_JWT_TOKEN_URL", "https://idp.example.org/token")
    cfg = client.get("/auth/config").json()
    assert cfg["mode"] != "jwt"
    assert cfg["oidc_authorize_url"] is None
