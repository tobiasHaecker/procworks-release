# SPDX-License-Identifier: BUSL-1.1
"""Im Kontrollfluss überdecken sich keine Beschriftungen und Overlays.

Der Bedienhinweis lag unter „▶ weiter rechts“ und der Schritt-Karte, die
Schleifenbeschriftung auf Daten-Badges, und eine lange Bedingung eines
mittleren Zweigs lief in den Nachbarknoten.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_vm import APP_JS, needs_node, run_app_js

STYLES = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")

_LOOP = r"""
const schema = { id: "s", name: "L", version: 1, lifecycle_state: "ENTWURF",
  nodes: { start: { id: "start", type: "START" }, ls: { id: "ls", type: "LOOP_START" },
    a: { id: "a", type: "ACTIVITY", label: "Nacharbeit" }, le: { id: "le", type: "LOOP_END" },
    end: { id: "end", type: "END" } },
  edges: [{ source: "start", target: "ls" }, { source: "ls", target: "a" },
    { source: "a", target: "le" }, { source: "le", target: "end" }],
  data_elements: { x: { id: "x", name: "X", data_type: "BOOLEAN", source: "INSTANCE" },
    y: { id: "y", name: "Y", data_type: "STRING", source: "INSTANCE" },
    z: { id: "z", name: "Z", data_type: "STRING", source: "INSTANCE" } },
  data_accesses: [{ node_id: "a", element_id: "x", mode: "WRITE", mandatory: true },
    { node_id: "a", element_id: "y", mode: "WRITE", mandatory: true },
    { node_id: "a", element_id: "z", mode: "WRITE", mandatory: true }],
  staff_rules: { a: { kind: "ROLE", ref: "r" } }, service_bindings: {},
  loop_decisions: { le: { discriminator: "x", max_iterations: 3 } } };
"""


@needs_node
def test_loop_caption_sits_below_the_badges(tmp_path: Path) -> None:
    res = run_app_js(
        _LOOP
        + r"""
const L = layoutSchema(schema);
const g = renderGraph(schema, {});
const cap = g.querySelectorAll("text").find((t) => t.classList.contains("gloop-txt"));
const badgeBottom = L.pos.a.y + L.pos.a.h + nodeBadgeStackHeight(schema, schema.nodes.a);
return { capY: Number(cap.getAttribute("y")), badgeBottom,
  badges: nodeBadgeStackHeight(schema, schema.nodes.a) };
""",
        tmp_path,
    )
    assert res["badges"] > 0  # sonst prüfte der Test nichts
    # Oberkante der 10-px-Schrift liegt unter der Unterkante des Badge-Stapels.
    assert res["capY"] - 10 > res["badgeBottom"], res


@needs_node
def test_long_condition_is_shortened_to_the_gap(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const long = "500 <= Bestellwert in Euro einschließlich Nebenkosten < 1000";
return { long: fitCaption(long, 74 - 8), usual: fitCaption("betrag >= 1000", 66),
  short: fitCaption("x < 5", 66) };
""",
        tmp_path,
    )
    # Übliche Bedingungen bleiben vollständig (notfalls zweizeilig) ...
    assert " ".join(res["usual"]) == "betrag >= 1000" and len(res["usual"]) <= 2
    assert res["short"] == ["x < 5"]
    # ... nur sehr lange enden nach zwei Zeilen mit „…“, jede passt in die Lücke.
    assert len(res["long"]) == 2 and res["long"][-1].endswith("…")
    assert all(len(line) <= 12 for line in res["long"])


def test_canvas_hint_sits_away_from_arrows_fit_button_and_card() -> None:
    rule = re.search(r"(?m)^\.canvas-hint \{([^}]*)\}", STYLES).group(1)
    assert "left: 10px" in rule and "top: 8px" in rule
    assert "bottom:" not in rule and "right:" not in rule
    assert "max-width: calc(100% - 130px)" in rule  # nie unter den Einpassen-Knopf
    assert re.search(r"\.canvas-hint \{ display: none; \}", STYLES)  # mobil ausgeblendet
    assert ".graph-body:has(.step-card) .canvas-hint { display: none; }" in STYLES


@needs_node
def test_condition_stays_whole_where_it_hits_no_node(tmp_path: Path) -> None:
    """Gekürzt wird nur, wo die volle Beschriftung einen Knoten schneidet."""
    res = run_app_js(
        r"""
const L = { pos: { a: { x: 0, y: 0, w: 100, h: 50 }, b: { x: 200, y: 0, w: 100, h: 50 } } };
const schema = { nodes: { a: { type: "START" }, b: { type: "END" } } };
return {
  free: labelHitsNode(L, schema, { x0: 60, x1: 240, y0: 100, y1: 112 }),
  hit: labelHitsNode(L, schema, { x0: 60, x1: 240, y0: 10, y1: 22 }),
};
""",
        tmp_path,
    )
    assert res == {"free": False, "hit": True}
