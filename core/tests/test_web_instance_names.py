# SPDX-License-Identifier: BUSL-1.1
"""Vorgänge heißen in der Oberfläche nach ihren Werten oder ihrem Start.

Monitoring, Arbeitslisten, Ausführung, Prüfinstanz und Migrationsassistent
zeigten ``instance_13``; zwei gleiche Aufgaben verschiedener Vorgänge waren
nicht zu unterscheiden. Die Kennung steht seither nur noch klein darunter.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_source import app_js_source
from web_vm import needs_node, run_app_js


@needs_node
def test_name_prefers_values_then_start_never_the_bare_id(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const vals = [{ name: "kunde", value: "Müller GmbH" }, { name: "nr", value: "A-4711" }];
const cell = instanceNameCell("instance_13", "2026-10-01T12:03:00Z", []);
return {
  values: instanceName("2026-10-01T12:03:00Z", vals),
  start: instanceName("2026-10-01T12:03:00Z", []),
  none: instanceName(null, undefined),
  cellText: textOf(cell), cellTitle: cell.getAttribute("title"),
};
""",
        tmp_path,
    )
    assert res["values"] == "Müller GmbH · A-4711"
    assert res["start"].startswith("Vorgang vom ") and "instance" not in res["start"]
    assert res["none"] == "Vorgang"
    assert res["cellText"].startswith("Vorgang vom ") and res["cellText"].endswith("instance_13")
    assert res["cellTitle"] == "Kennung: instance_13"


@needs_node
def test_numbers_and_yes_no_carry_their_field_name(tmp_path: Path) -> None:
    """Eine nackte „8“ sagt nichts: Zahlen und Ja/Nein tragen den Feldnamen;
    formatiert wird nach Datentyp (Betrag immer mit Punkt, Kennnummer ohne)."""
    res = run_app_js(
        r"""
return contextTitle([
  { name: "Begründung", value: "Sommerurlaub" },
  { name: "Urlaubstage", value: 8, data_type: "INTEGER" },
  { name: "Budget genehmigt", value: false, data_type: "BOOLEAN" },
  { name: "Auftragswert", value: 12000, data_type: "DECIMAL" },
  { name: "Gewicht", value: 8880.5, data_type: "FLOAT" },
  { name: "Kundennummer", value: 10044, data_type: "INTEGER" },
]);
""",
        tmp_path,
    )
    assert res == ("Sommerurlaub · Urlaubstage: 8 · Budget genehmigt: Nein · "
                   "Auftragswert: 12.000,00 · Gewicht: 8.880,5 · Kundennummer: 10044")


@needs_node
def test_two_equal_first_steps_are_distinguishable_by_start(tmp_path: Path) -> None:
    """Gleicher Schritt, gleicher Prozess, noch keine Werte: die Startzeit trennt."""
    res = run_app_js(
        r"""
return [instanceName("2026-10-01T08:15:00Z", []), instanceName("2026-10-01T12:40:00Z", [])];
""",
        tmp_path,
    )
    assert res[0] != res[1]


def test_no_view_renders_a_bare_instance_id_as_name() -> None:
    """Quelltext-Wächter über die bekannten Muster einer nackten Kennung."""
    src = app_js_source()
    bare = [
        r'el\("td", null, \w+\.instance_id\)',
        r'\{ class: "sub" \}, inst\.id\)',
        r'gestartet", \[inst\.id\]\)',
        r': i\.id;',
    ]
    found = [p for p in bare if re.search(p, src)]
    assert not found, "nackte Vorgangskennung als Name: " + ", ".join(found)
    caption = src[src.index("function instanceCaption("):]
    caption = caption[:caption.index("\n}\n")]
    assert "inst.id" not in caption, "instanceCaption faellt auf die Kennung zurueck"
