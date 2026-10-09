# SPDX-License-Identifier: BUSL-1.1
"""Dialog bodies: text runs full width, fields share the grid, nothing overflows.

Many dialogs build their body as ``form-grid`` (columns of at least 150 px).
Every child became a column -- a confirmation text stood in two narrow columns
side by side, a dry-run result landed in a middle column, a hint and a reason
in three slim columns. The escalation stage row used a fixed ``auto`` column
whose width came from the longest role name and pushed "Ziel-Rolle" past the
dialog edge. These guards pin the CSS rules that fix the whole class, plus the
dialog-specific widths.
"""

from __future__ import annotations

import re

from web_source import app_js_source
from web_vm import APP_JS

STYLES = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")
SRC = app_js_source()


def _rule(selector_regex: str) -> str:
    m = re.search(selector_regex + r"\s*\{([^}]*)\}", STYLES)
    assert m, f"CSS-Regel fehlt: {selector_regex}"
    return m.group(1)


def _body(name: str) -> str:
    start = SRC.index(name)
    end = SRC.index("\n}\n", start)
    return SRC[start:end]


def test_non_field_children_of_a_form_grid_span_the_full_width() -> None:
    m = re.search(r"\.form-grid > ((?::not\([^)]*\))+)\s*\{([^}]*)\}", STYLES)
    assert m, "Keine Vollbreiten-Regel fuer Nicht-Felder im Raster"
    excluded = set(re.findall(r":not\(([^)]*)\)", m.group(1)))
    assert "grid-column: 1 / -1" in m.group(2)
    # Felder, Beschriftungen und Knoepfe bleiben in ihrer Spalte.
    assert {".field", "label", "button", ".btn"} <= excluded
    # Text darf nicht ausgenommen sein -- genau er stand in schmalen Spalten.
    assert not excluded & {"p", "div", ".muted", ".card-hint"}


def test_grid_children_may_shrink_and_controls_stay_inside_their_field() -> None:
    assert "min-width: 0" in _rule(r"\.form-grid > \*")
    assert "min-width: 0" in _rule(r"\.branch-row > \*")
    assert "max-width: 100%" in _rule(r"\.field select, \.field input, \.field textarea")
    # Mehrzeilige Eingaben belegen die volle Breite.
    assert "grid-column: 1 / -1" in _rule(r"\.form-grid > \.field:has\(> textarea\)")


def test_escalation_stage_row_wraps_instead_of_overflowing() -> None:
    rule = _rule(r"\.branch-row\.esc-row")
    assert "auto-fit" in rule and "minmax(" in rule
    body = _body("function setEscalationFor(")
    # Kein ``.row`` (flex-wrap) als Huelle: der mass das Raster bei Minimalbreite.
    assert 'const rows = el("div", { class: "row"' not in body
    assert '"branch-row esc-row"' in body


def test_long_choices_get_their_own_line_in_rule_and_webhook_dialogs() -> None:
    staff = _body("function addStaffRule(")
    assert '"field wide" }, "Verknüpfen mit"' in staff
    hook = _body("function addWebhook(")
    assert '"field wide" }, "Ziel-URL"' in hook
