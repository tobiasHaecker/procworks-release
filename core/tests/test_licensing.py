# SPDX-License-Identifier: BUSL-1.1
"""Tests for the (dormant-by-default) licensing/metering layer.

Covers signature verification, the offline time ratchet, slot accounting and
coverage, the enforcement guards, the "default open" no-op behaviour, the
SQLAlchemy store round-trip, the audit hash chain and the API endpoints.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)
from fastapi.testclient import TestClient

from procworks.audit import (
    NON_PROCESS_EVENTS,
    EventType,
    InMemoryAuditLog,
    chain_hash,
    compute_kpis,
)
from procworks.licensing import (
    InMemoryLicenseStore,
    License,
    LicenseError,
    LicenseKind,
    LicenseManager,
    PendingClaim,
    _canonical_payload,
    verify_license,
)

# --------------------------------------------------------------------------
# helpers: a licensor keypair + a signing helper (licensor-side, test-only)
# --------------------------------------------------------------------------


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    pub_pem = (
        priv.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub_pem


def _sign_pack(
    priv: Ed25519PrivateKey,
    *,
    license_id: str = "pack1",
    slots: int = 5,
    install_id: str = "",
    issued_at: float = 1000.0,
    expires_at: float | None = 2000.0,
) -> License:
    lic = License(
        license_id=license_id,
        kind=LicenseKind.PACK,
        slots=slots,
        customer_id="cust1",
        install_id=install_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    signature = priv.sign(_canonical_payload(lic))
    return lic.model_copy(update={"signature": base64.b64encode(signature).decode()})


def _token(lic: License) -> str:
    return base64.b64encode(lic.model_dump_json().encode()).decode()


class FakeClock:
    """A mutable system clock for the ratchet tests."""

    def __init__(self, start: float) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


# --------------------------------------------------------------------------
# signature verification
# --------------------------------------------------------------------------


def test_free_license_is_always_valid_without_a_key() -> None:
    free = License(license_id="free", kind=LicenseKind.FREE, slots=3)
    assert verify_license(free, None) is True


def test_signed_pack_verifies_and_tampering_is_detected() -> None:
    priv, pub = _keypair()
    pack = _sign_pack(priv)
    assert verify_license(pack, pub) is True

    tampered = pack.model_copy(update={"slots": 500})
    assert verify_license(tampered, pub) is False

    later_expiry = pack.model_copy(update={"expires_at": 9_999_999.0})
    assert verify_license(later_expiry, pub) is False


def test_pack_without_key_is_invalid() -> None:
    priv, _pub = _keypair()
    pack = _sign_pack(priv)
    # No configured public key -> a pack can never be treated as authentic.
    assert verify_license(pack, None) is False


# --------------------------------------------------------------------------
# time ratchet
# --------------------------------------------------------------------------


def test_ratchet_never_runs_backwards_on_clock_rollback() -> None:
    store = InMemoryLicenseStore()
    clock = FakeClock(1000.0)
    mgr = LicenseManager(store, pubkey_pem="x", now=clock)

    assert mgr.now() == 1000.0
    clock.value = 500.0  # operator turns the system clock back
    assert mgr.now() == 1000.0  # effective time holds at the high-water-mark

    clock.value = 1500.0  # real time moves forward again
    assert mgr.now() == 1500.0


def test_trusted_anchor_sticks_and_dominates() -> None:
    store = InMemoryLicenseStore()
    clock = FakeClock(1000.0)
    mgr = LicenseManager(store, pubkey_pem="x", now=clock)

    mgr.refresh_time(5000.0)  # signed lower bound from an online contact
    clock.value = 10.0
    assert mgr.now() == 5000.0
    anchor = store.get_time_anchor()
    assert anchor is not None and anchor.trusted is True


def test_ratchet_is_fed_by_external_monotone_source() -> None:
    store = InMemoryLicenseStore()
    clock = FakeClock(1000.0)
    mgr = LicenseManager(store, pubkey_pem="x", now=clock, time_sources=[lambda: 8000.0])
    # The external source (e.g. the newest audit timestamp) pulls the effective
    # time forward even though the system clock is behind.
    assert mgr.now() == 8000.0


# --------------------------------------------------------------------------
# slots / coverage / guards
# --------------------------------------------------------------------------


def test_free_plus_pack_slots_and_expiry() -> None:
    store = InMemoryLicenseStore()
    priv, pub = _keypair()
    clock = FakeClock(1000.0)
    mgr = LicenseManager(store, pubkey_pem=pub, free_slots=3, now=clock)

    assert mgr.total_slots() == 3  # free only

    pack = _sign_pack(priv, install_id=store.install_id(), expires_at=1500.0)
    store.put_license(pack)
    assert mgr.total_slots() == 8  # 3 free + 5 pack

    clock.value = 2000.0  # past expiry, no grace
    assert mgr.total_slots() == 3  # pack no longer contributes


def test_pack_bound_to_other_install_is_ignored() -> None:
    store = InMemoryLicenseStore()
    priv, pub = _keypair()
    mgr = LicenseManager(store, pubkey_pem=pub, now=FakeClock(1000.0))
    pack = _sign_pack(priv, install_id="someone-elses-install")
    store.put_license(pack)
    assert mgr.total_slots() == 3  # foreign pack does not count here


def test_assert_agents_licensed_blocks_expired_agent() -> None:
    store = InMemoryLicenseStore()
    priv, pub = _keypair()
    clock = FakeClock(1000.0)
    mgr = LicenseManager(store, pubkey_pem=pub, now=clock)
    pack = _sign_pack(priv, install_id=store.install_id(), expires_at=1500.0)
    store.put_license(pack)
    store.bind("agent_a", "pack1")

    mgr.assert_agents_licensed(["agent_a"])  # covered -> no raise

    clock.value = 2000.0  # pack expired
    with pytest.raises(LicenseError) as exc:
        mgr.assert_agents_licensed(["agent_a"])
    assert exc.value.status == 402


def test_reconcile_and_auto_bind_respect_quota() -> None:
    store = InMemoryLicenseStore()
    _priv, pub = _keypair()
    mgr = LicenseManager(store, pubkey_pem=pub, free_slots=3, now=FakeClock(1000.0))

    mgr.reconcile({"a1", "a2"})  # two pre-existing agents auto-bind to free slots
    assert mgr.summary({"a1", "a2"}).used_slots == 2

    assert mgr.can_create_agent({"a1", "a2"}) is True
    mgr.auto_bind_new_agent("a3", {"a1", "a2"})  # fills the last free slot
    assert mgr.can_create_agent({"a1", "a2", "a3"}) is False
    with pytest.raises(LicenseError) as exc:
        mgr.auto_bind_new_agent("a4", {"a1", "a2", "a3"})
    assert exc.value.status == 402


def test_activate_rejects_invalid_and_foreign_tokens() -> None:
    store = InMemoryLicenseStore()
    priv, pub = _keypair()
    mgr = LicenseManager(store, pubkey_pem=pub, now=FakeClock(1000.0))

    with pytest.raises(LicenseError):
        mgr.activate("not-a-token")

    foreign = _sign_pack(priv, install_id="other-install")
    with pytest.raises(LicenseError) as exc:
        mgr.activate(_token(foreign))
    assert exc.value.status == 422

    good = _sign_pack(priv, install_id=store.install_id())
    installed = mgr.activate(_token(good))
    assert installed.license_id == "pack1"
    assert mgr.total_slots() == 8


# --------------------------------------------------------------------------
# default open: the whole layer is inert without a licensor key
# --------------------------------------------------------------------------


def test_default_open_is_a_noop() -> None:
    store = InMemoryLicenseStore()
    mgr = LicenseManager(store, pubkey_pem=None)

    assert mgr.enforced is False
    assert mgr.is_agent_licensed("whoever") is True
    assert mgr.can_create_agent({f"a{i}" for i in range(100)}) is True
    mgr.assert_agents_licensed(["x", "y", "z"])  # no raise
    mgr.reconcile({"a1", "a2"})
    mgr.auto_bind_new_agent("a3", {"a1", "a2"})
    # No bindings are ever written while enforcement is off.
    assert store.list_bindings() == []


# --------------------------------------------------------------------------
# SQLAlchemy store round-trip
# --------------------------------------------------------------------------


def test_sqlalchemy_license_store_roundtrip(tmp_path: Path) -> None:
    from procworks.db import SqlAlchemyLicenseStore
    from procworks.licensing import TimeAnchor

    url = f"sqlite:///{tmp_path / 'license.db'}"
    store = SqlAlchemyLicenseStore(url, create_tables=True)

    install = store.install_id()
    assert store.install_id() == install  # stable across reads

    lic = License(
        license_id="pack1", kind=LicenseKind.PACK, slots=5, expires_at=2000.0
    )
    store.put_license(lic)
    assert store.get_license("pack1") is not None
    assert [x.license_id for x in store.list_licenses()] == ["pack1"]

    store.bind("agent_a", "pack1")
    assert store.list_bindings()[0].agent_id == "agent_a"
    store.unbind("agent_a")
    assert store.list_bindings() == []

    store.put_time_anchor(TimeAnchor(high_water_mark=1234.0, trusted=True))
    anchor = store.get_time_anchor()
    assert anchor is not None and anchor.high_water_mark == 1234.0

    store.clear()
    assert store.list_licenses() == []
    assert store.get_time_anchor() is None
    assert store.install_id() == install  # install id survives a data reset


# --------------------------------------------------------------------------
# audit hash chain
# --------------------------------------------------------------------------


def test_audit_hash_chain_links_and_head() -> None:
    log = InMemoryAuditLog()
    first = log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    second = log.append(EventType.ACTIVITY_COMPLETED, "i1", "s1", node_id="a")

    assert first.prev_hash == ""
    assert first.entry_hash != ""
    assert second.prev_hash == first.entry_hash
    assert log.head_hash() == second.entry_hash


def test_audit_chain_detects_tampering() -> None:
    log = InMemoryAuditLog()
    log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    log.append(EventType.ACTIVITY_COMPLETED, "i1", "s1", node_id="a")
    events = log.list_all()

    # Recomputing the chain from scratch reproduces the stored hashes...
    prev = ""
    for ev in events:
        expected = chain_hash(
            prev,
            seq=ev.seq,
            timestamp=ev.timestamp,
            event_type=ev.event_type,
            instance_id=ev.instance_id,
            schema_id=ev.schema_id,
            schema_version=ev.schema_version,
            node_id=ev.node_id,
            label=ev.label,
            agent_id=ev.agent_id,
            detail=ev.detail,
        )
        assert expected == ev.entry_hash
        prev = ev.entry_hash

    # ...but altering a past entry's payload no longer matches its hash.
    altered = events[0].model_copy(update={"instance_id": "hacked"})
    recomputed = chain_hash(
        altered.prev_hash,
        seq=altered.seq,
        timestamp=altered.timestamp,
        event_type=altered.event_type,
        instance_id=altered.instance_id,
        schema_id=altered.schema_id,
        schema_version=altered.schema_version,
        node_id=altered.node_id,
        label=altered.label,
        agent_id=altered.agent_id,
        detail=altered.detail,
    )
    assert recomputed != altered.entry_hash


def test_time_anchor_events_excluded_from_kpis() -> None:
    log = InMemoryAuditLog()
    log.append(EventType.INSTANCE_CREATED, "i1", "s1")
    log.append(EventType.TIME_ANCHOR, "__license__", "__license__", detail={"hwm": "1"})

    assert EventType.TIME_ANCHOR in NON_PROCESS_EVENTS
    report = compute_kpis(log.list_all())
    assert report.total_instances == 1  # the anchor is not a phantom instance
    assert log.max_event_time() > 0.0


# --------------------------------------------------------------------------
# API: default-open behaviour + invalid activation
# --------------------------------------------------------------------------


def test_api_license_status_open_mode() -> None:
    from procworks.api import app

    client = TestClient(app)
    client.post("/admin/reset", json={"load_demo": False})
    status = client.get("/license/status").json()
    assert status["enforced"] is False
    assert status["total_slots"] == 3
    assert status["install_id"]
    # The agent list endpoint answers even in open mode.
    assert client.get("/license/agents").status_code == 200


def test_api_activate_invalid_token_is_422() -> None:
    from procworks.api import app

    client = TestClient(app)
    resp = client.post("/license/activate", json={"token": "garbage"})
    assert resp.status_code == 422


def test_api_checkout_without_backend_reports_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from procworks.api import app

    monkeypatch.delenv("PROCWORKS_LICENSE_CHECKOUT_URL", raising=False)
    client = TestClient(app)
    body = client.post("/license/checkout", json={}).json()
    assert body["checkout_url"] is None
    assert body["install_id"]


def _sign_for_install(priv: Ed25519PrivateKey, install_id: str) -> str:
    return _token(_sign_pack(priv, install_id=install_id))


def test_api_enforced_activate_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """With an enforced manager swapped in, a signed pack activates via the API."""

    import procworks.api as api

    priv, pub = _keypair()
    store = InMemoryLicenseStore()
    enforced = LicenseManager(store, pubkey_pem=pub, now=FakeClock(1000.0))
    monkeypatch.setattr(api, "_license", enforced)

    client = TestClient(api.app)
    install_id = client.get("/license/status").json()["install_id"]
    assert install_id == store.install_id()

    token = _sign_for_install(priv, install_id)
    resp = client.post("/license/activate", json={"token": token})
    assert resp.status_code == 200
    assert client.get("/license/status").json()["total_slots"] == 8


def test_api_enforced_agent_quota_blocks_with_402(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding an agent past the covered contingent returns 402 (purchase offer)."""

    import procworks.api as api

    _priv, pub = _keypair()
    store = InMemoryLicenseStore()
    enforced = LicenseManager(store, pubkey_pem=pub, free_slots=1, now=FakeClock(1000.0))
    monkeypatch.setattr(api, "_license", enforced)

    client = TestClient(api.app)
    client.post("/admin/reset", json={"load_demo": False})  # start from empty org
    oid = client.post("/org-models", json={"name": "Acme"}).json()["id"]

    first = client.post(f"/org-models/{oid}/agents", json={"name": "A1"})
    assert first.status_code == 200  # fits the single free slot
    second = client.post(f"/org-models/{oid}/agents", json={"name": "A2"})
    assert second.status_code == 402  # over quota -> purchase offer

    # The first agent is auto-bound and shows up as licensed on the agent page.
    views = {v["agent_id"]: v for v in client.get("/license/agents").json()}
    assert any(v["licensed"] for v in views.values())


# --------------------------------------------------------------------------
# online auto-pull (§4: claim + poll + best-effort auto-activate)
# --------------------------------------------------------------------------


def test_new_claim_persists_and_prunes_on_expiry() -> None:
    store = InMemoryLicenseStore()
    clock = FakeClock(1000.0)
    mgr = LicenseManager(
        store, pubkey_pem="x", claim_ttl_seconds=1800, now=clock
    )

    claim = mgr.new_claim("https://claim.example/claim", slots=5, months=12)
    assert claim.poll_url == f"https://claim.example/claim/{claim.claim_token}"
    assert claim.expires_at == 1000.0 + 1800
    assert len(mgr.pending_claims()) == 1

    clock.value = 1000.0 + 1801  # past the claim TTL
    assert mgr.pending_claims() == []  # pruned as a side effect
    assert store.list_claims() == []


def test_poll_claims_activates_issued_pack() -> None:
    priv, pub = _keypair()
    store = InMemoryLicenseStore()
    mgr = LicenseManager(store, pubkey_pem=pub, now=FakeClock(1000.0))
    claim = mgr.new_claim("https://claim.example/claim", slots=5, months=12)

    token = _token(_sign_pack(priv, install_id=store.install_id(), expires_at=2000.0))
    seen: list[str] = []

    def fetcher(url: str) -> str | None:
        seen.append(url)
        return token

    activated = mgr.poll_claims(fetcher)
    assert seen == [claim.poll_url]
    assert len(activated) == 1
    assert mgr.total_slots() == 3 + 5  # free + pack
    assert store.list_claims() == []  # claim retired after success


def test_poll_claims_keeps_pending_and_drops_invalid() -> None:
    _priv, pub = _keypair()
    store = InMemoryLicenseStore()
    mgr = LicenseManager(store, pubkey_pem=pub, now=FakeClock(1000.0))
    mgr.new_claim("https://claim.example/claim", slots=5, months=12)

    # Still pending at the licensor -> the claim survives for the next pass.
    assert mgr.poll_claims(lambda _url: None) == []
    assert len(store.list_claims()) == 1

    # A bogus token cannot activate -> the claim is retired (no infinite retry).
    assert mgr.poll_claims(lambda _url: "not-a-real-token") == []
    assert store.list_claims() == []
    assert mgr.total_slots() == 3


def test_poll_claims_is_a_noop_when_open() -> None:
    store = InMemoryLicenseStore()
    mgr = LicenseManager(store, pubkey_pem=None, now=FakeClock(1000.0))
    # A stray claim can exist, but without enforcement polling never runs.
    store.put_claim(
        PendingClaim(
            claim_token="t", poll_url="https://x/claim/t", expires_at=9_999.0
        )
    )
    called = False

    def fetcher(_url: str) -> str | None:
        nonlocal called
        called = True
        return "whatever"

    assert mgr.poll_claims(fetcher) == []
    assert called is False


def test_sqlalchemy_claim_roundtrip(tmp_path: Path) -> None:
    from procworks.db import SqlAlchemyLicenseStore

    url = f"sqlite:///{tmp_path / 'claims.db'}"
    store = SqlAlchemyLicenseStore(url, create_tables=True)

    store.put_claim(
        PendingClaim(
            claim_token="tok1",
            poll_url="https://claim.example/claim/tok1",
            slots=5,
            months=12,
            created_at=1000.0,
            expires_at=2800.0,
        )
    )
    claims = store.list_claims()
    assert [c.claim_token for c in claims] == ["tok1"]
    assert claims[0].poll_url.endswith("/tok1")

    install = store.install_id()
    store.clear()
    assert store.list_claims() == []  # claims dropped on reset
    assert store.install_id() == install  # install id survives


def test_api_checkout_offers_claim_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import procworks.api as api

    _priv, pub = _keypair()
    store = InMemoryLicenseStore()
    enforced = LicenseManager(store, pubkey_pem=pub, now=FakeClock(1000.0))
    monkeypatch.setattr(api, "_license", enforced)
    monkeypatch.setenv("PROCWORKS_LICENSE_CHECKOUT_URL", "https://buy.example/checkout")
    monkeypatch.setenv("PROCWORKS_LICENSE_CLAIM_URL", "https://buy.example/claim")

    client = TestClient(api.app)
    body = client.post("/license/checkout", json={"slots": 5, "months": 12}).json()
    assert body["claim_token"]
    assert body["poll_url"] == f"https://buy.example/claim/{body['claim_token']}"
    # The claim token also travels on the checkout deep-link for the licensor.
    assert f"claim_token={body['claim_token']}" in body["checkout_url"]


def test_api_claims_poll_auto_activates(monkeypatch: pytest.MonkeyPatch) -> None:
    import procworks.api as api

    priv, pub = _keypair()
    store = InMemoryLicenseStore()
    enforced = LicenseManager(store, pubkey_pem=pub, now=FakeClock(1000.0))
    monkeypatch.setattr(api, "_license", enforced)

    enforced.new_claim("https://buy.example/claim", slots=5, months=12)
    token = _token(_sign_pack(priv, install_id=store.install_id(), expires_at=2000.0))
    monkeypatch.setattr(api, "_claim_fetcher", lambda _url: token)

    client = TestClient(api.app)
    result = client.post("/license/claims/poll", json={}).json()
    assert result["activated"] == 1
    assert result["pending"] == 0
    assert result["summary"]["total_slots"] == 3 + 5
