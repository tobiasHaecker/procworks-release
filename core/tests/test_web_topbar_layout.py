# SPDX-License-Identifier: BUSL-1.1
"""Kopfzeile und Raster laufen bei Laptop-Breite (ca. 1050 px) nicht über.

Bei 1054 px lag „BPMN-Import“ außerhalb der Kopfzeile, Untertitel brachen auf
sechs Zeilen um und überdeckten den Panelkopf, und die Ressourcensicht ließ
sich seitlich verschieben (ein Raster wuchs auf die Breite seiner Tabellen).
Gemessen im Browser: danach Kopfzeile 92 px hoch, ``.main`` ohne Überlauf in
Modellieren, Daten, Ressourcen, Ausführung, Prüfinstanz, Monitoring, Integration
und Administration. Hier gesichert: die CSS-Regeln, die das tragen.
"""

from __future__ import annotations

import re

from web_vm import APP_JS

STYLES = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    m = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", STYLES)
    assert m, f"Regel {selector} fehlt"
    return m.group(1)


def test_topbar_wraps_and_subtitle_truncates() -> None:
    assert "flex-wrap: wrap" in _rule(".topbar")
    sub = _rule(".topbar .sub")
    assert "text-overflow: ellipsis" in sub and "white-space: nowrap" in sub
    assert "min-width: 0" in sub
    assert "flex-wrap: wrap" in _rule(".picker")
    # Feste Kuerzung nur bei knappem Platz -- breite Bildschirme zeigen alles.
    assert "max-width" not in sub
    assert re.search(r"@media \(max-width: 1400px\) \{ \.topbar \.sub \{ max-width: 34ch; \} \}",
                     STYLES)


def test_grid_columns_may_shrink_below_their_tables() -> None:
    assert "min-width: 0" in _rule(".grid-2 > *")
    assert "overflow-x: auto" in _rule(".grid-2 .panel-b")


def test_truncated_subtitle_keeps_full_text_as_tooltip() -> None:
    assert 'byId("view-sub").title = meta.sub;' in APP_JS.read_text(encoding="utf-8")
