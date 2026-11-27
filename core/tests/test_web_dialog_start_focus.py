# SPDX-License-Identifier: BUSL-1.1
"""Ein Dialog setzt den Startfokus nur auf ein Feld, das ihn annehmen kann.

``openModal`` suchte das erste Feld mit ``input:not([readonly]), select,
textarea`` -- auch ein gesperrtes (``disabled``). Beginnt eine Aufgabenmaske
mit einem Lesefeld (die Entscheidungsgrundlage steht oben), nahm dieses Feld
den Fokus nicht an: Er blieb auf ``<body>``, Escape erreichte den Dialog nicht
(sein Tastenweg haengt am Dialog), erst Tab fuehrte hinein. Jetzt ueberspringt
der Startfokus gesperrte, schreibgeschuetzte und versteckte Felder
(``firstEditableField``), sonst „Bestaetigen“ wie bisher.

Geprueft im Node-``vm`` (``web_vm.py``) mit dem echten ``openModal`` und der
echten Aufgabenmaske (``promptComplete``).
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_DOC = r"""
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
const focusInfo = () => {
  const a = document.activeElement;
  return { tag: a ? a.tagName : null, type: a && a.getAttribute ? a.getAttribute("type") : null,
           name: a && a.getAttribute ? a.getAttribute("name") : null,
           text: a && a.tagName === "BUTTON" ? textOf(a) : null,
           inDialog: !!(a && byId("modal-root").contains(a)) };
};
"""


@needs_node
def test_task_mask_starting_with_a_read_field_focuses_the_first_editable_one(
    tmp_path: Path,
) -> None:
    """Die Maske beginnt mit einem Lesefeld (gesperrt); der Fokus liegt im
    Dialog auf dem ersten bearbeitbaren Feld, und Escape schliesst."""

    res = run_app_js(
        _DOC
        + r"""
const schema = { id: "s", name: "Versand", version: 1,
  nodes: { a: { id: "a", type: "ACTIVITY", label: "Kunde informieren" } }, edges: [],
  data_elements: {
    track: { id: "track", name: "Sendungsverfolgung", data_type: "STRING", source: "INSTANCE" },
    done: { id: "done", name: "Informiert", data_type: "BOOLEAN", source: "INSTANCE" } },
  data_accesses: [], staff_rules: {}, service_bindings: {},
  forms: { a: { node_id: "a", title: "", columns: 1, fields: [
    { id: "f1", element_id: "track", widget: "TEXT", label: "Sendungsverfolgung", mode: "READ",
      options: [] },
    { id: "f2", element_id: "done", widget: "CHECKBOX", label: "Informiert", mode: "WRITE",
      options: [] } ] } } };
respond(() => ({ body: {} }));
promptComplete(schema, "i1", "a", "Kunde informieren", null, () => {}, { track: "DE123" });
const root = byId("modal-root");
const read = root.querySelectorAll("input").find((x) => x.hasAttribute("disabled"));
const start = focusInfo();
const modal = root.querySelector(".modal-backdrop");
await modal.dispatch("keydown", { key: "Escape", target: document.activeElement });
return { readIsDisabled: !!read, start, closed: root.children.length === 0 };
""",
        tmp_path,
    )
    assert res["readIsDisabled"] is True
    assert res["start"]["inDialog"] is True, res
    assert res["start"]["type"] == "checkbox", res
    assert res["closed"] is True


@needs_node
def test_start_focus_skips_disabled_readonly_and_hidden_fields(tmp_path: Path) -> None:
    """Negativ: Weder ein gesperrtes noch ein schreibgeschuetztes noch ein
    verstecktes Feld bekommt den Startfokus -- sondern das erste, in das man
    tippen kann."""

    res = run_app_js(
        _DOC
        + r"""
const body = el("div", null,
  el("input", { name: "gesperrt", disabled: "disabled" }),
  el("input", { name: "nurlesen", readonly: "readonly" }),
  el("input", { name: "versteckt", type: "hidden" }),
  el("select", { name: "gesperrtewahl", disabled: "" }),
  el("textarea", { name: "notiz" }));
openModal("Probe", body, () => true, "OK");
return focusInfo();
""",
        tmp_path,
    )
    assert res == {"tag": "TEXTAREA", "type": None, "name": "notiz", "text": None,
                   "inDialog": True}


@needs_node
def test_without_an_editable_field_the_confirm_button_takes_the_focus(tmp_path: Path) -> None:
    """Grenzfall: Sind alle Felder gesperrt, liegt der Fokus auf „Bestaetigen“
    (nie ``<body>``); bei einer zerstoerenden Aktion auf „Abbrechen“."""

    res = run_app_js(
        _DOC
        + r"""
const only = () => el("div", null, el("input", { name: "a", disabled: "" }),
                       el("textarea", { name: "b", readonly: "" }));
openModal("Probe", only(), () => true, "OK");
const plain = focusInfo();
openModal("Loeschen", only(), () => true, "Loeschen", { danger: true });
const danger = focusInfo();
return { plain: plain.text, danger: danger.text, inDialog: plain.inDialog && danger.inDialog };
""",
        tmp_path,
    )
    assert res == {"plain": "OK", "danger": "Abbrechen", "inDialog": True}
