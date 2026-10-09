# SPDX-License-Identifier: BUSL-1.1
"""Im Masken-Designer wirkt der erste Klick nach einer Eingabe.

Das Feld „Gruppe“ baute bei ``change`` den ganzen Designer neu. ``change``
feuert beim Verlassen des Felds -- also schon beim Drücken auf „+ Feld
hinzufügen“; der Knopf wurde vor dem Loslassen ersetzt, der Klick ging ins
Leere. Jetzt erneuert die Gruppe nur die Vorschau.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js


@needs_node
def test_group_change_keeps_the_add_button_and_updates_the_preview(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
state.schema = { id: "s", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { a: { id: "a", type: "ACTIVITY", label: "Erfassen" } }, edges: [],
  data_elements: { betrag: { id: "betrag", name: "Betrag", data_type: "INTEGER",
    source: "INSTANCE" } },
  data_accesses: [], forms: {}, staff_rules: {}, service_bindings: {} };
state.schemaId = "s";
openFormDesigner("a");
const root = byId("modal-root");
const addBtn = () => root.querySelectorAll("button").find((b) => textOf(b).includes("Feld hinzuf"));
if (!root.querySelector(".fd-field")) await addBtn().click();
const before = addBtn();
const group = root.querySelectorAll(".fd-cell").find((c) => textOf(c).startsWith("Gruppe"))
  .querySelector("input");
group.value = "Kopfdaten";
await group.dispatch("input");
await group.dispatch("change");      // Fokus verlässt das Feld (Mausdruck auf den Knopf)
const same = addBtn() === before;     // der Knopf wurde nicht ersetzt
await before.click();
return { same, fields: root.querySelectorAll(".fd-field").length,
  preview: textOf(root.querySelector(".fd-preview")) };
""",
        tmp_path,
    )
    assert res["same"] is True
    assert res["fields"] == 2
    assert "Kopfdaten" in res["preview"]
