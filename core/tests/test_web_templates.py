# SPDX-License-Identifier: BUSL-1.1
"""Eigene Vorlagen lassen sich in der Galerie wieder löschen.

Die Anleitung versprach es, die Oberfläche bot keinen Weg. Eingebaute Vorlagen
bleiben (der Kern lehnt sie ab, siehe ``test_templates.py``).
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_TEMPLATES = r"""
state.principal = { subject: "mara", roles: ["modeler"] };
let list = [
  { id: "tpl-urlaubsantrag", name: "Urlaubsantrag", origin: "BUILTIN", category: "HR" },
  { id: "usr-1", name: "Meine Vorlage", origin: "USER", category: "Eigene" },
];
respond((p, m) => {
  if (m === "DELETE" && p === "/templates/usr-1") { list = list.filter((t) => t.id !== "usr-1");
    return { status: 204 }; }
  if (p === "/templates") return { body: list };
  return { status: 404 };
});
"""


@needs_node
def test_only_user_templates_offer_delete_and_it_reopens_the_gallery(tmp_path: Path) -> None:
    res = run_app_js(
        _TEMPLATES
        + r"""
await newFromTemplate();
const root = byId("modal-root");
const dels = root.querySelectorAll("button").filter((b) => textOf(b) === "Löschen");
const before = { dels: dels.length, cards: root.querySelectorAll(".tpl-card").length };
await dels[0].click();                                   // Rückfrage
const confirm = root.querySelectorAll("button").find((b) => b.classList.contains("danger"));
await confirm.click();
await new Promise((r) => setTimeout(r, 0));
return { before, calls, title: textOf(root.querySelector("h3")),
  cards: root.querySelectorAll(".tpl-card").length };
""",
        tmp_path,
    )
    assert res["before"] == {"dels": 1, "cards": 2}
    assert "DELETE /templates/usr-1" in res["calls"]
    assert res["title"] == "Aus Vorlage erstellen" and res["cards"] == 1


@needs_node
def test_selection_marks_one_card_across_the_whole_gallery(tmp_path: Path) -> None:
    res = run_app_js(
        _TEMPLATES
        + r"""
await newFromTemplate();
const cards = byId("modal-root").querySelectorAll(".tpl-card");
await cards[0].click();
await cards[1].click();                                  // eigene Vorlage in Hülle
return cards.map((c) => c.classList.contains("selected"));
""",
        tmp_path,
    )
    assert res == [False, True]
