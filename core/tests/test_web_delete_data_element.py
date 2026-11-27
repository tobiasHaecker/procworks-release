# SPDX-License-Identifier: BUSL-1.1
"""Die Rueckfrage beim Loeschen eines Datenelements nennt, was mitgeht.

Vorher hiess es pauschal „alle zugehoerigen Lese-/Schreibbindungen und
Maskenfelder“ -- welche Schritte betroffen sind, sah man nicht. Jetzt listet
der Dialog jeden Schritt mit seiner Bindung (Richtung) bzw. seinem
Maskenfeld. Nur Anzeige aus dem geladenen Modell; geloescht wird weiterhin vom
Kern. Geprueft in Node (``web_vm.py``) am echten Dialog.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_SCHEMA = r"""
state.schemaId = "s";
state.schema = { id: "s", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { a: { id: "a", type: "ACTIVITY", label: "Antrag erfassen" },
           b: { id: "b", type: "ACTIVITY", label: "Antrag prüfen" },
           c: { id: "c", type: "ACTIVITY", label: "Auszahlen" } },
  edges: [],
  data_elements: {
    betrag: { id: "betrag", name: "Betrag", data_type: "INTEGER", source: "INSTANCE" },
    frei: { id: "frei", name: "Notiz", data_type: "STRING", source: "INSTANCE" } },
  data_accesses: [
    { node_id: "a", element_id: "betrag", mode: "WRITE", optional: false },
    { node_id: "b", element_id: "betrag", mode: "READ", optional: false },
    { node_id: "c", element_id: "frei", mode: "READ", optional: true } ],
  forms: { b: { title: "", columns: 1, fields: [
    { element_id: "betrag", widget: "NUMBER", label: "Beantragter Betrag", mode: "READ" } ] } },
  staff_rules: {}, service_bindings: {} };
const dialogText = (id) => {
  deleteDataElement(state.schema.data_elements[id]);
  const t = textOf(byId("modal-root"));
  clear(byId("modal-root"));
  return t;
};
const items = () => byId("modal-root").querySelectorAll("li").map((li) => textOf(li));
"""


@needs_node
def test_delete_dialog_lists_every_affected_step_with_its_binding(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
deleteDataElement(state.schema.data_elements.betrag);
return { items: items(), text: textOf(byId("modal-root")) };
""",
        tmp_path,
    )
    assert res["items"] == [
        "Antrag erfassen: schreibt",
        "Antrag prüfen: liest, Maskenfeld „Beantragter Betrag“",
    ]
    # Ein Schritt, der nur ein anderes Element bindet, steht nicht in der Liste.
    assert "Auszahlen" not in res["text"]


@needs_node
def test_element_without_bindings_asks_without_a_list(tmp_path: Path) -> None:
    """Grenzfall: Ohne Bindung keine (leere) Liste, sondern nur die Frage."""
    res = run_app_js(
        _SCHEMA
        + r"""
state.schema.data_accesses = state.schema.data_accesses.filter((a) => a.element_id !== "frei");
deleteDataElement(state.schema.data_elements.frei);
return { items: items(), lists: byId("modal-root").querySelectorAll("ul").length,
         text: textOf(byId("modal-root")) };
""",
        tmp_path,
    )
    assert res["items"] == [] and res["lists"] == 0
    assert "„Notiz“" in res["text"] and "keinem Schritt" in res["text"]
