# SPDX-License-Identifier: BUSL-1.1
"""Die Live-Landkarte zeigt beim Öffnen die aktiven Schritte.

Bei großen Prozessen (Order-to-Cash) begann die Karte oben links -- dort lag
nichts, man sah eine leere Fläche bis zum Klick auf „Einpassen“.
"""

from __future__ import annotations

from pathlib import Path

from web_source import app_js_source
from web_vm import needs_node, run_app_js


@needs_node
def test_active_region_covers_ready_and_running_steps_only(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const L = { pos: {
  a: { x: 0, y: 0, w: 100, h: 50 }, b: { x: 900, y: 400, w: 100, h: 50 },
  c: { x: 1200, y: 100, w: 100, h: 50 } } };
return {
  some: activeRegion(L, { node_states: { a: "COMPLETED", b: "ACTIVATED", c: "RUNNING" } }),
  none: activeRegion(L, { node_states: { a: "COMPLETED", b: "SKIPPED", c: "NOT_ACTIVATED" } }),
};
""",
        tmp_path,
    )
    assert res["some"] == {"x0": 900, "y0": 100, "x1": 1300, "y1": 450}
    assert res["none"] is None  # dann wird das ganze Modell eingepasst


def test_instance_maps_focus_their_active_steps_on_show() -> None:
    src = app_js_source()
    graph = src[src.index("function renderGraph("):]
    graph = graph[:graph.index("\n}\n")]
    assert "else if (opts.instance) fitWhenVisible(wrap, activeRegion(L, opts.instance));" in graph
    fit = src[src.index("function fitWhenVisible("):]
    fit = fit[:fit.index("\n}\n")]
    assert "centerOn(null, focus)" in fit
