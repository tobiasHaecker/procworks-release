# SPDX-License-Identifier: BUSL-1.1
"""Ein leerer XOR-Zweig ist sichtbar, beschriftet und befüllbar.

Die Kante ``split -> join`` eines leeren Zweigs lief gerade über die
Mittellinie -- durch den Knoten eines anderen Zweigs, die Bedingung mitten in
dessen Kasten. Simulation und Ad-hoc-Auswahl nannten Verzweigungen nur
„XOR ▶“. Geprüft wird das Verhalten von ``web/app.js`` in Node.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

# Erfassen -> XOR (leer | A | C) -> Ende. Der leere Zweig steht zuerst, liegt
# also oben; die Mittellinie gehoert A.
_SCHEMA = r"""
const schema = { id: "s1", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: {
    start: { id: "start", type: "START" },
    e: { id: "e", type: "ACTIVITY", label: "Betrag erfassen" },
    sp: { id: "sp", type: "XOR_SPLIT" }, a: { id: "a", type: "ACTIVITY", label: "Team" },
    c: { id: "c", type: "ACTIVITY", label: "Leitung" }, jn: { id: "jn", type: "XOR_JOIN" },
    end: { id: "end", type: "END" } },
  edges: [
    { source: "start", target: "e" }, { source: "e", target: "sp" },
    { source: "sp", target: "jn", condition: "500 <= betrag < 1000" },
    { source: "sp", target: "a", condition: "betrag < 500" },
    { source: "sp", target: "c", condition: "betrag >= 1000" },
    { source: "a", target: "jn" }, { source: "c", target: "jn" }, { source: "jn", target: "end" } ],
  data_elements: {}, data_accesses: [], staff_rules: {}, service_bindings: {} };
state.schema = schema; state.schemaId = "s1";
"""


@needs_node
def test_empty_branch_gets_its_own_lane_clear_of_every_node(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
const L = layoutSchema(schema);
const y = L.edgeLanes["sp->jn"];
const x1 = L.pos.sp.x + L.pos.sp.w, x2 = L.pos.jn.x;
const hits = Object.entries(L.pos).filter(([id, p]) =>
  id !== "sp" && id !== "jn" && p.x < x2 && p.x + p.w > x1 && y > p.y && y < p.y + p.h)
  .map(([id]) => id);
return { y, splitY: L.pos.sp.y + L.pos.sp.h / 2, hits };
""",
        tmp_path,
    )
    assert res["y"] is not None and abs(res["y"] - res["splitY"]) > 1
    assert res["hits"] == [], f"Bahn des leeren Zweigs schneidet: {res['hits']}"


@needs_node
def test_rendered_empty_branch_runs_on_its_lane_with_caption(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
const g = renderGraph(schema, {});
const L = layoutSchema(schema);
const path = g.querySelectorAll("path").find((p) => p.getAttribute("data-edge") === "sp->jn");
const texts = g.querySelectorAll("text").map((t) => textOf(t));
return { d: path.getAttribute("d"), lane: L.edgeLanes["sp->jn"], texts };
""",
        tmp_path,
    )
    assert f" {res['lane']}" in res["d"]  # die Kante laeuft ueber die Bahn
    assert any(t.startswith("leerer Zweig") for t in res["texts"])


@needs_node
def test_plus_on_a_branch_edge_inserts_at_that_branch_start(tmp_path: Path) -> None:
    """Das „+“ einer Zweigkante schickt das Ziel mit; angeboten wird nur „Schritt“."""
    res = run_app_js(
        _SCHEMA
        + r"""
const posted = [];
respond((p, m, b) => { if (m === "POST") posted.push({ p, b }); return { body: schema }; });
globalThis.refreshSchema = async () => {}; globalThis.render = () => {};
openInsertModal("sp", "jn");
const root = byId("modal-root");
const tabs = root.querySelectorAll("button").filter((b) => b.parentNode.classList.contains("tabs"));
const caption = textOf(root);
document.body.appendChild(root);
byId("ins-label").value = "Kurzprüfung";
await root.querySelector("button.btn.primary").click();
return { tabs: tabs.map((t) => textOf(t)), caption, posted };
""",
        tmp_path,
    )
    assert res["tabs"] == ["Seriell"]
    assert "Anfang des Zweigs: leerer Zweig" in res["caption"]
    assert res["posted"][0]["b"] == {
        "label": "Kurzprüfung", "after_node_id": "sp", "before_node_id": "jn"}


@needs_node
def test_simulation_and_adhoc_name_gateways_and_empty_branches(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
return {
  split: nodeCaptionInContext(schema, schema.nodes.sp),
  join: nodeCaptionInContext(schema, schema.nodes.jn),
  empty: branchCaption(schema, "sp", "jn"),
  full: branchCaption(schema, "sp", "a"),
  named: nodeCaptionInContext(schema, schema.nodes.a),
};
""",
        tmp_path,
    )
    assert res["split"] == "Entscheidung nach „Betrag erfassen“"
    assert res["join"].startswith("Ende der Entscheidung")
    assert res["empty"].startswith("leerer Zweig (")
    assert res["full"].startswith("Team (")
    assert res["named"] == "Team"


@needs_node
def test_empty_lane_stays_inside_the_canvas_top_and_bottom(tmp_path: Path) -> None:
    """Liegt die leere Bahn ganz oben oder ganz unten, wächst die Fläche mit --
    vorher wurde die Bahn unter dem letzten Knoten abgeschnitten."""
    res = run_app_js(
        _SCHEMA
        + r"""
const out = {};
for (const where of ["top", "bottom"]) {
  const s = JSON.parse(JSON.stringify(schema));
  const empty = s.edges.find((e) => e.source === "sp" && e.target === "jn");
  s.edges = s.edges.filter((e) => e !== empty);
  if (where === "top") s.edges.unshift(empty); else s.edges.push(empty);
  const L = layoutSchema(s);
  const y = L.edgeLanes["sp->jn"];
  out[where] = { y, height: L.height };
}
return out;
""",
        tmp_path,
    )
    for where in ("top", "bottom"):
        y, height = res[where]["y"], res[where]["height"]
        assert 16 <= y <= height - 24, f"{where}: Bahn {y} ausserhalb 0..{height}"
