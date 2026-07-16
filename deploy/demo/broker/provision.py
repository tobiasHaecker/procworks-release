# SPDX-License-Identifier: BUSL-1.1
"""Provisioning port for the demo broker (platform-neutral seam).

The broker never talks to a container platform directly. It depends only on the
narrow :class:`ProvisionPort` protocol (``create``/``start``/``stop``/
``destroy``/``status``), so swapping Fly.io for another on-demand platform stays
a local change. Two implementations ship here:

* :class:`InMemoryProvisioner` -- a fake for local development and tests; hands
  out fake instances without touching any cloud.
* :class:`FlyProvisioner` -- a *skeleton* that shows where the Fly Machines REST
  calls go. The actual HTTP payloads are marked ``TODO`` and must be filled in
  against the current Fly Machines API before use.

This module is a deployment artifact, not part of the correctness core.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class DemoInstance:
    """A provisioned demo instance the visitor is redirected to.

    Attributes:
        instance_id: Opaque platform id (Machine id), used to stop/destroy.
        url: Public HTTPS URL the visitor opens (the running demo).
        state: Coarse lifecycle state ("starting"/"started"/"stopped"/"gone").
    """

    instance_id: str
    url: str
    state: str


class ProvisionPort(Protocol):
    """The only surface the broker needs from a container platform."""

    def create(self, *, trial_id: str) -> DemoInstance:
        """Create AND start a fresh, isolated demo instance for one visitor."""

    def start(self, instance_id: str) -> DemoInstance:
        """Start a previously stopped instance (auto-start on re-access)."""

    def stop(self, instance_id: str) -> None:
        """Stop an idle instance (scale-to-zero; billing pauses)."""

    def destroy(self, instance_id: str) -> None:
        """Destroy an instance permanently (reaper / hard TTL). Idempotent."""

    def status(self, instance_id: str) -> DemoInstance | None:
        """Return the current instance state, or None if it no longer exists."""

    def list_ids(self) -> list[str]:
        """All currently live demo instance ids (authoritative active count)."""

    def instance_age_seconds(self, instance_id: str) -> float | None:
        """Age of an instance in seconds, or None if unknown/gone (reaper TTL)."""


@dataclass
class InMemoryProvisioner:
    """In-process fake provisioner for local dev / tests (no cloud calls).

    Deterministic and side-effect-free apart from its own dict. ``destroy`` is
    idempotent (destroying an unknown id is a no-op), matching the port contract.
    Tracks a creation timestamp per instance so the reaper's TTL logic is
    testable (see :meth:`instance_age_seconds`).
    """

    base_domain: str = "demo.procworks.local"
    _instances: dict[str, DemoInstance] = field(default_factory=dict)
    _created_at: dict[str, float] = field(default_factory=dict)

    def create(self, *, trial_id: str) -> DemoInstance:
        inst = DemoInstance(
            instance_id=f"mem-{trial_id}",
            url=f"https://trial-{trial_id}.{self.base_domain}",
            state="started",
        )
        self._instances[inst.instance_id] = inst
        self._created_at[inst.instance_id] = time.time()
        return inst

    def start(self, instance_id: str) -> DemoInstance:
        inst = self._instances[instance_id]
        started = DemoInstance(inst.instance_id, inst.url, "started")
        self._instances[instance_id] = started
        return started

    def stop(self, instance_id: str) -> None:
        inst = self._instances.get(instance_id)
        if inst is not None:
            self._instances[instance_id] = DemoInstance(inst.instance_id, inst.url, "stopped")

    def destroy(self, instance_id: str) -> None:
        self._instances.pop(instance_id, None)
        self._created_at.pop(instance_id, None)

    def status(self, instance_id: str) -> DemoInstance | None:
        return self._instances.get(instance_id)

    def list_ids(self) -> list[str]:
        return list(self._instances)

    def instance_age_seconds(self, instance_id: str) -> float | None:
        created = self._created_at.get(instance_id)
        return None if created is None else time.time() - created


@dataclass
class FlyProvisioner:
    """Skeleton :class:`ProvisionPort` backed by the Fly.io Machines REST API.

    The Machines API calls are real (auth header, base URL, create/start/stop/
    destroy/get payloads pinned against the current API). Reads its config from
    the environment (``FLY_API_TOKEN``, ``FLY_ORG``, ``DEMO_IMAGE_REF``,
    ``FLY_REGION``). Note ``DEMO_IMAGE_REF`` (not ``FLY_IMAGE_REF``) carries the
    demo image: Fly overrides any ``FLY_``-prefixed var at runtime.

    **Isolation model: one Fly app per visitor** (D2 decision, live-verified).
    Each trial gets its own app ``<app_prefix><trial_id>`` with exactly one
    Machine, so the visitor's URL is a unique, isolated ``https://<app>.fly.dev``
    and ``min_machines_running = 0`` per app keeps it at ~0 EUR at rest. Creating
    an app via the Machines API allocates **no** public IP, so ``create`` also
    allocates a shared v4 (+v6) via GraphQL (:meth:`_allocate_ips`) -- without it
    ``<app>.fly.dev`` does not resolve. The ``instance_id`` the port hands back is
    the **app name**; all lifecycle ops act on that app (one app = one demo). The
    reaper destroys the whole app at hard TTL.

    All correctness-relevant demo posture (in-memory, boot-seed, dormant
    licensing, egress-deny) lives in the *image* and the platform network policy,
    not here -- this class only creates/starts/stops/destroys that image.
    """

    api_base: str = "https://api.machines.dev/v1"
    graphql_base: str = "https://api.fly.io/graphql"
    org_slug: str = field(default_factory=lambda: os.environ.get("FLY_ORG", "personal"))
    image_ref: str = field(
        default_factory=lambda: os.environ.get("DEMO_IMAGE_REF")
        # NB: Fly reserves the ``FLY_`` env prefix and injects ``FLY_IMAGE_REF``
        # at *runtime* set to the running machine's own image -- which would clobber
        # a ``[env] FLY_IMAGE_REF`` and make the broker provision *itself* instead of
        # the demo image. Read ``DEMO_IMAGE_REF`` (never touched by Fly) first; keep
        # ``FLY_IMAGE_REF`` only as a legacy fallback for non-Fly callers.
        or os.environ.get("FLY_IMAGE_REF", "")
    )
    region: str = field(default_factory=lambda: os.environ.get("FLY_REGION", "fra"))
    app_prefix: str = "trial-"
    _token: str = field(default_factory=lambda: os.environ.get("FLY_API_TOKEN", ""))

    def _request(self, method: str, path: str, body: dict | None = None) -> object:
        """Issue an authenticated JSON request to the Machines API.

        Returns the parsed JSON (an object or array depending on the endpoint),
        or ``{}`` on an empty body. Raises RuntimeError on transport/HTTP error so
        the broker can fail the trial cleanly (and return a friendly "try again
        later" to the visitor).
        """
        if not self._token:
            raise RuntimeError("FLY_API_TOKEN is not set")
        url = f"{self.api_base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed host)
                raw = resp.read()
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise RuntimeError(f"Fly Machines API call failed: {exc}") from exc
        return json.loads(raw) if raw else {}

    def _graphql(self, query: str, variables: dict) -> dict:
        """Call the Fly GraphQL API (used for IP allocation, which flaps lacks).

        Raises RuntimeError on transport error or a GraphQL ``errors`` payload so
        a failed allocation fails the trial cleanly rather than handing the
        visitor a dead URL.
        """
        if not self._token:
            raise RuntimeError("FLY_API_TOKEN is not set")
        data = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(self.graphql_base, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed host)
                body = json.loads(resp.read())
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise RuntimeError(f"Fly GraphQL call failed: {exc}") from exc
        if body.get("errors"):
            raise RuntimeError(f"Fly GraphQL error: {body['errors']}")
        result = body.get("data")
        return result if isinstance(result, dict) else {}

    def _allocate_ips(self, app_name: str) -> None:
        """Give a fresh per-visitor app a public shared IPv4 + IPv6 so it routes.

        Creating a Machine via the Machines API does **not** auto-allocate public
        IPs (unlike ``fly launch``); without them ``<app>.fly.dev`` does not even
        resolve. A **shared** v4 keeps cost at zero. The v6 is best-effort (v4
        alone already routes ``.fly.dev``); the v4 is required, so its failure
        propagates and fails the trial.
        """
        mutation = (
            "mutation($input: AllocateIPAddressInput!) {"
            " allocateIpAddress(input: $input) { ipAddress { address type } } }"
        )
        self._graphql(mutation, {"input": {"appId": app_name, "type": "shared_v4"}})
        try:
            self._graphql(mutation, {"input": {"appId": app_name, "type": "v6"}})
        except RuntimeError:  # pragma: no cover - v6 is optional for routing
            pass

    def _app_name(self, trial_id: str) -> str:
        return f"{self.app_prefix}{trial_id}"

    def _public_url(self, app_name: str) -> str:
        """Unique per-visitor URL (one app per visitor -> own ``.fly.dev``)."""
        return f"https://{app_name}.fly.dev"

    def create(self, *, trial_id: str) -> DemoInstance:
        """Create a per-visitor app + its single demo Machine (real Fly calls).

        Two steps: (1) create the isolated app, (2) create+start one Machine in
        it. Small capped guest; HTTP service with scale-to-zero (``autostop=stop``
        / ``autostart=true``) so an idle Machine costs ~0 and wakes on the next
        request. ``auto_destroy`` stays False on purpose -- the reaper owns the
        hard-TTL destroy; ``auto_destroy`` would kill the Machine on every idle
        stop and defeat auto-start.
        """
        app_name = self._app_name(trial_id)
        self._request("POST", "/apps", {"app_name": app_name, "org_slug": self.org_slug})
        # A Machines-API-created app has NO public IP (verified live), so
        # <app>.fly.dev would not resolve. Allocate a shared v4 (+v6) before the
        # Machine so the visitor URL routes.
        self._allocate_ips(app_name)
        machine_payload = {
            "name": app_name,
            "region": self.region,
            "config": {
                "image": self.image_ref,
                "auto_destroy": False,
                "restart": {"policy": "on-failure"},
                "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 512},
                "services": [
                    {
                        "protocol": "tcp",
                        "internal_port": 8000,
                        "autostart": True,
                        "autostop": "stop",
                        "force_https": True,
                        "ports": [
                            {"port": 443, "handlers": ["tls", "http"]},
                            {"port": 80, "handlers": ["http"]},
                        ],
                    }
                ],
            },
        }
        created = self._request("POST", f"/apps/{app_name}/machines", machine_payload)
        state = created.get("state", "starting") if isinstance(created, dict) else "starting"
        return DemoInstance(instance_id=app_name, url=self._public_url(app_name), state=str(state))

    def _machines(self, app_name: str) -> list[dict]:
        """List the Machines of a per-visitor app (normally exactly one)."""
        result = self._request("GET", f"/apps/{app_name}/machines")
        return result if isinstance(result, list) else []

    def start(self, instance_id: str) -> DemoInstance:
        # instance_id is the per-visitor app name; start its Machine(s).
        for machine in self._machines(instance_id):
            self._request("POST", f"/apps/{instance_id}/machines/{machine['id']}/start")
        status = self.status(instance_id)
        if status is None:  # pragma: no cover - race with destroy
            raise RuntimeError(f"demo app {instance_id} vanished during start")
        return status

    def stop(self, instance_id: str) -> None:
        for machine in self._machines(instance_id):
            self._request("POST", f"/apps/{instance_id}/machines/{machine['id']}/stop")

    def destroy(self, instance_id: str) -> None:
        # Deleting the whole app tears down its Machine(s) and the URL. Idempotent
        # from the broker's view -- a 404 (already gone) is treated as success.
        try:
            self._request("DELETE", f"/apps/{instance_id}")
        except RuntimeError:  # pragma: no cover - treat "already gone" as done
            pass

    def status(self, instance_id: str) -> DemoInstance | None:
        try:
            machines = self._machines(instance_id)
        except RuntimeError:  # pragma: no cover - app gone
            return None
        url = self._public_url(instance_id)
        if not machines:
            # App exists but no Machine yet (mid-provision) -> report "created".
            return DemoInstance(instance_id=instance_id, url=url, state="created")
        return DemoInstance(
            instance_id=instance_id, url=url, state=str(machines[0].get("state", "unknown"))
        )

    def list_ids(self) -> list[str]:
        """Reaper inventory: all per-visitor demo app names in the org.

        Filters the org's apps by the trial prefix so only demo apps are reaped.
        """
        result = self._request("GET", f"/apps?org_slug={self.org_slug}")
        apps = result.get("apps", []) if isinstance(result, dict) else []
        return [a["name"] for a in apps if str(a.get("name", "")).startswith(self.app_prefix)]

    def instance_age_seconds(self, instance_id: str) -> float | None:
        """Age of the app's oldest Machine in seconds (reaper hard-TTL check).

        Reads the Machine ``created_at`` timestamps and returns ``now - oldest``.
        Returns None when the app has no Machine or the timestamp is unparseable,
        so the reaper can decide its policy for unknown ages (it reaps them).
        """
        try:
            machines = self._machines(instance_id)
        except RuntimeError:  # pragma: no cover - app gone
            return None
        created: list[float] = []
        for machine in machines:
            raw = str(machine.get("created_at", ""))
            if not raw:
                continue
            try:
                created.append(datetime.fromisoformat(raw).timestamp())
            except ValueError:  # pragma: no cover - unexpected format
                continue
        if not created:
            return None
        return time.time() - min(created)


def new_trial_id() -> str:
    """Return a short, URL-safe, unguessable trial id (per-visitor subdomain)."""
    return secrets.token_hex(8)
