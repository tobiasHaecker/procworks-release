# SPDX-License-Identifier: BUSL-1.1
"""Ein Leser bekommt keine Aktionen angeboten, die der Kern ablehnt.

``vera.viewer`` sah in der Vorgangsansicht „Als Aufsicht abschließen“ und
„Daten eingeben“, füllte die Maske aus und scheiterte erst dann mit 403. Die
Oberfläche zeigt jetzt nur, was die Rollenregeln des Kerns zulassen; ob ein
Eingriff im Einzelfall erlaubt ist, entscheidet weiter allein der Kern.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import APP_JS, needs_node, run_app_js

_CASE = r"""
const inst = { id: "i1", state: "RUNNING", is_test: false };
const schema = { staff_rules: { n1: { kind: "ROLE", ref: "r" } } };
const label = (x) => (x ? textOf(x) : null);
const out = {};
for (const [who, roles, agent] of [["viewer", ["viewer"], null],
    ["operator", ["operator"], "a1"], ["admin", ["admin"], null]]) {
  state.principal = { subject: who, roles, agent_id: agent };
  out[who] = {
    staffed: label(completionActionFor(inst, "n1", { label: "Prüfen" }, ["a1"], schema)),
    unstaffed: label(completionActionFor(inst, "n1", { label: "Prüfen" }, [], schema)),
  };
}
return out;
"""


@needs_node
def test_viewer_gets_no_completion_action(tmp_path: Path) -> None:
    res = run_app_js(_CASE, tmp_path)
    assert res["viewer"] == {"staffed": None, "unstaffed": None}
    # Gegenproben: Bearbeiter schließt eigene Aufgabe ab, Admin als Aufsicht.
    assert res["operator"]["staffed"] == "Abschließen"
    assert res["admin"]["staffed"] == "Als Aufsicht abschließen"
    assert res["admin"]["unstaffed"] == "Als Aufsicht abschließen"


def test_data_entry_button_follows_the_core_roles() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("const canEditData =")
    guard = src[start:src.index(";", start)]
    assert 'hasRole("operator", "modeler", "admin")' in guard  # Schreibrolle
    assert ('hasRole("modeler", "admin") || inst.is_test || mayActForOthers() || ownOpenStep'
            in guard)  # wie _authorize_data_write: Aufsicht, Test, Token/Integration, eigene
    own = src[src.index("const ownOpenStep ="):start]
    assert 'hasRole("operator")' in own and "includes(meAgent)" in own
