# SPDX-License-Identifier: BUSL-1.1
"""Reaper: destroys expired/orphaned demo instances (cost backstop, skeleton).

A scheduled, idempotent job (cron / platform scheduler, itself scale-to-zero)
that enforces the hard TTL: any demo instance older than ``DEMO_TTL_SECONDS`` is
destroyed, so a runaway or stuck instance can never keep billing. This is a
*backstop* -- the platform's own auto-stop-on-idle is the primary cost control;
the reaper only guarantees an upper bound.

The instance inventory ("which demos exist and when did they start") is owned by
the platform, not by ProcWorks. The reaper therefore lists instances and reads
their age through the same :class:`ProvisionPort` seam the broker uses
(:meth:`~provision.ProvisionPort.list_ids` +
:meth:`~provision.ProvisionPort.instance_age_seconds`; the Fly path reads each
Machine's ``created_at``), so it needs no state of its own and is safe to run
concurrently with the broker's own opportunistic reaping.

Two ways to run this backstop on a schedule -- pick whichever fits the platform:

* **Poke the broker** (no extra always-on infra): ``POST /admin/reap`` on the
  already-deployed, scale-to-zero broker runs the very same sweep and wakes the
  broker for the duration. Any scheduler (a Fly scheduled Machine, cron, an
  uptime pinger) can trigger it with one authenticated request. This is the
  recommended path -- the broker already holds the Fly token.
* **Run this CLI directly** (``python reaper.py``) from a scheduler that can
  provide ``FLY_API_TOKEN`` + ``DEMO_PROVISIONER=fly`` itself.

This is a deployment artifact, not part of the correctness core.
"""

from __future__ import annotations

import os
import sys

# The reaper ships alongside the broker and reuses its provisioning seam.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "broker"))

from provision import FlyProvisioner, InMemoryProvisioner, ProvisionPort  # noqa: E402


def _select_provisioner() -> ProvisionPort:
    """Mirror the broker's provisioner selection (fly vs. in-memory fake)."""
    if os.environ.get("DEMO_PROVISIONER", "").lower() == "fly":
        return FlyProvisioner()
    return InMemoryProvisioner()


def reap(provisioner: ProvisionPort, *, ttl_seconds: int) -> list[str]:
    """Destroy every instance older than ``ttl_seconds``; return destroyed ids.

    Idempotent: destroying an already-gone instance is a no-op (port contract),
    so overlapping runs and retries are safe (and it is safe to run alongside the
    broker's own opportunistic reaping).

    The age comes from :meth:`~provision.ProvisionPort.instance_age_seconds`
    (the Fly path reads the Machine's ``created_at``). An instance younger than
    ``ttl_seconds`` is kept; every other one -- including any whose age cannot be
    read (``None``) -- is reaped, so nothing lingers unbounded (cost safety).
    """
    destroyed: list[str] = []
    # Inventory listing goes through ``list_ids``: the in-memory fake lists its
    # own instances; the Fly path lists the org's per-visitor demo apps (by the
    # ``trial-`` prefix). ``destroy`` then tears the whole app down.
    list_ids = getattr(provisioner, "list_ids", None)
    if list_ids is None:
        return destroyed
    age_of = getattr(provisioner, "instance_age_seconds", None)
    for instance_id in list_ids():
        # Hard-TTL policy: keep instances younger than ttl_seconds; reap the rest.
        # Unknown age (None) is reaped -- a demo whose age we cannot read is
        # treated as expired so nothing lingers unbounded (cost safety).
        if age_of is not None:
            age = age_of(instance_id)
            if age is not None and age < ttl_seconds:
                continue
        provisioner.destroy(instance_id)
        destroyed.append(instance_id)
    return destroyed


def main() -> int:
    """CLI entry point for the scheduled run."""
    ttl = int(os.environ.get("DEMO_TTL_SECONDS", str(24 * 3600)))
    provisioner = _select_provisioner()
    destroyed = reap(provisioner, ttl_seconds=ttl)
    print(f"reaper: destroyed {len(destroyed)} expired demo instance(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
