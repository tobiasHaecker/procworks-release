# SPDX-License-Identifier: BUSL-1.1
"""Die Aktionsspalte einer Tabelle ist als solche markiert.

Bei Tablet-Breite scrollen Tabellen in ihrem Panel. Die Knöpfe einer Zeile
(„Erledigen“, „Übernehmen“) standen in der letzten Spalte und lagen damit
ohne Hinweis außerhalb des sichtbaren Bereichs. ``table()`` markiert die
letzte Spalte ohne Überschrift mit ``col-actions``; styles.css heftet sie
rechts an (Wächter in ``test_web_topbar_layout.py``). Hier: welche Zellen die
Markierung bekommen -- im Node-``vm``, nicht per Regex.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_PROBE = r"""
const cls = (t) => ({
  head: t.querySelectorAll("th").map((c) => c.className || ""),
  cells: t.querySelectorAll("td").map((c) => c.className || ""),
  labels: t.querySelectorAll("td").map((c) => c.getAttribute("data-label")),
});
"""


@needs_node
def test_the_last_column_without_a_heading_is_the_action_column(tmp_path: Path) -> None:
    """Positiv: wie „Meine Aufgaben“ -- nur die letzte, leere Spalte."""

    res = run_app_js(_PROBE + r"""
return cls(table(["Aufgabe", "Fällig", ""], [["a", "b", el("button", null, "Erledigen")],
                                             ["c", "d", "e"]]));
""", tmp_path)
    assert res["head"] == ["", "", "col-actions"]
    assert res["cells"] == ["", "", "col-actions", "", "", "col-actions"]
    assert res["labels"] == ["Aufgabe", "Fällig", None, "Aufgabe", "Fällig", None]


@needs_node
def test_tables_without_an_action_column_stay_unmarked(tmp_path: Path) -> None:
    """Negativ: eine Tabelle mit Überschrift in der letzten Spalte, eine leere
    Überschrift in der Mitte und eine einspaltige Tabelle ohne Überschrift
    bekommen keine Markierung -- nur die Knopfspalte am Ende ist gemeint."""

    res = run_app_js(_PROBE + r"""
return [
  cls(table(["Element", "Wert"], [["a", "b"]])),
  cls(table(["A", "", "C"], [["a", "b", "c"]])),
  cls(table([""], [["a"]])),
];
""", tmp_path)
    for probe in res:
        assert set(probe["head"]) == {""} and set(probe["cells"]) == {""}, probe


@needs_node
def test_the_task_list_marks_its_buttons_as_the_action_column(tmp_path: Path) -> None:
    """Grenzfall: die echte Sicht -- die Knöpfe „Erledigen“/„Übernehmen“ der
    Aufgabenliste stehen in Zellen mit ``col-actions``."""

    res = run_app_js(r"""
state.principal = { subject: "erika", roles: ["operator"], agent_id: "a1", display_name: "Erika" };
state.view = "tasks";
byId("auth-overlay").style.display = "none";
document.body.appendChild(byId("content"));
respond((p) => {
  if (p === "/directory/agents") return { body: [{ agent_id: "a1", name: "Erika" }] };
  if (p === "/me/tasks") return { body: [{ instance_id: "i1", node_id: "n1", schema_id: "s1",
    label: "Antrag erfassen", eligible_agents: ["a1"] }] };
  if (p === "/monitoring/revision") return { body: { revision: 1 } };
  return { status: 404 };
});
render();
do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
const buttons = byId("content").querySelectorAll("button")
  .filter((b) => ["Erledigen", "Übernehmen"].includes(textOf(b)));
const cellOf = (n) => { while (n && n.tagName !== "TD") n = n.parentNode; return n; };
return buttons.map((b) => (cellOf(b) || {}).className || "");
""", tmp_path)
    assert res == ["col-actions", "col-actions"]
