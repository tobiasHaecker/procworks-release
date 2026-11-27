# SPDX-License-Identifier: BUSL-1.1
"""Der Kritikalitaets-Filter der Arbeitsliste nennt genau die Baender, die er zeigt.

Der Filter "Nur kritische" zeigte "gefaehrdet" und "ueberfaellig", hiess aber
"wird knapp + ueberfaellig" -- Text und Filterung waren getrennt gepflegt und
liefen auseinander. Seitdem kommen Filterung, Auswahlliste und Leer-Hinweis
aus ``TASK_FILTERS``. Geprueft wird das Verhalten von ``web/app.js`` in Node (siehe ``web_vm.py``).
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

# Je eine Aufgabe pro Band, dazu eine ohne Soll-Zeit und eine ohne Feld.
_TASKS = r"""
const tasks = [
  { node_id: "on", time_criticality: "ON_TRACK" },
  { node_id: "warn", time_criticality: "WARNING" },
  { node_id: "risk", time_criticality: "AT_RISK" },
  { node_id: "over", time_criticality: "OVERDUE" },
  { node_id: "none", time_criticality: "NONE" },
  { node_id: "bare" },
];
const ids = (key) => filterTasksByCriticality(tasks, key).map((t) => t.node_id);
"""


@needs_node
def test_critical_filter_label_names_exactly_the_bands_it_shows(tmp_path: Path) -> None:
    """Positiv: Beschriftung und gezeigte Aufgaben stimmen fuer jeden Filter ueberein."""
    result = run_app_js(_TASKS + r"""
return {
  critical: ids("critical"), criticalLabel: taskFilterLabel("critical"),
  overdue: ids("overdue"), overdueLabel: taskFilterLabel("overdue"),
};
""", tmp_path)

    assert result["critical"] == ["risk", "over"]
    assert result["criticalLabel"] == "Nur kritische (gefährdet + überfällig)"
    assert result["overdue"] == ["over"]
    assert result["overdueLabel"] == "Nur überfällige"


@needs_node
def test_critical_filter_neither_shows_nor_names_warning(tmp_path: Path) -> None:
    """Negativ: "wird knapp" (WARNING) ist kein kritisches Band -- weder in der
    Liste noch im Text. Genau dieser Widerspruch war der Fehler."""
    result = run_app_js(_TASKS + r"""
return { critical: ids("critical"), label: taskFilterLabel("critical"),
         warningLabel: CRITICALITY_LABELS.WARNING };
""", tmp_path)

    assert "warn" not in result["critical"], "WARNING darf der Filter nicht zeigen"
    assert result["warningLabel"] not in result["label"], (
        "der Text nennt ein Band, das der Filter gar nicht zeigt"
    )


@needs_node
def test_unknown_or_all_filter_keeps_every_task(tmp_path: Path) -> None:
    """Grenzfall: "all" und ein veralteter Schluessel aus localStorage leeren die
    Liste nicht; Aufgaben ohne Band fallen nur bei echten Filtern heraus."""
    result = run_app_js(_TASKS + r"""
return { all: ids("all"), stale: ids("kritisch-alt"),
         staleLabel: taskFilterLabel("kritisch-alt") };
""", tmp_path)

    everything = ["on", "warn", "risk", "over", "none", "bare"]
    assert result["all"] == everything
    assert result["stale"] == everything
    assert result["staleLabel"] == "Alle Aufgaben"


@needs_node
def test_prototype_keys_from_storage_do_not_break_the_worklist(tmp_path: Path) -> None:
    """Negativ: Ein gespeicherter Wert wie "constructor" traf frueher die
    Prototyp-Kette des Filterobjekts; er muss wie jeder unbekannte Wert auf
    "all" fallen, statt beim Filtern eine Ausnahme zu werfen."""
    result = run_app_js(_TASKS + r"""
const keys = ["constructor", "toString", "__proto__", "hasOwnProperty", null];
return keys.map((k) => ({ norm: normalizeTaskFilter(k), shown: ids(k).length,
                          label: taskFilterLabel(k) }));
""", tmp_path)

    for entry in result:
        assert entry == {"norm": "all", "shown": 6, "label": "Alle Aufgaben"}


@needs_node
def test_every_filter_has_the_word_forms_its_texts_use(tmp_path: Path) -> None:
    """Grenzfall: Auswahlliste und Leer-Hinweis lesen ihre Woerter aus derselben
    Tabelle -- jeder Filter braucht beide Formen und nur bekannte Baender."""
    result = run_app_js(r"""
return Object.entries(TASK_FILTERS).map(([key, f]) => ({
  key, word: f.word, emptyWord: f.emptyWord,
  unknownBands: f.bands.filter((b) => !CRITICALITY_LABELS[b]),
}));
""", tmp_path)

    assert [e["key"] for e in result] == ["critical", "overdue"]
    for entry in result:
        assert entry["word"] and entry["emptyWord"], entry["key"]
        assert entry["unknownBands"] == [], entry["key"]
