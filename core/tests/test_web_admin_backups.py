# SPDX-License-Identifier: BUSL-1.1
"""Die Sicherungsliste der Administration sagt, welche Version sie meint.

Die Spalte hiess nur „Version“. Gemeint ist die Version der Anwendung, mit
der die Sicherung entstand (``app_version`` aus dem Manifest) -- nicht der
Datenbank-Schemastand, nach dem sich ein Restore richtet, und nicht die
Version eines Prozessmodells. Die Spalte heisst deshalb „App-Version“.

Geprueft im Node-``vm`` (``web_vm.py``) mit der echten ``renderBackupsBody``.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_RENDER = r"""
const frag = renderBackupsBody({
  available: true,
  last_success: "2026-10-09T08:00:00Z",
  backups: [{ file: "procworks-2026-10-09.dump", created_at: "2026-10-09T08:00:00Z",
              app_version: "1.33.2", size_bytes: 2048, encrypted: true }],
}, el("div"));
const heads = frag.querySelectorAll("th").map((th) => textOf(th));
const cells = frag.querySelectorAll("td").map((td) => textOf(td));
return { heads, cells };
"""


@needs_node
def test_backup_list_names_the_app_version_column(tmp_path: Path) -> None:
    res = run_app_js(_RENDER, tmp_path)
    assert res["heads"] == ["Datei", "Zeitpunkt", "App-Version", "Größe", "Verschlüsselt"]
    # Der Wert der Spalte ist die Anwendungsversion aus dem Manifest.
    assert res["cells"][2] == "1.33.2"


@needs_node
def test_backup_without_app_version_shows_a_dash(tmp_path: Path) -> None:
    """Grenzfall: Ein Manifest ohne Versionsangabe zeigt „–“, keine leere Zelle."""

    res = run_app_js(
        _RENDER.replace('app_version: "1.33.2", ', ""),
        tmp_path,
    )
    assert res["heads"][2] == "App-Version"
    assert res["cells"][2] == "–"
