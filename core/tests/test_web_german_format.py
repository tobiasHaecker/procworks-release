# SPDX-License-Identifier: BUSL-1.1
"""Zahlen, Größen, Daten und Zähler erscheinen deutsch.

„Ø Durchlaufzeit 7.6 h“, „20.0 KB“, „2026-09-30 – 2026-10-07“, „1 Einträge“
und ein Nur-Lese-Betrag „1500“ statt „1.500,00“.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_vm import APP_JS, needs_node, run_app_js


@needs_node
def test_formatters_are_german(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
return {
  dur: [fmtDuration(27360), fmtDuration(90), fmtDuration(42.5), fmtDuration(0.2)],
  bytes: [fmtBytes(20480), fmtBytes(512), fmtBytes(1572864)],
  date: fmtDate("2026-09-30T00:00:00Z"),
  counts: [countLabel(0, "Eintrag", "Einträge"), countLabel(1, "Eintrag", "Einträge"),
           countLabel(2, "Eintrag", "Einträge")],
  num: fmtNumber(1234.5, 1),
  money: formatValue({ data_type: "DECIMAL" }, 1500),
};
""",
        tmp_path,
    )
    assert res["dur"] == ["7,6 h", "1,5 min", "42,5 s", "< 1 s"]
    assert res["bytes"] == ["20,0 KB", "512 B", "1,5 MB"]
    assert res["date"] == "30.09.2026"
    assert res["counts"] == ["0 Einträge", "1 Eintrag", "2 Einträge"]
    assert res["num"] == "1.234,5"
    assert res["money"] == "1.500,00"


@needs_node
def test_read_only_amount_in_a_task_mask_is_formatted(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const schema = { nodes: { a: { id: "a", type: "ACTIVITY", label: "Prüfen" } },
  data_elements: { betrag: { id: "betrag", name: "Betrag", data_type: "DECIMAL",
    source: "INSTANCE" } },
  forms: { a: { title: "", columns: 1, fields: [{ element_id: "betrag", widget: "NUMBER",
    label: "Betrag", mode: "READ", options: [], required: false }] } },
  data_accesses: [], staff_rules: {} };
promptComplete(schema, "i1", "a", "Prüfen", null, async () => {}, { betrag: 1500 });
const input = byId("modal-root").querySelector("input");
return { type: input.getAttribute("type"), value: input.value };
""",
        tmp_path,
    )
    assert res == {"type": "text", "value": "1.500,00"}


def test_display_formatters_do_not_use_tofixed() -> None:
    """Wächter über die Klasse: ``toFixed`` liefert einen Dezimalpunkt."""
    src = APP_JS.read_text(encoding="utf-8")
    assert not re.search(r"\.toFixed\(", src), "toFixed in app.js -- fmtNumber verwenden"


def test_no_bracketed_plural_in_visible_texts() -> None:
    """Wächter über die Klasse: „Schritt(e)“, „Aufgabe(n)“, „Feld(er)“ … statt countLabel."""
    src = APP_JS.read_text(encoding="utf-8")
    # Großgeschriebenes Wort mit Klammer-Endung hinter einer Zahl oder einem
    # Platzhalter („3 Schritt(e)“, „${n} Aufgabe(n)“, „… laufende Instanz(en)“);
    # Funktionsaufrufe wie fmtBytes(n) zählen nicht.
    hits = sorted(set(re.findall(r"(?:\d|\}|[a-zäöü]) [A-ZÄÖÜ][a-zäöüß]+\((?:e|n|en|er|s)\)", src)))
    assert not hits, "Klammer-Plural im Text: " + ", ".join(hits)
