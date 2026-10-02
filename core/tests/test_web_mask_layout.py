# SPDX-License-Identifier: BUSL-1.1
"""Die Aufgabenmaske sieht aus wie die Vorschau im Designer.

Mit „2 Spalten“ und einer Gruppe standen alle Felder in einer schmalen linken
Spalte: Die Maske lag in einem ``form-grid`` mit automatischen 150-px-Spalten
und belegte nur eine Zelle. Im Browser nachgemessen: danach zwei Spalten mit je
252 px statt einer mit 165 px.
"""

from __future__ import annotations

import re

from web_vm import APP_JS

SRC = APP_JS.read_text(encoding="utf-8")
STYLES = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")


def _body(head: str) -> str:
    start = SRC.index(head)
    return SRC[start:SRC.index("\n}\n", start)]


def test_preview_and_task_mask_use_the_same_layout_function() -> None:
    assert "maskLayout(" in _body("async function promptComplete(")
    assert "return maskLayout(fields.map(" in _body("function openFormDesigner(")


def test_mask_layout_spans_the_whole_form_grid() -> None:
    assert re.search(r"\.form-grid > \.mask-layout \{ grid-column: 1 / -1; \}", STYLES)
