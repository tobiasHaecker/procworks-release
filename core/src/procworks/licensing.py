# SPDX-License-Identifier: BUSL-1.1
"""Licensing & agent metering (dormant by default, boundary-only).

This module implements the technical side of the licensing/business model
concept: a *free* agent quota plus purchasable, time-limited *agent packs*,
enforced through cryptographically signed license files with an offline-safe
expiry check. It is deliberately **additive and boundary-only** -- the pure
correctness core (``validator``/``execution``/``model``) is never touched.

Two design invariants make it safe to ship *now* while it only "bites" later:

* **Default open.** Without a configured licensor public key
  (``PROCWORKS_LICENSE_PUBKEY`` unset) :attr:`LicenseManager.enforced` is
  ``False``: every check short-circuits to "allowed", no bindings are written,
  and the whole feature is inert. Dev/test/quickstart stay green, exactly like
  ``PROCWORKS_AUTH=open``. Activation later is *only* setting the env var.
* **Soft, never destructive.** Enforcement only ever blocks *new* agents /
  *new* instances. Running instances and all data are never touched, mirroring
  the "fair barrier, not DRM" line of the concept (§5A.3, §5A.4).

The pieces:

``License``/``LicenseKind``
    A signed slot contingent (``FREE`` = perpetual free quota, ``PACK`` = a
    bought, expiring pack). :func:`verify_license` checks the Ed25519 signature.
``TimeAnchor``/:class:`Clock`
    A monotone, tamper-evidenced "time ratchet": ``effective_now`` never runs
    backwards even if the operator turns the system clock back (§5A.4). It is
    fed by the system clock, by monotone external lower bounds (the append-only,
    hash-chained audit log's newest timestamp) and -- occasionally, when online
    -- by a trusted signed anchor.
``LicenseStore``
    Swappable persistence (in-memory / SQLAlchemy) exactly like the other
    stores, holding licenses, agent bindings, the install id and the anchor.
``LicenseManager``
    The boundary logic: slot accounting, coverage checks, activation and the
    enforcement guards the API calls.

The **private** signing key never lives here or in any shipped artefact; the
product only ever *verifies*. Issuing signed packs is a separate licensor-side
tool.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
import uuid
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

#: Free tier size: how many agents may exist without any bought pack. The
#: concept fixes this at three (enough to exercise all demo data, §5A.1).
FREE_SLOTS_DEFAULT = 3

#: Reserved id used for the synthetic ``TIME_ANCHOR`` audit events that embed
#: the time ratchet into the hash-chained log. Kept out of KPI/mining grouping.
LICENSE_PSEUDO_ID = "__license__"


# --------------------------------------------------------------------------
# Data model (additive; kept out of the pure ``model.py`` meta-model)
# --------------------------------------------------------------------------


class LicenseKind(StrEnum):
    """The kind of a slot contingent."""

    FREE = "FREE"  # perpetual free quota, unsigned, no expiry
    PACK = "PACK"  # bought, time-limited, signed pack


class License(BaseModel):
    """A single slot contingent; ``signature`` covers all other fields.

    ``FREE`` licenses are unsigned and never expire. ``PACK`` licenses are
    Ed25519-signed by the licensor, carry an ``expires_at`` and are bound to a
    specific installation via ``install_id`` so a copied license file does not
    work elsewhere (§5A.4, baseline 5).
    """

    license_id: str
    kind: LicenseKind
    slots: int
    customer_id: str = ""  # pseudonymous; empty for FREE
    install_id: str = ""  # installation binding; empty for FREE
    issued_at: float = 0.0  # epoch seconds
    expires_at: float | None = None  # None = perpetual (FREE)
    features: list[str] = Field(default_factory=list)
    signature: str = ""  # base64(Ed25519); required for PACK only


class AgentBinding(BaseModel):
    """Explicit assignment of one agent to the license contingent covering it."""

    agent_id: str
    license_id: str


class TimeAnchor(BaseModel):
    """The monotone, tamper-evidenced time high-water-mark ("ratchet")."""

    high_water_mark: float = 0.0  # greatest trustworthy time ever seen
    trusted: bool = False  # True once set from a signed/authoritative source
    updated_at: float = 0.0
    #: Audit head hash observed when the anchor was last written into the chain;
    #: ties the anchor to a position in the hash-chained log (empty if never).
    audit_head: str = ""


class SlotSummary(BaseModel):
    """Aggregate contingent view for the agent page's quota bar."""

    enforced: bool
    total_slots: int  # 3 (free) + Σ active packs
    used_slots: int  # agents currently covered by an active slot
    free_slots: int  # total_slots - used_slots (never negative)
    packs_active: int
    next_expiry_at: float | None  # earliest expiry of an active pack
    days_to_next_expiry: int | None
    install_id: str


class AgentLicenseView(BaseModel):
    """Per-agent licensing status shown as a badge on the agent page."""

    agent_id: str
    license_id: str | None  # None = uncovered (over quota)
    kind: LicenseKind | None
    licensed: bool  # covered by a valid, non-expired slot
    expires_at: float | None
    days_left: int | None


class PendingClaim(BaseModel):
    """An open online purchase awaiting fulfilment ("auto-pull", §4).

    After a checkout is started the product remembers a short-lived, single-use
    *claim* so it can later fetch the freshly signed pack from the (separate,
    hosted) licensor service and activate it automatically, sparing the operator
    the copy-&-paste step. It is purely operational, best-effort state: losing it
    (process restart, expiry) only falls back to the manual copy-&-paste flow --
    the token also remains available in the customer portal.
    """

    claim_token: str  # high-entropy, single-use handle shared with the licensor
    poll_url: str  # where the instance polls for the issued token
    slots: int = 0  # requested pack size (informational)
    months: int = 0  # requested duration (informational)
    created_at: float = 0.0
    expires_at: float = 0.0  # after this the claim is dropped unfulfilled


# --------------------------------------------------------------------------
# Signature verification (verify-only in the product)
# --------------------------------------------------------------------------


class SignatureError(ValueError):
    """Raised when a license token cannot be parsed or verified."""


def _canonical_payload(lic: License) -> bytes:
    """Deterministic bytes over all license fields except ``signature``.

    Both the licensor's signing tool and this verifier must serialise the exact
    same bytes, so the encoding is pinned: JSON in ``mode="json"`` with sorted
    keys and no whitespace.
    """

    data = lic.model_dump(mode="json", exclude={"signature"})
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def verify_license(lic: License, pubkey_pem: str | None) -> bool:
    """Return whether ``lic`` is authentic.

    ``FREE`` licenses are unsigned and always valid. A ``PACK`` is valid only
    when its Ed25519 signature verifies against ``pubkey_pem``. If the public
    key is missing or the optional ``cryptography`` dependency is not installed,
    a ``PACK`` is treated as **invalid** -- so no pack can ever be "invented"
    without the licensor's real key.
    """

    if lic.kind is LicenseKind.FREE:
        return True
    if not pubkey_pem or not lic.signature:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            load_pem_public_key,
        )
    except ModuleNotFoundError:
        return False
    try:
        key = load_pem_public_key(pubkey_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            return False  # only Ed25519 licensor keys are accepted
        key.verify(base64.b64decode(lic.signature), _canonical_payload(lic))
    except InvalidSignature:
        return False
    except Exception:
        # Malformed key/signature/base64 -> treat as invalid, never crash.
        return False
    return True


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class LicenseStore(Protocol):
    """Swappable persistence for licenses, bindings, install id and anchor.

    An operational store like the mail outbox or absence store: it carries no
    correctness logic. In-memory by default; SQLAlchemy when ``DATABASE_URL``
    is set (see :func:`create_license_store`).
    """

    def list_licenses(self) -> list[License]: ...

    def put_license(self, lic: License) -> None: ...

    def get_license(self, license_id: str) -> License | None: ...

    def remove_license(self, license_id: str) -> None: ...

    def list_bindings(self) -> list[AgentBinding]: ...

    def bind(self, agent_id: str, license_id: str) -> None: ...

    def unbind(self, agent_id: str) -> None: ...

    def get_time_anchor(self) -> TimeAnchor | None: ...

    def put_time_anchor(self, anchor: TimeAnchor) -> None: ...

    def list_claims(self) -> list[PendingClaim]: ...

    def put_claim(self, claim: PendingClaim) -> None: ...

    def remove_claim(self, claim_token: str) -> None: ...

    def install_id(self) -> str: ...

    def clear(self) -> None: ...


class InMemoryLicenseStore:
    """A dict-backed license store; the default without configuration.

    The install id is generated lazily on first read and then kept stable for
    the process lifetime (a durable backend persists it instead).
    """

    def __init__(self) -> None:
        self._licenses: dict[str, License] = {}
        self._bindings: dict[str, str] = {}  # agent_id -> license_id
        self._anchor: TimeAnchor | None = None
        self._claims: dict[str, PendingClaim] = {}  # claim_token -> claim
        self._install_id: str | None = None

    def list_licenses(self) -> list[License]:
        return list(self._licenses.values())

    def put_license(self, lic: License) -> None:
        self._licenses[lic.license_id] = lic

    def get_license(self, license_id: str) -> License | None:
        return self._licenses.get(license_id)

    def remove_license(self, license_id: str) -> None:
        self._licenses.pop(license_id, None)

    def list_bindings(self) -> list[AgentBinding]:
        return [
            AgentBinding(agent_id=agent_id, license_id=license_id)
            for agent_id, license_id in self._bindings.items()
        ]

    def bind(self, agent_id: str, license_id: str) -> None:
        self._bindings[agent_id] = license_id

    def unbind(self, agent_id: str) -> None:
        self._bindings.pop(agent_id, None)

    def get_time_anchor(self) -> TimeAnchor | None:
        return self._anchor

    def put_time_anchor(self, anchor: TimeAnchor) -> None:
        self._anchor = anchor

    def list_claims(self) -> list[PendingClaim]:
        return list(self._claims.values())

    def put_claim(self, claim: PendingClaim) -> None:
        self._claims[claim.claim_token] = claim

    def remove_claim(self, claim_token: str) -> None:
        self._claims.pop(claim_token, None)

    def install_id(self) -> str:
        if self._install_id is None:
            self._install_id = uuid.uuid4().hex
        return self._install_id

    def clear(self) -> None:
        self._licenses.clear()
        self._bindings.clear()
        self._anchor = None
        self._claims.clear()
        # The install id is intentionally *not* reset: it identifies the
        # installation, not the data, and packs are bound to it.


def create_license_store() -> LicenseStore:
    """Build the license store from the environment (mirrors the other stores).

    ``DATABASE_URL`` -> durable SQLAlchemy store, otherwise in-memory. The
    SQLAlchemy import stays lazy so the default path has no import cost.
    """

    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyLicenseStore

        return SqlAlchemyLicenseStore(url, create_tables=True)
    return InMemoryLicenseStore()


# --------------------------------------------------------------------------
# Time ratchet
# --------------------------------------------------------------------------


class Clock:
    """The offline-safe time ratchet: ``effective_now`` never runs backwards.

    ``effective_now`` is the maximum of the system clock, the persisted
    high-water-mark and every injected monotone lower bound (e.g. the newest
    timestamp of the append-only, hash-chained audit log). Whenever the system
    clock is *ahead* of the stored mark the ratchet is advanced forward, so a
    later clock rollback cannot lower the effective time.

    Parameters
    ----------
    store:
        Where the :class:`TimeAnchor` is persisted.
    time_sources:
        Callables returning monotone external lower bounds (epoch seconds).
    now:
        System clock, injectable for tests.
    """

    def __init__(
        self,
        store: LicenseStore,
        *,
        time_sources: Iterable[Callable[[], float]] = (),
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._sources = list(time_sources)
        self._now = now

    def effective_now(self) -> float:
        anchor = self._store.get_time_anchor()
        hwm = anchor.high_water_mark if anchor else 0.0
        candidate = max([self._now(), hwm, *(src() for src in self._sources)])
        if candidate > hwm:
            self.advance(candidate, trusted=False)
        return candidate

    def advance(self, ts: float, *, trusted: bool, audit_head: str = "") -> None:
        """Move the high-water-mark forward (never backward).

        The ``trusted`` flag "sticks" once set from a signed/authoritative
        source, and ``audit_head`` records the log position that witnessed a
        trusted advance (tamper evidence).
        """

        anchor = self._store.get_time_anchor()
        cur = anchor.high_water_mark if anchor else 0.0
        was_trusted = anchor.trusted if anchor else False
        head = audit_head or (anchor.audit_head if anchor else "")
        if ts > cur or (trusted and not was_trusted):
            self._store.put_time_anchor(
                TimeAnchor(
                    high_water_mark=max(ts, cur),
                    trusted=was_trusted or trusted,
                    updated_at=self._now(),
                    audit_head=head,
                )
            )


# --------------------------------------------------------------------------
# Manager (boundary logic)
# --------------------------------------------------------------------------


class LicenseError(Exception):
    """A licensing block; carries the HTTP status the API should surface.

    Blocking a purchase-gated action uses ``402 Payment Required`` (a purchase
    offer, not an error); an invalid activation uses ``422``.
    """

    def __init__(self, message: str, *, status: int = 402) -> None:
        super().__init__(message)
        self.status = status


class LicenseManager:
    """Slot accounting, coverage checks, activation and enforcement guards.

    The manager is decoupled from the org store: methods that need to know the
    universe of existing agents take an explicit ``all_agent_ids`` set, and the
    caller (the API boundary) supplies it. This keeps the manager free of core
    imports.

    Enforcement is gated on :attr:`enforced` (a configured licensor public key).
    When it is off, every guard is a no-op and *no* state is mutated.
    """

    def __init__(
        self,
        store: LicenseStore,
        *,
        pubkey_pem: str | None = None,
        free_slots: int = FREE_SLOTS_DEFAULT,
        grace_days: int = 0,
        claim_ttl_seconds: int = 1800,
        time_sources: Iterable[Callable[[], float]] = (),
        anchor_writer: Callable[[float, bool], str] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._pubkey = pubkey_pem or None
        self._free = free_slots
        self._grace_s = grace_days * 86400
        self._claim_ttl = claim_ttl_seconds
        self._anchor_writer = anchor_writer
        self._clock = Clock(store, time_sources=time_sources, now=now)

    # ---- configuration -----------------------------------------------------

    @property
    def enforced(self) -> bool:
        """Whether licensing bites (a licensor public key is configured)."""

        return bool(self._pubkey)

    def install_id(self) -> str:
        return self._store.install_id()

    # ---- time / validity ---------------------------------------------------

    def now(self) -> float:
        return self._clock.effective_now()

    def _pack_active(self, lic: License) -> bool:
        """Whether a license currently contributes slots (valid + unexpired)."""

        if lic.kind is LicenseKind.FREE:
            return True
        if not verify_license(lic, self._pubkey):
            return False
        if lic.install_id and lic.install_id != self._store.install_id():
            return False
        if lic.expires_at is None:
            return True
        return self.now() <= lic.expires_at + self._grace_s

    def _ensure_free_license(self) -> License:
        """Return the FREE license, materialising it on first need."""

        for lic in self._store.list_licenses():
            if lic.kind is LicenseKind.FREE:
                if lic.slots != self._free:
                    lic = lic.model_copy(update={"slots": self._free})
                    self._store.put_license(lic)
                return lic
        free = License(license_id="free", kind=LicenseKind.FREE, slots=self._free)
        self._store.put_license(free)
        return free

    def active_licenses(self) -> list[License]:
        self._ensure_free_license()
        return [lic for lic in self._store.list_licenses() if self._pack_active(lic)]

    def total_slots(self) -> int:
        return sum(lic.slots for lic in self.active_licenses())

    # ---- slots / coverage --------------------------------------------------

    def _binding_map(self) -> dict[str, str]:
        return {b.agent_id: b.license_id for b in self._store.list_bindings()}

    def _used_slots(self, all_agent_ids: Iterable[str]) -> int:
        """Number of existing agents covered by a still-active license."""

        known = set(all_agent_ids)
        bindings = self._binding_map()
        active_ids = {lic.license_id for lic in self.active_licenses()}
        return sum(
            1
            for agent_id, license_id in bindings.items()
            if agent_id in known and license_id in active_ids
        )

    def summary(self, all_agent_ids: Iterable[str]) -> SlotSummary:
        known = list(all_agent_ids)
        total = self.total_slots()
        used = self._used_slots(known)
        packs = [lic for lic in self.active_licenses() if lic.kind is LicenseKind.PACK]
        expiries = [lic.expires_at for lic in packs if lic.expires_at is not None]
        next_expiry = min(expiries) if expiries else None
        days_to_next = (
            max(0, int((next_expiry - self.now()) // 86400))
            if next_expiry is not None
            else None
        )
        return SlotSummary(
            enforced=self.enforced,
            total_slots=total,
            used_slots=used,
            free_slots=max(0, total - used),
            packs_active=len(packs),
            next_expiry_at=next_expiry,
            days_to_next_expiry=days_to_next,
            install_id=self.install_id(),
        )

    def agent_view(self, agent_id: str) -> AgentLicenseView:
        license_id = self._binding_map().get(agent_id)
        lic = self._store.get_license(license_id) if license_id else None
        if lic is None or not self._pack_active(lic):
            return AgentLicenseView(
                agent_id=agent_id,
                license_id=license_id if lic is not None else None,
                kind=lic.kind if lic is not None else None,
                licensed=False,
                expires_at=lic.expires_at if lic is not None else None,
                days_left=None,
            )
        days_left = (
            max(0, int((lic.expires_at + self._grace_s - self.now()) // 86400))
            if lic.expires_at is not None
            else None
        )
        return AgentLicenseView(
            agent_id=agent_id,
            license_id=lic.license_id,
            kind=lic.kind,
            licensed=True,
            expires_at=lic.expires_at,
            days_left=days_left,
        )

    def agent_views(self, all_agent_ids: Iterable[str]) -> list[AgentLicenseView]:
        return [self.agent_view(agent_id) for agent_id in all_agent_ids]

    def is_agent_licensed(self, agent_id: str) -> bool:
        """Whether new work for ``agent_id`` is allowed (always so when open)."""

        if not self.enforced:
            return True
        return self.agent_view(agent_id).licensed

    def assert_agents_licensed(self, agent_ids: Iterable[str]) -> None:
        """Guard for starting new instances: raise 402 for an expired agent.

        Only bites when enforced. Running instances are never affected -- the
        caller invokes this only when *creating* a new instance.
        """

        if not self.enforced:
            return
        for agent_id in agent_ids:
            if not self.is_agent_licensed(agent_id):
                raise LicenseError(
                    f"Lizenz für Agent '{agent_id}' abgelaufen oder ungedeckt "
                    f"– bitte ein Agenten-Paket verlängern.",
                    status=402,
                )

    # ---- reconciliation / binding -----------------------------------------

    def _prune_stale_bindings(self, known: set[str]) -> None:
        for binding in self._store.list_bindings():
            if binding.agent_id not in known:
                self._store.unbind(binding.agent_id)

    def _slot_capacity(self) -> dict[str, int]:
        """Free capacity per active license id (slots minus current bindings)."""

        used_per: dict[str, int] = {}
        for binding in self._store.list_bindings():
            used_per[binding.license_id] = used_per.get(binding.license_id, 0) + 1
        capacity: dict[str, int] = {}
        for lic in self.active_licenses():
            capacity[lic.license_id] = max(0, lic.slots - used_per.get(lic.license_id, 0))
        return capacity

    def _first_free_license_id(self) -> str | None:
        """Pick an active license with spare capacity (FREE first, then packs)."""

        capacity = self._slot_capacity()
        free = self._ensure_free_license()
        if capacity.get(free.license_id, 0) > 0:
            return free.license_id
        for lic in self.active_licenses():
            if lic.kind is LicenseKind.PACK and capacity.get(lic.license_id, 0) > 0:
                return lic.license_id
        return None

    def reconcile(self, all_agent_ids: Iterable[str]) -> None:
        """Bind so-far-unbound existing agents to spare capacity (idempotent).

        This makes turning enforcement *on* seamless: agents that predate
        licensing are auto-bound to free/pack slots up to capacity; any overflow
        stays unbound and shows as "uncovered" (needing a pack), never blocking
        anything already running. A no-op while enforcement is off.
        """

        if not self.enforced:
            return
        known = set(all_agent_ids)
        self._prune_stale_bindings(known)
        bound = set(self._binding_map())
        for agent_id in sorted(known - bound):
            license_id = self._first_free_license_id()
            if license_id is None:
                break  # out of capacity -> remaining agents stay uncovered
            self._store.bind(agent_id, license_id)

    def can_create_agent(self, all_agent_ids: Iterable[str]) -> bool:
        if not self.enforced:
            return True
        self.reconcile(all_agent_ids)
        return self._used_slots(all_agent_ids) < self.total_slots()

    def auto_bind_new_agent(self, agent_id: str, all_agent_ids: Iterable[str]) -> None:
        """Bind a newly created agent to spare capacity, else raise 402.

        Called *after* an agent was created. When enforcement is off it does
        nothing (no bindings are written in open mode).
        """

        if not self.enforced:
            return
        known = set(all_agent_ids) | {agent_id}
        self._prune_stale_bindings(known)
        license_id = self._first_free_license_id()
        if license_id is None:
            raise LicenseError(
                "Agenten-Kontingent ausgeschöpft – bitte ein Agenten-Paket "
                "(+5 Agenten / 1 Jahr) hinzubuchen.",
                status=402,
            )
        self._store.bind(agent_id, license_id)

    def bind(self, agent_id: str, license_id: str, all_agent_ids: Iterable[str]) -> None:
        """Manually re-home an agent onto another license (§5A.2 "umhängen").

        Raises 422 for an unknown/inactive target and 402 when it has no spare
        slot (excluding the agent's own current binding to that license).
        """

        if not self.enforced:
            return
        lic = self._store.get_license(license_id)
        if lic is None or not self._pack_active(lic):
            raise LicenseError(
                f"Lizenz '{license_id}' unbekannt oder abgelaufen.", status=422
            )
        current = self._binding_map().get(agent_id)
        if current != license_id and self._slot_capacity().get(license_id, 0) <= 0:
            raise LicenseError(
                f"Lizenz '{license_id}' hat keinen freien Slot mehr.", status=402
            )
        self._store.bind(agent_id, license_id)

    # ---- activation / online anchoring ------------------------------------

    def activate(self, token: str) -> License:
        """Install a signed license token (base64-of-JSON or raw JSON).

        Verifies the signature and the installation binding before persisting;
        an invalid token raises ``LicenseError(422)``. Also writes a trusted
        time anchor from ``issued_at`` (a signed lower bound on real time).
        """

        lic = _parse_token(token)
        if lic.kind is not LicenseKind.PACK:
            raise LicenseError("Nur signierte Pakete können aktiviert werden.", status=422)
        if not verify_license(lic, self._pubkey):
            raise LicenseError("Ungültige oder nicht signierte Lizenz.", status=422)
        if lic.install_id and lic.install_id != self.install_id():
            raise LicenseError(
                "Diese Lizenz gehört zu einer anderen Installation.", status=422
            )
        self._store.put_license(lic)
        if lic.issued_at:
            self._anchor_trusted(lic.issued_at)
        return lic

    def refresh_time(self, trusted_now: float | None = None) -> TimeAnchor:
        """Advance the ratchet from an authoritative timestamp (occasional online).

        With no argument it advances the *untrusted* ratchet from the current
        effective time (harmless housekeeping); with a signed server timestamp
        it sets a trusted lower bound and anchors it into the hash-chained log.
        """

        if trusted_now is not None:
            self._anchor_trusted(trusted_now)
        else:
            self._clock.effective_now()
        anchor = self._store.get_time_anchor()
        return anchor or TimeAnchor()

    def _anchor_trusted(self, ts: float) -> None:
        """Record a trusted lower bound and embed it into the audit chain."""

        head = ""
        if self._anchor_writer is not None:
            head = self._anchor_writer(ts, True) or ""
        self._clock.advance(ts, trusted=True, audit_head=head)

    # ---- online auto-pull (§4: "online kaufen, offline weiterarbeiten") ----

    def new_claim(self, claim_base: str, *, slots: int, months: int) -> PendingClaim:
        """Mint a short-lived, single-use claim for online auto-pull.

        The high-entropy ``claim_token`` is generated here and shared with the
        licensor via the checkout deep-link; the licensor associates the paid,
        signed pack with it. The instance later polls ``poll_url`` (built from
        ``claim_base`` + token) and activates the returned token itself. A no-op
        precondition -- like every other guard -- when enforcement is off: minting
        a claim only makes sense once a licensor key is configured, because only a
        signed pack can be activated.

        Parameters
        ----------
        claim_base:
            Base URL of the licensor's claim endpoint; the token is appended.
        slots, months:
            Requested pack size / duration (informational; the licensor derives
            the real values from the paid order).
        """

        now = self.now()
        token = secrets.token_urlsafe(24)
        claim = PendingClaim(
            claim_token=token,
            poll_url=f"{claim_base.rstrip('/')}/{token}",
            slots=slots,
            months=months,
            created_at=now,
            expires_at=now + self._claim_ttl,
        )
        self._store.put_claim(claim)
        return claim

    def pending_claims(self) -> list[PendingClaim]:
        """Return the still-open claims, pruning any that have expired.

        Expired claims are dropped from the store as a side effect so the poller
        never contacts the licensor for a claim the customer can no longer fulfil
        online (they fall back to portal copy-&-paste).
        """

        now = self.now()
        open_claims: list[PendingClaim] = []
        for claim in self._store.list_claims():
            if claim.expires_at and now > claim.expires_at:
                self._store.remove_claim(claim.claim_token)
            else:
                open_claims.append(claim)
        return open_claims

    def poll_claims(
        self, fetcher: Callable[[str], str | None]
    ) -> list[License]:
        """Best-effort: fetch and activate issued packs for all open claims.

        For every open claim the ``fetcher`` is called with the claim's
        ``poll_url`` and returns either the signed license token (once the
        licensor has fulfilled the order) or ``None`` (still pending / transient
        network error). A returned token is activated and the claim retired; a
        failed activation retires the claim too (it will not succeed on retry).
        Every claim is isolated in its own ``try`` so one bad claim never stops
        the others, and the whole pass is a no-op while enforcement is off --
        mirroring the mail outbox: observing, never blocking, never raising.

        Returns the licenses that were successfully activated in this pass.
        """

        if not self.enforced:
            return []
        activated: list[License] = []
        for claim in self.pending_claims():
            try:
                token = fetcher(claim.poll_url)
            except Exception:  # noqa: BLE001 - best-effort; a claim never crashes
                continue  # transient; leave the claim open for the next pass
            if not token:
                continue  # licensor has not issued yet -> keep polling
            try:
                activated.append(self.activate(token))
            except LicenseError:
                pass  # invalid/foreign token: give up on this claim
            self._store.remove_claim(claim.claim_token)
        return activated


def _parse_token(token: str) -> License:
    """Decode a license token into a :class:`License` (base64-JSON or JSON)."""

    raw = token.strip()
    if not raw:
        raise LicenseError("Leerer Lizenz-Schlüssel.", status=422)
    text = raw
    if not raw.lstrip().startswith("{"):
        try:
            text = base64.b64decode(raw).decode()
        except Exception as exc:  # noqa: BLE001 - normalise to a licensing error
            raise LicenseError("Lizenz-Schlüssel ist nicht lesbar.", status=422) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LicenseError("Lizenz-Schlüssel ist kein gültiges JSON.", status=422) from exc
    try:
        return License.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - pydantic validation -> licensing error
        raise LicenseError("Lizenz-Schlüssel hat ein ungültiges Format.", status=422) from exc
