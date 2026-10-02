# SPDX-License-Identifier: BUSL-1.1
"""Phone width: tables keep their labels, nothing scrolls sideways.

* Tables built with ``el("table")`` directly showed cards without "Instanz /
  Schema / Status" -- only ``table()`` sets ``data-label``.
* The sign-in card was ``94vw`` wide inside a 24 px padded overlay and stuck
  out on the right at about 390 px.
* The resource view scrolled sideways: ``1fr`` is ``minmax(auto, 1fr)``, so
  the widest non-wrapping row (unit buttons) set the column width.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_vm import APP_JS, needs_node, run_app_js

SRC = APP_JS.read_text(encoding="utf-8")
STYLES = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")


def test_every_table_is_built_by_the_shared_helper() -> None:
    """Class guard: the only ``el("table"`` is inside ``table()`` itself."""

    code = re.sub(r"(?m)^\s*(//|\*).*$", "", SRC)
    assert code.count('el("table"') == 1
    assert 'el("td", null' not in code


@needs_node
def test_table_rows_take_attributes_and_cells_carry_labels(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
let clicked = null;
const t = table(["Instanz", ""], [["A", "x"], ["B", "y"]],
  (i) => ({ class: "clickable", onClick: () => { clicked = i; } }), { class: "mig-table" });
const rows = t.querySelectorAll("tr").slice(1);   // erste Zeile: Kopf
await rows[1].click();
const legacy = table(["H"], [["a"]], () => "sel").querySelectorAll("tr")[1];
return { cls: t.classList.contains("mig-table"), clicked,
  rowCls: rows[0].classList.contains("clickable"),
  labels: rows[0].querySelectorAll("td").map((td) => td.getAttribute("data-label")),
  legacy: legacy.classList.contains("sel") };
""",
        tmp_path,
    )
    assert res["cls"] is True and res["clicked"] == 1
    assert res["rowCls"] is True
    assert res["labels"] == ["Instanz", None]   # leere Ueberschrift: kein Label
    assert res["legacy"] is True               # bisherige Aufrufer (Klasse als String)


def test_sign_in_card_fits_inside_the_padded_overlay() -> None:
    m = re.search(r"\.auth-card \{([^}]*)\}", STYLES)
    rule = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S) if m else ""
    assert "width: min(380px, calc(100vw - 48px))" in rule
    assert "94vw" not in rule


def test_resource_view_columns_may_shrink_and_unit_rows_wrap() -> None:
    assert re.search(r"@media \(max-width: 1100px\) \{ \.grid-2 \{ "
                     r"grid-template-columns: minmax\(0, 1fr\); \} \}", STYLES)
    row = re.search(r"\.tree-row \{([^}]*)\}", STYLES)
    assert row and "flex-wrap: wrap" in row.group(1)


def test_migration_dialog_table_becomes_cards_too() -> None:
    """The migration assistant is a dialog, outside ``.content`` -- its table
    needs the card rules explicitly."""

    assert ".mig-table td::before" in STYLES
    assert re.search(r"\.content td, \.mig-table td \{", STYLES)
