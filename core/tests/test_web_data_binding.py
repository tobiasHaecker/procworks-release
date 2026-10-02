# SPDX-License-Identifier: BUSL-1.1
"""Alle Wege, eine Datenbindung anzulegen, sind gleich mächtig.

Die Datensicht hatte einen eigenen, schwächeren Dialog: Richtung als
READ/WRITE, immer Pflicht -- die Abhilfe der D1-Meldung („nicht als Pflicht
setzen“) war dort unmöglich. Jetzt laufen Schritt-Karte, Palette und Datensicht
über denselben Dialog und denselben einen API-Aufruf. Dass der Kern optionales
Lesen ohne Schreiber annimmt, sichert ``test_dataflow.py``
(``test_optional_read_does_not_require_supply``).
"""

from __future__ import annotations

import re
from pathlib import Path

from web_vm import APP_JS, needs_node, run_app_js


def test_every_data_binding_goes_through_one_function() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    pattern = r"api\.post\(`/schemas/\$\{\w+\}/data-access`"
    posts = [m.start() for m in re.finditer(pattern, src)]
    assert len(posts) == 1, f"{len(posts)} Stellen legen Datenbindungen an, erlaubt ist eine"
    owner = src.rfind("\nfunction ", 0, posts[0])
    assert src[owner:owner + 40].startswith("\nfunction createDataAccess("), (
        "Der einzige data-access-Aufruf gehoert in createDataAccess")


_SCHEMA = r"""
state.schema = { id: "s1", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { start: { id: "start", type: "START" },
           a: { id: "a", type: "ACTIVITY", label: "Prüfen" },
           end: { id: "end", type: "END" } },
  edges: [{ source: "start", target: "a" }, { source: "a", target: "end" }],
  data_elements: { betrag: { id: "betrag", name: "Betrag", data_type: "DECIMAL",
                             source: "INSTANCE" } },
  data_accesses: [], staff_rules: {}, service_bindings: {} };
state.schemaId = "s1";
const posted = [];
respond((p, m, body) => {
  if (m === "POST" && p === "/schemas/s1/data-access") {
    posted.push(body);
    return { body: state.schema };
  }
  return { body: state.schema };
});
globalThis.refreshSchema = async () => {}; globalThis.render = () => {};
"""


@needs_node
def test_data_view_binding_offers_direction_and_optional(tmp_path: Path) -> None:
    """Datensicht: Schritt wählen → Bindungsdialog mit deutscher Richtung und
    Pflicht/optional; optional gebundenes Lesen geht als mandatory=false raus."""
    res = run_app_js(
        _SCHEMA
        + r"""
addDataAccess();
const root = byId("modal-root");
await root.querySelector("button.btn.primary").click();           // „Weiter“
const title = textOf(root.querySelector("h3"));
const options = root.querySelectorAll("option").map((o) => textOf(o));
const sel = root.querySelectorAll("select")
  .find((s) => s.children.some((o) => o.getAttribute("value") === "READ"));
sel.value = "READ";
const box = root.querySelectorAll("input")
  .find((i) => i.getAttribute("type") === "checkbox");
box.checked = false;
await root.querySelector("button.btn.primary").click();           // „Binden“
return { title, options, posted };
""",
        tmp_path,
    )
    assert res["title"].startswith("Datenelement an „Prüfen")
    assert "Lesen (liest den Wert)" in res["options"] and "READ" not in res["options"]
    assert res["posted"] == [
        {"node_id": "a", "element_id": "betrag", "mode": "READ", "mandatory": False}
    ]


@needs_node
def test_palette_drop_opens_the_same_dialog_with_element_preselected(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
dropDataElementOnNode("a", { element_id: "betrag", name: "Betrag" });
const root = byId("modal-root");
return { title: textOf(root.querySelector("h3")),
  hasMandatory: textOf(root).includes("Pflichtbindung") };
""",
        tmp_path,
    )
    assert res["title"].startswith("Datenelement an „Prüfen")
    assert res["hasMandatory"] is True
