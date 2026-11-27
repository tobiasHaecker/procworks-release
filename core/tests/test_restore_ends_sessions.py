# SPDX-License-Identifier: BUSL-1.1
"""A restore ends every login session; a normal restart does not.

Login sessions live in the database (table ``auth_session``) and therefore in
every backup. Restoring a backup used to bring back the sessions of the backup
time -- including ones that had been logged out, or whose login was deleted,
after the backup. After a restore following a security incident that is the
opposite of what an operator wants. The restore now replays everything except
the *data* of ``auth_session``: the table is recreated empty, in the same
single transaction, and everybody logs in again.

Tested without a database: ``pg_restore`` is replaced by a stand-in that prints
a table of contents as the real one does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LIB = _REPO / "deploy" / "backup" / "lib.sh"
_RESTORE = _REPO / "deploy" / "backup" / "restore.sh"

_needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")

#: A table of contents as ``pg_restore --list`` prints it (shortened).
_TOC = """;
; Archive created at 2026-10-10 02:00:01 UTC
;
215; 1259 16390 TABLE public auth_session process
216; 1259 16398 TABLE public process_instance process
217; 1259 16405 TABLE public auth_session_archive process
3410; 0 16390 TABLE DATA public auth_session process
3411; 0 16398 TABLE DATA public process_instance process
3412; 0 16405 TABLE DATA public auth_session_archive process
3290; 2606 16396 CONSTRAINT public auth_session auth_session_pkey process
"""


def _list_without_sessions(
    tmp_path: Path, toc: str, status: int = 0
) -> subprocess.CompletedProcess[str]:
    """Run ``restore_list_without_sessions`` with a stand-in ``pg_restore``
    that prints ``toc`` and exits with ``status``."""

    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "pg_restore"
    script.write_text(
        "#!/bin/sh\n"
        '[ "$1" = "--list" ] || { echo "unexpected: $*" >&2; exit 9; }\n'
        f"cat <<'TOC'\n{toc}TOC\n"
        f"exit {status}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run(  # noqa: S603 - fixed arguments, files of this test
        ["sh", "-c", f'. "{_LIB}"; restore_list_without_sessions backup.dump'],
        capture_output=True, text=True, env=env, check=False, timeout=30,
    )


@_needs_sh
def test_the_restore_list_leaves_out_exactly_the_session_data(tmp_path: Path) -> None:
    result = _list_without_sessions(tmp_path, _TOC)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "3410; 0 16390 TABLE DATA public auth_session process" not in lines
    # the table itself is recreated (empty), its constraint too
    assert "215; 1259 16390 TABLE public auth_session process" in lines
    assert "3290; 2606 16396 CONSTRAINT public auth_session auth_session_pkey process" in lines
    # every other table keeps its data -- also one whose name merely starts alike
    assert "3411; 0 16398 TABLE DATA public process_instance process" in lines
    assert "3412; 0 16405 TABLE DATA public auth_session_archive process" in lines


@_needs_sh
def test_a_backup_without_sessions_is_listed_unchanged(tmp_path: Path) -> None:
    toc = "216; 1259 16398 TABLE public process_instance process\n"
    result = _list_without_sessions(tmp_path, toc)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["216; 1259 16398 TABLE public process_instance process"]


@_needs_sh
def test_an_unreadable_backup_fails_instead_of_restoring_nothing(tmp_path: Path) -> None:
    """Without this, an empty list would make pg_restore restore *nothing* --
    and drop nothing -- while the script reported success."""

    result = _list_without_sessions(tmp_path, "", status=1)

    assert result.returncode == 1
    assert "not found" not in result.stderr  # the helper ran and passed the failure on


def test_the_restore_replays_that_list_in_its_single_transaction() -> None:
    script = _RESTORE.read_text(encoding="utf-8")

    assert 'restore_list_without_sessions "$_plain" > "$_list"' in script
    restore = script[script.index("if pg_restore --clean"):]
    restore = restore[: restore.index("; then")]
    assert "--single-transaction" in restore
    assert '--use-list "$_list"' in restore
