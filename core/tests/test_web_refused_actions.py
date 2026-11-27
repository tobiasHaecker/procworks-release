# SPDX-License-Identifier: BUSL-1.1
"""Keine Aktion, die der Kern sicher ablehnt -- und Aufraeumen nach einer Ablehnung.

* **„Neue Revision“ an einer aelteren Version:** Der Kern lehnt eine Revision
  ab, wenn die Familie schon eine hoehere freigegebene Version
  (``LC.not-latest``) oder eine offene Revision mit hoeherer Nummer
  (``LC.open-revision``) hat. Der Knopf erscheint dann gesperrt mit Erklaerung
  (aus der geladenen Prozessliste: Version, Status, ``revision_of``).
* **„Als Aufsicht abschliessen“ bei Vier-Augen-Bezug:** Richtet sich ein
  anderer Schritt nach der Person, die diesen erledigt, lehnt der Kern den
  Aufsichtsabschluss ohne Bearbeiter ab (``SUP.four-eyes`` /
  ``SUP.performer-needed``). Der Knopf erscheint gesperrt mit Erklaerung.
* **Nach so einer Ablehnung** schliesst der Abschlussdialog, statt hinter der
  Meldung offen zu bleiben (dort, wo der Client die Regel nicht kennt, etwa in
  „Meine Aufgaben“ der Aufsicht).

Nur Anzeige: Der Kern bleibt maßgeblich. Geprueft im Node-``vm``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_vm import needs_node, run_app_js

# v1 -> v2a (freigegeben), v1 -> v2b (Altbestand, freigegeben), v2a -> v3 (Entwurf);
# fremd: eine andere Familie mit Version 5.
_BLOCK_INFO = r"""
// Ein gesperrter Knopf: aria-disabled (fokussierbar, kein ``disabled``), der
// Grund als Text eines Elements, auf das ``aria-describedby`` zeigt.
const blockInfo = (w) => {
  const b = w.tagName === "BUTTON" ? w : w.querySelector("button");
  const ref = b.getAttribute("aria-describedby");
  const desc = ref ? w.querySelector(".sr-only") : null;
  return { disabled: b.getAttribute("aria-disabled") === "true",
           focusable: !b.hasAttribute("disabled"),
           described: !!desc && desc.getAttribute("id") === ref,
           reason: desc ? textOf(desc) : "",
           title: b.getAttribute("title") || "", text: textOf(b), node: b };
};
"""

_FAMILY = _BLOCK_INFO + r"""
state.principal = { subject: "mara", roles: ["modeler"] };
const rows = [
  ["v1", 1, "RELEASED", null], ["v2a", 2, "RELEASED", "v1"], ["v2b", 2, "RELEASED", "v1"],
  ["v3", 3, "ENTWURF", "v2a"], ["x1", 1, "RELEASED", null], ["x5", 5, "RELEASED", "x1"],
];
state.schemaIds = rows.map((r) => r[0]);
for (const [id, v, s, of] of rows) {
  state.schemaNames[id] = "P"; state.schemaVersions[id] = v;
  state.schemaLifecycle[id] = s; state.schemaRevisionOf[id] = of;
}
const button = (id) => {
  state.schemaId = id;
  return blockInfo(revisionButton("btn small"));
};
"""


@needs_node
def test_older_versions_offer_the_revision_only_disabled_with_the_reason(tmp_path: Path) -> None:
    res = run_app_js(
        _FAMILY + 'const r = { v1: button("v1"), v2b: button("v2b") };'
        + " delete r.v1.node; delete r.v2b.node; return r;",
        tmp_path,
    )
    assert res["v1"]["disabled"] is True
    assert "neuere freigegebene Version (Version 2)" in res["v1"]["title"]
    # Der Altbestands-Zwilling: keine hoehere freigegebene, aber eine offene v3.
    assert res["v2b"]["disabled"] is True
    assert "offene Revision (Version 3" in res["v2b"]["title"]


@needs_node
def test_the_newest_version_and_other_families_stay_free(tmp_path: Path) -> None:
    """Negativ: Ohne Hoeheres in der eigenen Familie bleibt der Knopf frei --
    eine fremde Familie mit hoeherer Nummer zaehlt nicht."""

    res = run_app_js(
        _FAMILY
        + r"""
state.schemaLifecycle.v3 = "RELEASED";
const r = { v3: button("v3"), x5: button("x5") }; delete r.v3.node; delete r.x5.node;
return r;
""",
        tmp_path,
    )
    assert res["v3"]["disabled"] is False
    assert res["x5"]["disabled"] is False


@needs_node
def test_a_lineage_cycle_or_unknown_parent_does_not_hang(tmp_path: Path) -> None:
    """Grenzfall: Ein Zyklus oder ein unbekannter Vorgaenger beendet die Suche."""

    res = run_app_js(
        _FAMILY
        + r"""
state.schemaRevisionOf.v1 = "v2a";          // Zyklus v1 <-> v2a
state.schemaRevisionOf.x1 = "weg";          // unbekannter Vorgaenger
const r = { v1: button("v1"), x1: button("x1") }; delete r.v1.node; delete r.x1.node;
return r;
""",
        tmp_path,
    )
    assert isinstance(res["v1"]["disabled"], bool)
    assert res["x1"]["disabled"] is True  # x5 ist hoeher und freigegeben


_FOUR_EYES = _BLOCK_INFO + r"""
state.principal = { subject: "admin", roles: ["admin"] };
const schema = {
  nodes: { a: { id: "a", label: "Erfassen" }, b: { id: "b", label: "Pruefen" } },
  staff_rules: {
    a: { kind: "ROLE", ref: "sb", operands: [] },
    b: { kind: "EXCEPT", operands: [
      { kind: "ROLE", ref: "sb", operands: [] },
      { kind: "NODE_PERFORMING_AGENT", ref: "a", operands: [] } ] } },
};
const inst = { id: "i1", is_test: false };
const info = (b) => { const r = blockInfo(b); delete r.node; return r; };
"""


@needs_node
def test_supervision_is_offered_disabled_when_a_later_step_needs_the_performer(
    tmp_path: Path,
) -> None:
    res = run_app_js(
        _FOUR_EYES
        + 'return { a: info(completionActionFor(inst, "a", schema.nodes.a, ["x"], schema)),'
        + ' b: info(completionActionFor(inst, "b", schema.nodes.b, ["x"], schema)) };',
        tmp_path,
    )
    assert res["a"]["text"] == "Als Aufsicht abschließen"
    assert res["a"]["disabled"] is True
    assert "„Pruefen“" in res["a"]["title"] and "Vier-Augen" in res["a"]["title"]
    # Negativ: Den Vier-Augen-Schritt selbst darf die Aufsicht abschliessen.
    assert res["b"]["disabled"] is False


@needs_node
@pytest.mark.parametrize("code", ["SUP.four-eyes", "SUP.performer-needed"])
def test_a_refused_supervision_closes_the_dialog(tmp_path: Path, code: str) -> None:
    res = run_app_js(
        r"""
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
state.principal = { subject: "admin", roles: ["admin"] };
respond((p, m) => (m === "POST" && p === "/instances/i1/complete")
  ? { status: 422, body: { detail: { message: "x", code: "CODE",
                                     params: { step: "Erfassen", dependants: "Pruefen" } } } }
  : { status: 404 });
const schema = { nodes: { a: { label: "Erfassen" } }, forms: {}, data_elements: {},
                 staff_rules: {} };
await promptComplete(schema, "i1", "a", "Erfassen", null, null, {});
const root = byId("modal-root");
const confirm = root.querySelectorAll("button").find((b) => textOf(b) === "Abschließen");
await confirm.click();
await new Promise((r) => setTimeout(r, 5));
return { open: root.children.length, toast: textOf(byId("toast-root")) };
""".replace("CODE", code),
        tmp_path,
    )
    assert res["open"] == 0, res
    assert "Erfassen" in res["toast"]


@needs_node
def test_another_refusal_keeps_the_dialog_open(tmp_path: Path) -> None:
    """Negativ: Eine Ablehnung, die eine Eingabe beheben kann (D1), laesst den
    Dialog offen."""

    res = run_app_js(
        r"""
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
respond(() => ({ status: 422, body: { detail: { findings: [
  { rule: "D1", code: "D1.write-missing", message: "x", params: { element: "Betrag" } }] } } }));
const schema = { nodes: { a: { label: "Erfassen" } }, forms: {}, data_elements: {},
                 staff_rules: {} };
await promptComplete(schema, "i1", "a", "Erfassen", null, null, {});
const root = byId("modal-root");
await root.querySelectorAll("button").find((b) => textOf(b) === "Abschließen").click();
await new Promise((r) => setTimeout(r, 5));
return root.children.length;
""",
        tmp_path,
    )
    assert res == 1


@needs_node
def test_a_blocked_button_stays_reachable_and_names_its_reason(tmp_path: Path) -> None:
    """Gesperrt heisst nicht unerreichbar: Ein ``disabled``-Knopf ist nicht
    fokussierbar, sein ``title`` erreichen Tastatur und Screenreader nicht.
    Der Grund steht deshalb in einem Element, auf das ``aria-describedby``
    zeigt, und ein Klick nennt ihn, statt etwas auszuloesen."""

    res = run_app_js(
        _FAMILY
        + r"""
const calls = [];
respond((p, m) => { calls.push(m + " " + p); return { body: {} }; });
const v1 = button("v1");
await v1.node.click();
const toastText = textOf(byId("toast-root"));
const sup = blockInfo(completionActionFor(
  { id: "i1", is_test: false }, "a", { id: "a", label: "Erfassen" }, ["x"],
  { nodes: { a: { id: "a", label: "Erfassen" }, b: { id: "b", label: "Pruefen" } },
    staff_rules: { b: { kind: "EXCEPT", operands: [
      { kind: "ROLE", ref: "sb", operands: [] },
      { kind: "NODE_PERFORMING_AGENT", ref: "a", operands: [] } ] } } }));
delete v1.node; delete sup.node;
return { v1, sup, calls, toastText };
""",
        tmp_path,
    )
    for info in (res["v1"], res["sup"]):
        assert info["disabled"] is True and info["focusable"] is True
        assert info["described"] is True and info["reason"] == info["title"]
    assert "neuere freigegebene Version" in res["v1"]["reason"]
    assert "Vier-Augen" in res["sup"]["reason"]
    # Der Klick loest nichts aus, sondern nennt den Grund.
    assert res["calls"] == []
    assert "neuere freigegebene Version" in res["toastText"]


@needs_node
def test_two_blocked_buttons_have_their_own_description(tmp_path: Path) -> None:
    """Grenzfall: Kopfzeile und Inspektor zugleich -- je eine eigene Kennung."""

    res = run_app_js(
        _FAMILY
        + r"""
state.schemaId = "v1";
const a = revisionButton("btn small"); const b = revisionButton("btn small");
const ref = (w) => w.querySelector("button").getAttribute("aria-describedby");
return { a: ref(a), b: ref(b) };
""",
        tmp_path,
    )
    assert res["a"] and res["b"] and res["a"] != res["b"]


@needs_node
def test_the_released_hint_does_not_advise_a_blocked_revision(tmp_path: Path) -> None:
    """Die Hinweise freigegebener Versionen raten nur dort zur neuen Revision,
    wo sie moeglich ist; sonst nennen sie den Grund."""

    res = run_app_js(
        _FAMILY
        + r"""
state.schemaId = "v1"; const blocked = releasedEditHint();
state.schemaId = "x5"; const free = releasedEditHint();
return { blocked, free };
""",
        tmp_path,
    )
    assert "neue Revision anlegen" not in res["blocked"]
    assert "neuere freigegebene Version (Version 2)" in res["blocked"]
    assert "neue Revision anlegen" in res["free"]
