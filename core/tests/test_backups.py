# SPDX-License-Identifier: BUSL-1.1
"""Tests for the read-only backup admin view (roadmap B6).

Covers the pure :mod:`procworks.backups` reader/trigger and the two API
endpoints ``GET /admin/backups`` and ``POST /admin/backups/run-now``. The API
only ever reads the metadata index the scheduler publishes into a shared control
directory and writes a ``.run-now`` marker -- it never touches the dumps.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from procworks import backups
from procworks.api import app

client = TestClient(app)


def _write_index(directory: Path, payload: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / backups.INDEX_FILE).write_text(json.dumps(payload), encoding="utf-8")


_SAMPLE_INDEX: dict[str, object] = {
    "generated_at": "2026-07-08T02:00:05Z",
    "last_success": "2026-07-08T02:00:05Z",
    "last_verify_success": "2026-07-07T03:00:10Z",
    "backups": [
        {
            "file": "procworks-2026-07-08T02-00.dump",
            "created_at": "2026-07-08T02:00:01Z",
            "app_version": "1.1.0",
            "alembic_head": "0007_webhook_outbox",
            "pg_major_version": 16,
            "size_bytes": 20480,
            "sha256": "abc123",
            "encrypted": False,
        },
        {
            "file": "procworks-2026-07-07T02-00.dump",
            "created_at": "2026-07-07T02:00:01Z",
            "encrypted": True,
        },
    ],
}


# --------------------------------------------------------------------------
# Pure reader (procworks.backups)
# --------------------------------------------------------------------------

def test_load_status_unconfigured() -> None:
    """No control directory -> not available, no directory echoed."""
    status = backups.load_status(None)
    assert status.available is False
    assert status.directory is None
    assert status.backups == []


def test_load_status_no_index(tmp_path: Path) -> None:
    """Configured directory but nothing published yet -> not available."""
    status = backups.load_status(tmp_path)
    assert status.available is False
    assert status.directory == str(tmp_path)


def test_load_status_valid_index(tmp_path: Path) -> None:
    """A well-formed index is parsed, newest-first order preserved."""
    _write_index(tmp_path, _SAMPLE_INDEX)
    status = backups.load_status(tmp_path)
    assert status.available is True
    assert status.last_success == "2026-07-08T02:00:05Z"
    assert status.last_verify_success == "2026-07-07T03:00:10Z"
    assert [b.file for b in status.backups] == [
        "procworks-2026-07-08T02-00.dump",
        "procworks-2026-07-07T02-00.dump",
    ]
    assert status.backups[0].pg_major_version == 16
    assert status.backups[0].encrypted is False
    assert status.backups[1].encrypted is True


def test_load_status_malformed_json(tmp_path: Path) -> None:
    """Garbage JSON degrades to not-available rather than raising."""
    (tmp_path / backups.INDEX_FILE).write_text("{not json", encoding="utf-8")
    status = backups.load_status(tmp_path)
    assert status.available is False


def test_load_status_skips_bad_entries(tmp_path: Path) -> None:
    """Unparseable list elements are skipped, good ones kept."""
    _write_index(
        tmp_path,
        {
            "backups": [
                "not-a-dict",
                {"no_file_key": True},
                {"file": "procworks-2026-07-08T02-00.dump"},
                {"file": "procworks-2026-07-06T02-00.dump", "size_bytes": "oops"},
            ]
        },
    )
    status = backups.load_status(tmp_path)
    assert status.available is True
    # "not-a-dict" and the file-less dict are dropped; both valid files survive
    # (the one with an ill-typed size falls back to just the file name).
    assert [b.file for b in status.backups] == [
        "procworks-2026-07-08T02-00.dump",
        "procworks-2026-07-06T02-00.dump",
    ]
    assert status.backups[1].size_bytes is None


def test_request_run_now_writes_marker(tmp_path: Path) -> None:
    backups.request_run_now(tmp_path)
    assert (tmp_path / backups.RUN_NOW_MARKER).is_file()


def test_request_run_now_unconfigured_raises() -> None:
    with pytest.raises(RuntimeError):
        backups.request_run_now(None)


# --------------------------------------------------------------------------
# API endpoints
# --------------------------------------------------------------------------

def test_get_admin_backups_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(backups.CONTROL_DIR_ENV, raising=False)
    resp = client.get("/admin/backups")
    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_get_admin_backups_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_index(tmp_path, _SAMPLE_INDEX)
    monkeypatch.setenv(backups.CONTROL_DIR_ENV, str(tmp_path))
    resp = client.get("/admin/backups")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["last_success"] == "2026-07-08T02:00:05Z"
    assert len(body["backups"]) == 2


def test_run_now_writes_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(backups.CONTROL_DIR_ENV, str(tmp_path))
    resp = client.post("/admin/backups/run-now")
    assert resp.status_code == 200
    assert resp.json()["requested"] is True
    assert (tmp_path / backups.RUN_NOW_MARKER).is_file()


def test_run_now_unconfigured_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(backups.CONTROL_DIR_ENV, raising=False)
    resp = client.post("/admin/backups/run-now")
    assert resp.status_code == 503
