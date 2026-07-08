# SPDX-License-Identifier: BUSL-1.1
"""Guard test: the Helm ConfigMap copies of the backup scripts must stay in
byte-for-byte sync with the canonical scripts under ``deploy/backup/``.

Why this test exists
--------------------
The backup/restore shell scripts have a single canonical home in
``deploy/backup/`` (used directly by the Docker Compose ``backup`` service).
The Helm chart cannot read files outside its own directory via ``.Files``, so
the Kubernetes ``CronJob``/restore ``Job`` load the scripts from a ConfigMap
built out of copies under ``deploy/helm/files/backup/``. Copies can silently
drift from the originals; this test fails CI the moment they do, so an edit to a
canonical script is never shipped to Kubernetes users half-applied.

The scheduler (``run-scheduler.sh``) is intentionally *not* mirrored: Kubernetes
schedules via the CronJob, so the in-container cron loop is Compose-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# repo root: core/tests/this_file.py -> parents[2] == repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = _REPO_ROOT / "deploy" / "backup"
_CHART_COPY = _REPO_ROOT / "deploy" / "helm" / "files" / "backup"

# The scripts the Helm ConfigMap ships (everything the K8s CronJob/Job needs).
_MIRRORED = ("lib.sh", "backup-once.sh", "restore.sh", "verify.sh")


@pytest.mark.parametrize("name", _MIRRORED)
def test_helm_backup_script_matches_canonical(name: str) -> None:
    """Each mirrored script must be identical to its ``deploy/backup/`` original."""
    canonical = _CANONICAL / name
    chart_copy = _CHART_COPY / name
    assert canonical.is_file(), f"canonical script missing: {canonical}"
    assert chart_copy.is_file(), (
        f"Helm chart copy missing: {chart_copy}. Copy it from {canonical} "
        f"(the chart ConfigMap loads deploy/helm/files/backup/*.sh)."
    )
    assert canonical.read_bytes() == chart_copy.read_bytes(), (
        f"{chart_copy} drifted from {canonical}. After editing the canonical "
        f"script, re-copy it into deploy/helm/files/backup/."
    )
