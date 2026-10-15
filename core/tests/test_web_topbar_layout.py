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

from web_source import app_js_source
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
    assert 'byId("view-sub").title = meta.sub;' in app_js_source()


def test_every_panel_with_a_table_scrolls_it_instead_of_clipping() -> None:
    """Tablet-Breite (820 px): Die Tabellen „Instanzen“ und „Engpässe“ des
    Monitorings stehen nicht im Raster ``.grid-2`` und waren breiter als ihr
    Panel; ``.panel`` schneidet mit ``overflow: hidden`` ab, „Fortschritt“ und
    „Ø Bearbeitung“ waren unerreichbar. Jeder Panelkoerper mit Tabelle scrollt
    sie jetzt selbst -- die Seite bleibt ohne Seitwaertsscrollen."""
    assert "overflow-x: auto" in _rule(".panel-b:has(table)")
    # Das Panel selbst schneidet weiter ab (runde Ecken), scrollt also nicht
    # seitlich -- sonst liefe der Kopf mit.
    assert "overflow: hidden" in _rule(".panel")


def test_the_action_column_is_pinned_right_while_its_table_scrolls() -> None:
    """Tablet-Breite (820 px): In „Meine Aufgaben“ lag die Aktionsspalte
    rechts außerhalb des Panels, erreichbar nur durch Seitwärtsscrollen ohne
    Hinweis. Sie ist jetzt rechts angeheftet (``position: sticky``), deckend in
    der Panelfarbe über eine CSS-Variable, mit Trennlinie und sichtbarem Fokus."""

    rule = _rule("th.col-actions, td.col-actions")
    assert "position: sticky" in rule and "right: 0" in rule
    assert "background: var(--bg-elev)" in rule
    assert "box-shadow: inset 1px 0 0 var(--line)" in rule
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rule), "Farben nur über Variablen"
    focus = _rule("td.col-actions .btn:focus-visible")
    assert "outline: 2px solid var(--accent)" in focus
    # Zeilen-Hintergruende (Hover, Auswahl, Hervorhebung) bleiben spezifischer
    # als die Spaltenregel und gelten damit auch in der angehefteten Zelle.
    assert "tr.clickable:hover td" in STYLES and "tr.hl-row td" in STYLES


def test_the_card_layout_does_not_pin_the_action_column() -> None:
    """Grenzfall (390 px): In der Kartenansicht steht die Aktionszeile in der
    Karte; angeheftet würde sie über den Karteninhalt rutschen."""

    mobile = STYLES[STYLES.index("@media (max-width: 720px)"):]
    m = re.search(r"\.content td\.col-actions \{([^}]*)\}", mobile)
    assert m, "Kartenansicht setzt die Aktionsspalte nicht zurück"
    assert "position: static" in m.group(1)
