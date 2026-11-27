# SPDX-License-Identifier: BUSL-1.1
"""The loop tab creates its repeat attribute itself, like the decision tab.

The insert dialog offered "new attribute" only for a decision. A loop over a
not yet existing yes/no attribute -- the case the guide recommends -- meant
cancelling the dialog and creating the element in the data view first. Both
tabs now use the same picker and the same create-run-clean-up function, so
both modelling surfaces (which open the same dialog) behave alike.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

# Ein Modell ohne jedes Datenelement: frueher war der Schleifen-Tab hier gesperrt.
_SCHEMA = r"""
const schema = { id: "s", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { start: { id: "start", type: "START" }, a: { id: "a", type: "ACTIVITY", label: "Prüfen" },
    end: { id: "end", type: "END" } },
  edges: [{ source: "start", target: "a", type: "CONTROL" },
    { source: "a", target: "end", type: "CONTROL" }],
  data_elements: {}, data_accesses: [], staff_rules: {}, service_bindings: {} };
state.schema = schema; state.schemaId = "s";
globalThis.refreshSchema = async () => {}; globalThis.render = () => {};
function openLoopTab() {
  openInsertModal("a");
  const root = byId("modal-root");
  document.body.appendChild(root);
  const tab = root.querySelectorAll("button").find((b) => textOf(b) === "Schleife");
  tab.click();
  return root;
}
/** Antwort des Kerns auf das Anlegen: das Schema mit dem neuen Element de_1. */
function created(b) {
  const de = { id: "de_1", name: b.name, data_type: b.data_type };
  return { body: Object.assign({}, schema, { data_elements: { de_1: de } }) };
}
/** Name/Art-Feld der Neuanlage im Schleifen-Tab. */
function newBox(root) {
  return root.querySelector("select.loop-disc").parentNode.parentNode.querySelector(".new-disc");
}
"""


@needs_node
def test_loop_tab_creates_a_yes_no_attribute_and_inserts_with_it(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
const posted = [];
respond((p, m, b) => {
  if (m === "POST") posted.push({ p, b });
  if (p.endsWith("/data-elements") && m === "POST") {
    return created(b);
  }
  return { body: schema };
});
const root = openLoopTab();
const sel = root.querySelector("select.loop-disc");
const loopBox = newBox(root);
loopBox.querySelector("input").value = "Nacharbeit nötig";
byId("loop-label").value = "Nacharbeit erledigen";
const confirm = root.querySelector("button.btn.primary");
const disabled = !!confirm.disabled;
await confirm.click();
return { selected: sel.value, newType: loopBox.querySelector("select").value,
  disabled, posted, rows: textOf(sel.parentNode.parentNode) };
""",
        tmp_path,
    )
    assert res["selected"] == "__new__"          # ohne Elemente vorgewaehlt
    assert res["newType"] == "BOOLEAN"           # Ja/Nein vorgeschlagen
    assert res["disabled"] is False              # nicht mehr gesperrt
    assert "Wiederholen, solange der Wert" in res["rows"]  # Ja/Nein-Zeile steht
    assert [x["p"] for x in res["posted"]] == ["/schemas/s/data-elements", "/schemas/s/loop-insert"]
    assert res["posted"][0]["b"] == {"name": "Nacharbeit nötig", "data_type": "BOOLEAN"}
    loop = res["posted"][1]["b"]
    assert loop["discriminator"] == "de_1" and loop["repeat_value"] is True


@needs_node
def test_rejected_loop_insert_removes_the_new_attribute_again(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
respond((p, m, b) => {
  if (p.endsWith("/data-elements") && m === "POST") {
    return created(b);
  }
  if (p.endsWith("/loop-insert")) {
    const f = { rule: "K6", code: "K6.empty-body", message: "x" };
    return { status: 422, body: { detail: { findings: [f] } } };
  }
  return { body: schema };
});
const root = openLoopTab();
const loopBox = newBox(root);
loopBox.querySelector("input").value = "Nacharbeit nötig";
byId("loop-label").value = "Nacharbeit erledigen";
await root.querySelector("button.btn.primary").click();
return { calls: calls.filter((c) => !c.startsWith("GET")) };
""",
        tmp_path,
    )
    # Grund: der Kern lehnt das Einfuegen ab -> das eben angelegte Merkmal geht wieder.
    assert res["calls"] == [
        "POST /schemas/s/data-elements",
        "POST /schemas/s/loop-insert",
        "DELETE /schemas/s/data-elements/de_1",
    ]


@needs_node
def test_loop_tab_without_a_name_creates_nothing(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
respond(() => ({ body: schema }));
const root = openLoopTab();
byId("loop-label").value = "Nacharbeit erledigen";
await root.querySelector("button.btn.primary").click();
return { calls: calls.filter((c) => !c.startsWith("GET")) };
""",
        tmp_path,
    )
    assert res["calls"] == []  # kein Element ohne Namen


@needs_node
def test_existing_attribute_is_used_without_creating_one(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
schema.data_elements = {
  ok: { id: "ok", name: "Fertig", data_type: "BOOLEAN", source: "INSTANCE" } };
const posted = [];
respond((p, m, b) => { if (m === "POST") posted.push({ p, b }); return { body: schema }; });
const root = openLoopTab();
const sel = root.querySelector("select.loop-disc");
const hidden = newBox(root).style.display;
byId("loop-label").value = "Nacharbeit erledigen";
await root.querySelector("button.btn.primary").click();
return { selected: sel.value, hidden, posted };
""",
        tmp_path,
    )
    assert res["selected"] == "ok" and res["hidden"] == "none"
    assert [x["p"] for x in res["posted"]] == ["/schemas/s/loop-insert"]
    assert res["posted"][0]["b"]["discriminator"] == "ok"
