# SPDX-License-Identifier: BUSL-1.1
"""Read-only view onto the operational backup layer (roadmap B6).

The datensicherung itself lives entirely on the operations layer (the Compose
``backup`` service / Helm ``CronJob`` running ``deploy/backup/*.sh``). The API
deliberately does **not** run ``pg_dump`` and, per the concept's security rule
(§9, *"kein Web-/API-Zugriff auf das Backup-Verzeichnis"*), it also never mounts
the volume that holds the actual dumps.

Instead the backup scheduler publishes a small **metadata** file
``backups-index.json`` (plus the ``.last-success`` / ``.last-verify-success``
markers) into a separate *control* directory shared with the API. This module
reads that metadata so an admin can see the backup state in the GUI, and writes
a ``.run-now`` marker that asks the scheduler to take a backup now. No dump
contents are ever exposed to the API, and the API holds no database dump rights.

Everything degrades gracefully: if no control directory is configured or the
index has not been written yet, the status simply reports ``available = false``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

# Environment variable naming the shared control directory. Empty/unset means the
# feature is not wired for this deployment (in-memory dev, or a stack without the
# backup service) -- the endpoint then reports "not available" rather than error.
CONTROL_DIR_ENV = "PROCWORKS_BACKUP_CONTROL_DIR"

# File names agreed with the shell side (deploy/backup/lib.sh :: publish_index).
INDEX_FILE = "backups-index.json"
RUN_NOW_MARKER = ".run-now"


class BackupEntry(BaseModel):
    """One backup as advertised in the index (metadata only, never dump bytes)."""

    file: str = Field(description="Backup file name, e.g. procworks-2026-07-08T02-00.dump")
    created_at: str | None = Field(default=None, description="UTC timestamp the dump was taken")
    app_version: str | None = Field(default=None, description="ProcWorks version at backup time")
    alembic_head: str | None = Field(
        default=None, description="Schema migration revision in the dump"
    )
    pg_major_version: int | None = Field(default=None, description="PostgreSQL major version")
    size_bytes: int | None = Field(
        default=None, description="On-disk size of the (possibly encrypted) dump"
    )
    sha256: str | None = Field(default=None, description="Checksum recorded in the manifest")
    encrypted: bool | None = Field(
        default=None, description="Whether the dump is encrypted at rest"
    )


class BackupsStatus(BaseModel):
    """Overall backup state for the admin view."""

    available: bool = Field(
        description="False when the backup control surface is not wired/populated"
    )
    directory: str | None = Field(default=None, description="Configured control directory, if any")
    generated_at: str | None = Field(
        default=None, description="When the index was last published"
    )
    last_success: str | None = Field(
        default=None, description="Timestamp of the last successful backup"
    )
    last_verify_success: str | None = Field(
        default=None, description="Timestamp of the last successful restore self-test"
    )
    backups: list[BackupEntry] = Field(
        default_factory=list, description="Known backups, newest first"
    )


def control_dir() -> Path | None:
    """Return the configured control directory, or ``None`` when unset/blank.

    Read at call time (not import time) so tests and reconfiguration take effect
    without reimporting the module.
    """
    raw = os.environ.get(CONTROL_DIR_ENV, "").strip()
    return Path(raw) if raw else None


def _coerce_entry(raw: object) -> BackupEntry | None:
    """Build a :class:`BackupEntry` from one index element, tolerating junk.

    The index is produced by a shell script; a malformed or partial element must
    never break the whole listing, so anything unparseable is skipped.
    """
    if not isinstance(raw, dict):
        return None
    file = raw.get("file")
    if not isinstance(file, str) or not file:
        return None
    try:
        return BackupEntry.model_validate(raw)
    except Exception:
        # Fall back to just the file name if optional fields are ill-typed.
        return BackupEntry(file=file)


def load_status(directory: Path | None) -> BackupsStatus:
    """Read the published index and return a :class:`BackupsStatus`.

    Never raises: a missing directory, missing index file or malformed JSON all
    resolve to ``available = false`` (optionally with the directory echoed back),
    so the GUI can show a clear "not configured yet" state.
    """
    if directory is None:
        return BackupsStatus(available=False)

    index_path = directory / INDEX_FILE
    if not index_path.is_file():
        return BackupsStatus(available=False, directory=str(directory))

    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return BackupsStatus(available=False, directory=str(directory))

    if not isinstance(data, dict):
        return BackupsStatus(available=False, directory=str(directory))

    raw_backups = data.get("backups")
    entries: list[BackupEntry] = []
    if isinstance(raw_backups, list):
        for item in raw_backups:
            entry = _coerce_entry(item)
            if entry is not None:
                entries.append(entry)

    def _str_or_none(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    return BackupsStatus(
        available=True,
        directory=str(directory),
        generated_at=_str_or_none(data.get("generated_at")),
        last_success=_str_or_none(data.get("last_success")),
        last_verify_success=_str_or_none(data.get("last_verify_success")),
        backups=entries,
    )


def request_run_now(directory: Path | None) -> None:
    """Ask the scheduler to take a backup now by touching the ``.run-now`` marker.

    The API never runs ``pg_dump`` itself; it only drops a marker file that the
    scheduler polls (file-based handoff, no coupling, no DB credentials in the
    API). Raises :class:`RuntimeError` if no control directory is configured and
    lets :class:`OSError` propagate if the marker cannot be written (e.g. the
    control volume is read-only), so the endpoint can surface a clear error.
    """
    if directory is None:
        raise RuntimeError("backup control directory is not configured")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RUN_NOW_MARKER).touch()
